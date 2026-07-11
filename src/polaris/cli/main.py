"""Typer-based local daemon client."""
# ruff: noqa: B008

from __future__ import annotations

import json
import os
import re
import secrets
import time
from pathlib import Path
from typing import Annotated, Any
from urllib.parse import urlencode

import typer
from rich.console import Console
from rich.table import Table

from ..backup import BackupError, BackupManager
from ..config import (
    AppConfig,
    DaemonConfig,
    ProviderSpec,
    ToolConfig,
    load_config,
    save_config,
    secret_from_env,
)
from ..daemon.main import serve as serve_daemon
from ..daemon.service_manager import LaunchdServiceManager, ServiceManagerError
from ..paths import default_paths
from ..secrets import (
    SecretsFile,
    SecretsFileError,
    runtime_environment,
    validate_secret_name,
)
from .client import DaemonClient, DaemonClientError, read_token

app = typer.Typer(add_completion=False, no_args_is_help=True, help="Durable local agent runtime.")
daemon_app = typer.Typer(add_completion=False, help="Manage the local daemon.")
backup_app = typer.Typer(add_completion=False, help="Export or restore encrypted local state.")
memory_app = typer.Typer(add_completion=False, help="Manage scope-isolated curated memory.")
cron_app = typer.Typer(add_completion=False, help="Manage durable scheduled jobs.")
channels_app = typer.Typer(add_completion=False, help="Inspect private channel delivery.")
secrets_app = typer.Typer(
    add_completion=False,
    help=(
        "Manage owner-only runtime secrets. Relative POLARIS_SECRETS_FILE paths "
        "resolve under data_dir."
    ),
)
app.add_typer(daemon_app, name="daemon")
app.add_typer(backup_app, name="backup")
app.add_typer(memory_app, name="memory")
app.add_typer(cron_app, name="cron")
app.add_typer(channels_app, name="channels")
app.add_typer(secrets_app, name="secrets")
console = Console()
_TERMINAL = {"completed", "failed", "cancelled"}


class State:
    def __init__(self, config_file: Path | None, url: str | None) -> None:
        self.config_file = config_file
        self.config = load_config(config_file)
        self.url = url or f"http://{self.config.daemon.host}:{self.config.daemon.port}"

    def client(self) -> DaemonClient:
        secrets_path = _runtime_secrets_path(self.config)
        try:
            env = runtime_environment(secrets_path)
        except SecretsFileError as exc:
            raise DaemonClientError(str(exc)) from exc
        token = secret_from_env(self.config.daemon.api_token_env, env)
        if token is None:
            token = read_token(self.config.paths.token_file)
        return DaemonClient(self.url, token)


@app.callback()
def callback(
    ctx: typer.Context,
    config_file: Path | None = typer.Option(None, "--config", dir_okay=False),
    url: str | None = typer.Option(None, "--url"),
) -> None:
    ctx.obj = State(config_file, url)


def _state(ctx: typer.Context) -> State:
    return ctx.ensure_object(State)


def _runtime_secrets_path(config: AppConfig) -> Path:
    override = os.environ.get("POLARIS_SECRETS_FILE")
    return config.resolve_secrets_file(override)


def _emit(value: Any, json_output: bool) -> None:
    if json_output:
        typer.echo(json.dumps(value, indent=2, sort_keys=True, ensure_ascii=False))
    elif isinstance(value, dict):
        for key, item in value.items():
            console.print(f"[bold]{key}:[/bold] {item}")
    else:
        console.print(value)


def _fail(exc: Exception) -> None:
    typer.echo(f"Error: {exc}", err=True)
    raise typer.Exit(1)


@app.command()
def setup(
    config_file: Path | None = typer.Option(None, "--config", dir_okay=False),
    data_dir: Path | None = typer.Option(None, "--data-dir", file_okay=False),
    root: Path = typer.Option(Path.cwd(), "--root", exists=True, file_okay=False),
    force: bool = typer.Option(False, "--force"),
    json_output: bool = typer.Option(False, "--json"),
) -> None:
    paths = default_paths()
    destination = (config_file or paths.config_file).expanduser().resolve()
    selected_data = (data_dir or paths.data_dir).expanduser().resolve()
    if destination.exists() and not force:
        _fail(RuntimeError(f"configuration already exists at {destination}; use --force"))
    token_file = selected_data / "api-token"
    config = AppConfig(
        data_dir=selected_data,
        providers={
            "ollama": ProviderSpec.model_validate(
                {
                    "kind": "ollama",
                    "model": "llama3.2",
                    "base_url": "http://127.0.0.1:11434",
                }
            )
        },
        tools=ToolConfig(roots=(root.expanduser().resolve(),)),
        daemon=DaemonConfig(token_file=token_file),
    )
    config.paths.ensure()
    token = secrets.token_urlsafe(48)
    descriptor = os.open(token_file, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    try:
        os.write(descriptor, (token + "\n").encode())
        os.fsync(descriptor)
    finally:
        os.close(descriptor)
    os.chmod(token_file, 0o600)
    save_config(config, destination)
    _emit(
        {"configured": True, "config_file": str(destination), "data_dir": str(selected_data)},
        json_output,
    )


@app.command()
def doctor(ctx: typer.Context, json_output: bool = typer.Option(False, "--json")) -> None:
    try:
        with _state(ctx).client() as client:
            result = client.request("GET", "/v1/providers/doctor")
        _emit(result, json_output)
        if isinstance(result, dict) and any(
            isinstance(value, dict) and not value.get("ok", False) for value in result.values()
        ):
            raise typer.Exit(1)
    except DaemonClientError as exc:
        _fail(exc)


@daemon_app.command("serve")
def daemon_serve(
    ctx: typer.Context,
    host: str | None = typer.Option(None, "--host"),
    port: int | None = typer.Option(None, "--port", min=1, max=65535),
    allow_remote: bool = typer.Option(False, "--allow-remote"),
) -> None:
    state = _state(ctx)
    serve_daemon(state.config_file, host, port, allow_remote)


def _service_manager(state: State) -> LaunchdServiceManager:
    secret_envs = {
        name: spec.api_key_env
        for name, spec in state.config.providers.items()
        if spec.api_key_env is not None
    }
    channel_envs: dict[str, str | None] = {}
    if state.config.channels.telegram.enabled:
        channel_envs["channel:telegram"] = state.config.channels.telegram.token_env
    if state.config.channels.slack.enabled:
        channel_envs["channel:slack-bot"] = state.config.channels.slack.bot_token_env
        channel_envs["channel:slack-app"] = state.config.channels.slack.app_token_env
    secret_envs.update(
        {name: env_name for name, env_name in channel_envs.items() if env_name is not None}
    )
    if state.config.daemon.api_token_env is not None:
        secret_envs["daemon:api-token"] = state.config.daemon.api_token_env
    return LaunchdServiceManager(
        data_dir=state.config.data_dir,
        config_file=state.config_file or default_paths().config_file,
        provider_api_key_envs=secret_envs,
        secrets_file=_runtime_secrets_path(state.config),
    )


def _required_secret_names(config: AppConfig) -> set[str]:
    required = {
        spec.api_key_env
        for spec in config.providers.values()
        if spec.api_key_env is not None
    }
    if config.channels.telegram.enabled and config.channels.telegram.token_env is not None:
        required.add(config.channels.telegram.token_env)
    slack = config.channels.slack
    if slack.enabled:
        required.update(
            name for name in (slack.bot_token_env, slack.app_token_env) if name is not None
        )
    if config.daemon.api_token_env is not None:
        required.add(config.daemon.api_token_env)
    return required


def _secrets_file(state: State) -> SecretsFile:
    return SecretsFile(_runtime_secrets_path(state.config))


@secrets_app.command("set")
def secrets_set(
    ctx: typer.Context,
    name: str,
    from_env: str | None = typer.Option(
        None,
        "--from-env",
        help="Read the value from this environment variable.",
    ),
) -> None:
    try:
        validate_secret_name(name)
        if from_env is None:
            value = str(typer.prompt("Secret value", hide_input=True))
        else:
            validate_secret_name(from_env)
            if from_env not in os.environ:
                raise SecretsFileError(f"environment variable {from_env!r} is not set")
            value = os.environ[from_env]
        _secrets_file(_state(ctx)).set(name, value)
        console.print(f"Stored secret {name}.")
    except (OSError, SecretsFileError) as exc:
        _fail(exc)


@secrets_app.command("list")
def secrets_list(ctx: typer.Context) -> None:
    try:
        for name in _secrets_file(_state(ctx)).names():
            typer.echo(name)
    except (OSError, SecretsFileError) as exc:
        _fail(exc)


@secrets_app.command("remove")
def secrets_remove(ctx: typer.Context, name: str) -> None:
    try:
        removed = _secrets_file(_state(ctx)).remove(name)
        console.print(f"{'Removed' if removed else 'No stored value for'} secret {name}.")
    except (OSError, SecretsFileError) as exc:
        _fail(exc)


@secrets_app.command("check")
def secrets_check(ctx: typer.Context, names: list[str] | None = typer.Argument(None)) -> None:
    state = _state(ctx)
    try:
        required = set(names or _required_secret_names(state.config))
        for name in required:
            validate_secret_name(name)
        missing = _secrets_file(state).check(required)
        if missing:
            typer.echo(f"Missing required secrets: {', '.join(missing)}", err=True)
            for name in missing:
                typer.echo(f"Run `polaris secrets set {name}`.", err=True)
            raise typer.Exit(1)
        typer.echo("Runtime secrets file is valid.")
    except (OSError, SecretsFileError) as exc:
        _fail(exc)


def _backup_manager(state: State) -> BackupManager:
    paths = state.config.paths
    return BackupManager(
        data_dir=state.config.data_dir,
        config_file=state.config_file or default_paths().config_file,
        journal_file=paths.journal_file,
        artifact_dir=paths.artifact_dir,
        secrets_file=_runtime_secrets_path(state.config),
    )


@daemon_app.command("install")
def daemon_install(ctx: typer.Context) -> None:
    try:
        path = _service_manager(_state(ctx)).install()
        console.print(f"Installed LaunchAgent: {path}")
    except ServiceManagerError as exc:
        _fail(exc)


@daemon_app.command("start")
def daemon_start(ctx: typer.Context) -> None:
    try:
        _service_manager(_state(ctx)).start()
        console.print("Polaris daemon started.")
    except ServiceManagerError as exc:
        _fail(exc)


@daemon_app.command("stop")
def daemon_stop(ctx: typer.Context) -> None:
    try:
        _service_manager(_state(ctx)).stop()
        console.print("Polaris daemon stopped.")
    except ServiceManagerError as exc:
        _fail(exc)


@daemon_app.command("uninstall")
def daemon_uninstall(ctx: typer.Context) -> None:
    try:
        _service_manager(_state(ctx)).uninstall()
        console.print("Polaris LaunchAgent uninstalled.")
    except ServiceManagerError as exc:
        _fail(exc)


@daemon_app.command("status")
def daemon_status(ctx: typer.Context, json_output: bool = typer.Option(False, "--json")) -> None:
    state = _state(ctx)
    try:
        response = http_health(state.url)
        _emit(response, json_output)
    except DaemonClientError as exc:
        _fail(exc)


def _backup_passphrase(env_name: str | None, *, confirm: bool) -> str:
    if env_name is not None:
        if re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", env_name) is None:
            raise typer.BadParameter("passphrase environment variable name is invalid")
        value = os.environ.get(env_name)
        if not value:
            raise typer.BadParameter(
                f"passphrase environment variable {env_name!r} is not set or empty"
            )
        return value
    return str(
        typer.prompt(
            "Backup passphrase",
            hide_input=True,
            confirmation_prompt=confirm,
        )
    )


@backup_app.command("export")
def backup_export(
    ctx: typer.Context,
    path: Path = typer.Argument(..., dir_okay=False),
    passphrase_env: str | None = typer.Option(
        None,
        "--passphrase-env",
        help="Read the passphrase from this environment variable.",
    ),
) -> None:
    try:
        report = _backup_manager(_state(ctx)).export(
            path, _backup_passphrase(passphrase_env, confirm=True)
        )
        console.print(
            f"Encrypted backup written to {report.path} "
            f"({report.files} files, {report.bytes} bytes)."
        )
        console.print("Credentials were excluded and must be re-established after import.")
    except (BackupError, OSError, ValueError) as exc:
        _fail(exc)


@backup_app.command("import")
def backup_import(
    ctx: typer.Context,
    path: Path = typer.Argument(..., exists=True, dir_okay=False),
    force: bool = typer.Option(False, "--force", help="Replace existing local state."),
    passphrase_env: str | None = typer.Option(
        None,
        "--passphrase-env",
        help="Read the passphrase from this environment variable.",
    ),
) -> None:
    try:
        report = _backup_manager(_state(ctx)).import_archive(
            path,
            _backup_passphrase(passphrase_env, confirm=False),
            force=force,
        )
        console.print(f"Imported {report.files} files from {report.path}.")
        console.print("Re-establish API and provider credentials before starting the daemon.")
    except (BackupError, OSError, ValueError) as exc:
        _fail(exc)


def http_health(url: str) -> Any:
    import httpx

    try:
        response = httpx.get(f"{url.rstrip('/')}/health", timeout=5)
        response.raise_for_status()
        return response.json()
    except httpx.HTTPError as exc:
        raise DaemonClientError(f"daemon is unavailable: {exc}") from exc


def _worker(value: str, index: int) -> dict[str, str]:
    provider, separator, role = value.partition(":")
    if not separator or not provider.strip() or not role.strip():
        raise typer.BadParameter("workers must use provider:role")
    return {
        "id": f"worker-{index}",
        "provider": provider.strip(),
        "role": role.strip(),
        "instructions": f"Research as the {role.strip()} specialist and cite evidence.",
    }


@app.command()
def run(
    ctx: typer.Context,
    prompt: Annotated[str, typer.Argument(help="Prompt or research question.")],
    mode: str = typer.Option("single", "--mode"),
    provider: str | None = typer.Option(None, "--provider"),
    worker: list[str] | None = typer.Option(None, "--worker"),
    verifier: str | None = typer.Option(None, "--verifier"),
    synthesizer: str | None = typer.Option(None, "--synthesizer"),
    call_limit: int | None = typer.Option(None, "--call-limit", min=0),
    token_limit: int | None = typer.Option(None, "--token-limit", min=0),
    micro_usd_limit: int | None = typer.Option(None, "--micro-usd-limit", min=0),
    wall_seconds_limit: float | None = typer.Option(None, "--wall-seconds-limit", min=0),
    wait: bool = typer.Option(False, "--wait"),
    json_output: bool = typer.Option(False, "--json"),
) -> None:
    budget = {
        key: value
        for key, value in {
            "call_limit": call_limit,
            "token_limit": token_limit,
            "micro_usd_limit": micro_usd_limit,
            "wall_seconds_limit": wall_seconds_limit,
        }.items()
        if value is not None
    }
    try:
        with _state(ctx).client() as client:
            payload: dict[str, Any]
            if mode == "single":
                payload = {"prompt": prompt, "provider": provider, "budget": budget}
                result = client.request("POST", "/v1/runs/single", json=payload)
            elif mode == "fan-out":
                if not worker or verifier is None or synthesizer is None:
                    raise typer.BadParameter(
                        "fan-out requires --worker, --verifier, and --synthesizer"
                    )
                payload = {
                    "question": prompt,
                    "workers": [_worker(value, index) for index, value in enumerate(worker, 1)],
                    "verifier": verifier,
                    "synthesizer": synthesizer,
                    "budget": budget,
                }
                result = client.request("POST", "/v1/runs/fanout", json=payload)
            elif mode == "foundry-router":
                if provider is None:
                    raise typer.BadParameter("foundry-router requires --provider")
                payload = {
                    "question": prompt,
                    "provider": provider,
                    "budget": budget,
                }
                result = client.request(
                    "POST",
                    "/v1/runs/foundry-router",
                    json=payload,
                )
            else:
                raise typer.BadParameter("mode must be single, fan-out, or foundry-router")
            if wait:
                result = _wait(client, str(result["id"]))
                if result.get("status") == "completed":
                    result["replay"] = client.request("GET", f"/v1/runs/{result['id']}/replay")
            _emit(result, json_output)
            if result.get("status") in {"failed", "cancelled"}:
                raise typer.Exit(1)
    except DaemonClientError as exc:
        _fail(exc)


def _wait(client: DaemonClient, run_id: str) -> dict[str, Any]:
    while True:
        result = client.request("GET", f"/v1/runs/{run_id}")
        if not isinstance(result, dict):
            raise DaemonClientError("daemon returned an invalid run response")
        if result["status"] in _TERMINAL:
            return result
        time.sleep(0.25)


@app.command("runs")
def runs_command(
    ctx: typer.Context,
    status: str | None = typer.Option(None, "--status"),
    json_output: bool = typer.Option(False, "--json"),
) -> None:
    try:
        with _state(ctx).client() as client:
            result = client.request("GET", "/v1/runs", params={"run_status": status})
        if json_output:
            _emit(result, True)
            return
        table = Table("ID", "Mode", "Status", "Created")
        for item in result:
            table.add_row(item["id"], item["mode"], item["status"], item["created_at"])
        console.print(table)
    except DaemonClientError as exc:
        _fail(exc)


@app.command()
def show(
    ctx: typer.Context,
    run_id: str,
    json_output: bool = typer.Option(False, "--json"),
) -> None:
    _simple_request(ctx, "GET", f"/v1/runs/{run_id}", json_output=json_output)


@app.command()
def approve(
    ctx: typer.Context,
    approval_id: str,
    reason: str | None = typer.Option(None, "--reason"),
    json_output: bool = typer.Option(False, "--json"),
) -> None:
    _decision(ctx, approval_id, "approved", reason, json_output)


@app.command()
def deny(
    ctx: typer.Context,
    approval_id: str,
    reason: str | None = typer.Option(None, "--reason"),
    json_output: bool = typer.Option(False, "--json"),
) -> None:
    _decision(ctx, approval_id, "rejected", reason, json_output)


def _decision(
    ctx: typer.Context, approval_id: str, decision: str, reason: str | None, json_output: bool
) -> None:
    _simple_request(
        ctx,
        "POST",
        f"/v1/approvals/{approval_id}",
        payload={"decision": decision, "reason": reason, "decided_by": "cli"},
        json_output=json_output,
    )


@app.command()
def resume(
    ctx: typer.Context,
    run_id: str,
    json_output: bool = typer.Option(False, "--json"),
) -> None:
    _simple_request(ctx, "POST", f"/v1/runs/{run_id}/resume", json_output=json_output)


@app.command()
def replay(
    ctx: typer.Context,
    run_id: str,
    json_output: bool = typer.Option(False, "--json"),
) -> None:
    _simple_request(ctx, "GET", f"/v1/runs/{run_id}/replay", json_output=json_output)


@app.command()
def models(ctx: typer.Context, json_output: bool = typer.Option(False, "--json")) -> None:
    _simple_request(ctx, "GET", "/v1/models", json_output=json_output)


@app.command()
def tools(ctx: typer.Context, json_output: bool = typer.Option(False, "--json")) -> None:
    _simple_request(ctx, "GET", "/v1/tools", json_output=json_output)


def _query(path: str, **values: object) -> str:
    selected = {key: value for key, value in values.items() if value is not None}
    return path if not selected else f"{path}?{urlencode(selected)}"


@memory_app.command("list")
def memory_list_command(
    ctx: typer.Context,
    profile_id: str = typer.Option("default", "--profile"),
    subject_key: str = typer.Option("local", "--subject"),
    include_tombstones: bool = typer.Option(False, "--include-tombstones"),
    limit: int | None = typer.Option(None, "--limit", min=1),
    json_output: bool = typer.Option(False, "--json"),
) -> None:
    _simple_request(
        ctx,
        "GET",
        _query(
            "/v1/memory",
            profile_id=profile_id,
            subject_key=subject_key,
            include_tombstones=str(include_tombstones).lower(),
            limit=limit,
        ),
        json_output=json_output,
    )


@memory_app.command("search")
def memory_search_command(
    ctx: typer.Context,
    query: str,
    profile_id: str = typer.Option("default", "--profile"),
    subject_key: str = typer.Option("local", "--subject"),
    limit: int = typer.Option(10, "--limit", min=1, max=50),
    json_output: bool = typer.Option(False, "--json"),
) -> None:
    _simple_request(
        ctx,
        "GET",
        _query(
            "/v1/memory/search",
            query=query,
            profile_id=profile_id,
            subject_key=subject_key,
            limit=limit,
        ),
        json_output=json_output,
    )


@memory_app.command("add")
def memory_add_command(
    ctx: typer.Context,
    content: str,
    profile_id: str = typer.Option("default", "--profile"),
    subject_key: str = typer.Option("local", "--subject"),
    kind: str = typer.Option("fact", "--kind"),
    trust_level: str = typer.Option("user_asserted", "--trust"),
    provenance_run_id: str | None = typer.Option(None, "--run-id"),
    json_output: bool = typer.Option(False, "--json"),
) -> None:
    _simple_request(
        ctx,
        "POST",
        "/v1/memory",
        payload={
            "profile_id": profile_id,
            "subject_key": subject_key,
            "content": content,
            "kind": kind,
            "trust_level": trust_level,
            "provenance_run_id": provenance_run_id,
        },
        json_output=json_output,
    )


@memory_app.command("revise")
def memory_revise_command(
    ctx: typer.Context,
    entry_id: str,
    content: str,
    expected_revision: int = typer.Option(..., "--revision", min=1),
    expected_hash: str | None = typer.Option(None, "--hash"),
    profile_id: str = typer.Option("default", "--profile"),
    subject_key: str = typer.Option("local", "--subject"),
    kind: str | None = typer.Option(None, "--kind"),
    trust_level: str | None = typer.Option(None, "--trust"),
    json_output: bool = typer.Option(False, "--json"),
) -> None:
    _simple_request(
        ctx,
        "PUT",
        f"/v1/memory/{entry_id}",
        payload={
            key: value
            for key, value in {
                "profile_id": profile_id,
                "subject_key": subject_key,
                "content": content,
                "kind": kind,
                "trust_level": trust_level,
                "expected_revision": expected_revision,
                "expected_hash": expected_hash,
            }.items()
            if value is not None
        },
        json_output=json_output,
    )


@memory_app.command("remove")
def memory_remove_command(
    ctx: typer.Context,
    entry_id: str,
    expected_revision: int = typer.Option(..., "--revision", min=1),
    expected_hash: str | None = typer.Option(None, "--hash"),
    profile_id: str = typer.Option("default", "--profile"),
    subject_key: str = typer.Option("local", "--subject"),
    json_output: bool = typer.Option(False, "--json"),
) -> None:
    _simple_request(
        ctx,
        "DELETE",
        f"/v1/memory/{entry_id}",
        payload={
            "profile_id": profile_id,
            "subject_key": subject_key,
            "expected_revision": expected_revision,
            "expected_hash": expected_hash,
        },
        json_output=json_output,
    )


def _job_payload(
    prompt: str,
    provider: str | None,
    delivery_platform: str | None,
    delivery_channel: str | None,
) -> dict[str, Any]:
    delivery = None
    if delivery_platform is not None or delivery_channel is not None:
        if delivery_platform is None or delivery_channel is None:
            raise typer.BadParameter(
                "--delivery-platform and --delivery-channel must be used together"
            )
        delivery = {
            "platform": delivery_platform,
            "channel_id": delivery_channel,
        }
    return {
        "mode": "single",
        "request": {"prompt": prompt, "provider": provider},
        "delivery": delivery,
    }


def _create_job(
    ctx: typer.Context,
    *,
    name: str,
    schedule: dict[str, Any],
    prompt: str,
    provider: str | None,
    catchup: str,
    max_catchup: int,
    delivery_platform: str | None,
    delivery_channel: str | None,
    json_output: bool,
) -> None:
    _simple_request(
        ctx,
        "POST",
        "/v1/jobs",
        payload={
            "name": name,
            "schedule": schedule,
            "payload": _job_payload(
                prompt,
                provider,
                delivery_platform,
                delivery_channel,
            ),
            "catchup_policy": catchup,
            "max_catchup": max_catchup,
        },
        json_output=json_output,
    )


@cron_app.command("once")
def cron_once(
    ctx: typer.Context,
    name: str,
    at: str,
    prompt: str,
    timezone: str = typer.Option("UTC", "--timezone"),
    provider: str | None = typer.Option(None, "--provider"),
    delivery_platform: str | None = typer.Option(None, "--delivery-platform"),
    delivery_channel: str | None = typer.Option(None, "--delivery-channel"),
    json_output: bool = typer.Option(False, "--json"),
) -> None:
    _create_job(
        ctx,
        name=name,
        schedule={"kind": "once", "once_at": at, "timezone": timezone},
        prompt=prompt,
        provider=provider,
        catchup="fire_once",
        max_catchup=1,
        delivery_platform=delivery_platform,
        delivery_channel=delivery_channel,
        json_output=json_output,
    )


@cron_app.command("interval")
def cron_interval(
    ctx: typer.Context,
    name: str,
    seconds: float,
    prompt: str,
    timezone: str = typer.Option("UTC", "--timezone"),
    start_at: str | None = typer.Option(None, "--start-at"),
    provider: str | None = typer.Option(None, "--provider"),
    catchup: str = typer.Option("fire_once", "--catchup"),
    max_catchup: int = typer.Option(1, "--max-catchup", min=1, max=10),
    delivery_platform: str | None = typer.Option(None, "--delivery-platform"),
    delivery_channel: str | None = typer.Option(None, "--delivery-channel"),
    json_output: bool = typer.Option(False, "--json"),
) -> None:
    _create_job(
        ctx,
        name=name,
        schedule={
            "kind": "interval",
            "interval_seconds": seconds,
            "start_at": start_at,
            "timezone": timezone,
        },
        prompt=prompt,
        provider=provider,
        catchup=catchup,
        max_catchup=max_catchup,
        delivery_platform=delivery_platform,
        delivery_channel=delivery_channel,
        json_output=json_output,
    )


@cron_app.command("add")
def cron_add(
    ctx: typer.Context,
    name: str,
    expression: str,
    prompt: str,
    timezone: str = typer.Option(..., "--timezone"),
    provider: str | None = typer.Option(None, "--provider"),
    catchup: str = typer.Option("fire_once", "--catchup"),
    max_catchup: int = typer.Option(1, "--max-catchup", min=1, max=10),
    delivery_platform: str | None = typer.Option(None, "--delivery-platform"),
    delivery_channel: str | None = typer.Option(None, "--delivery-channel"),
    json_output: bool = typer.Option(False, "--json"),
) -> None:
    _create_job(
        ctx,
        name=name,
        schedule={"kind": "cron", "cron": expression, "timezone": timezone},
        prompt=prompt,
        provider=provider,
        catchup=catchup,
        max_catchup=max_catchup,
        delivery_platform=delivery_platform,
        delivery_channel=delivery_channel,
        json_output=json_output,
    )


@cron_app.command("list")
def cron_list(
    ctx: typer.Context,
    state: str | None = typer.Option(None, "--state"),
    json_output: bool = typer.Option(False, "--json"),
) -> None:
    _simple_request(
        ctx,
        "GET",
        _query("/v1/jobs", job_state=state),
        json_output=json_output,
    )


@cron_app.command("show")
def cron_show(
    ctx: typer.Context,
    job_id: str,
    json_output: bool = typer.Option(False, "--json"),
) -> None:
    _simple_request(ctx, "GET", f"/v1/jobs/{job_id}", json_output=json_output)


def _job_action(
    ctx: typer.Context, job_id: str, action: str, json_output: bool
) -> None:
    _simple_request(
        ctx,
        "POST",
        f"/v1/jobs/{job_id}/{action}",
        json_output=json_output,
    )


@cron_app.command("pause")
def cron_pause(
    ctx: typer.Context,
    job_id: str,
    json_output: bool = typer.Option(False, "--json"),
) -> None:
    _job_action(ctx, job_id, "pause", json_output)


@cron_app.command("resume")
def cron_resume(
    ctx: typer.Context,
    job_id: str,
    json_output: bool = typer.Option(False, "--json"),
) -> None:
    _job_action(ctx, job_id, "resume", json_output)


@cron_app.command("cancel")
def cron_cancel(
    ctx: typer.Context,
    job_id: str,
    json_output: bool = typer.Option(False, "--json"),
) -> None:
    _job_action(ctx, job_id, "cancel", json_output)


@cron_app.command("preview")
def cron_preview(
    ctx: typer.Context,
    expression: str,
    after: str,
    timezone: str = typer.Option(..., "--timezone"),
    count: int = typer.Option(5, "--count", min=1, max=100),
    json_output: bool = typer.Option(False, "--json"),
) -> None:
    _simple_request(
        ctx,
        "POST",
        "/v1/jobs/preview",
        payload={
            "schedule": {
                "kind": "cron",
                "cron": expression,
                "timezone": timezone,
            },
            "after": after,
            "count": count,
        },
        json_output=json_output,
    )


@cron_app.command("runs")
def cron_runs(
    ctx: typer.Context,
    job_id: str | None = typer.Option(None, "--job"),
    status: str | None = typer.Option(None, "--status"),
    json_output: bool = typer.Option(False, "--json"),
) -> None:
    _simple_request(
        ctx,
        "GET",
        _query("/v1/jobs/runs", job_id=job_id, run_status=status),
        json_output=json_output,
    )


@cron_app.command("retry")
def cron_retry(
    ctx: typer.Context,
    run_id: str,
    json_output: bool = typer.Option(False, "--json"),
) -> None:
    _simple_request(
        ctx,
        "POST",
        f"/v1/jobs/runs/{run_id}/retry",
        json_output=json_output,
    )


@channels_app.command("status")
def channels_status_command(
    ctx: typer.Context,
    json_output: bool = typer.Option(False, "--json"),
) -> None:
    _simple_request(ctx, "GET", "/v1/channels/status", json_output=json_output)


@channels_app.command("unknown")
def channels_unknown(
    ctx: typer.Context,
    platform: str | None = typer.Option(None, "--platform"),
    limit: int = typer.Option(500, "--limit", min=1, max=5000),
    json_output: bool = typer.Option(False, "--json"),
) -> None:
    _simple_request(
        ctx,
        "GET",
        _query("/v1/channels/outbox/unknown", platform=platform, limit=limit),
        json_output=json_output,
    )


@channels_app.command("mark-sent")
def channels_mark_sent(
    ctx: typer.Context,
    idempotency_key: str,
    note: str = typer.Option(..., "--note"),
    json_output: bool = typer.Option(False, "--json"),
) -> None:
    _simple_request(
        ctx,
        "POST",
        f"/v1/channels/outbox/{idempotency_key}/mark-sent",
        payload={"note": note},
        json_output=json_output,
    )


@channels_app.command("retry")
def channels_retry(
    ctx: typer.Context,
    idempotency_key: str,
    note: str = typer.Option(..., "--note"),
    json_output: bool = typer.Option(False, "--json"),
) -> None:
    _simple_request(
        ctx,
        "POST",
        f"/v1/channels/outbox/{idempotency_key}/retry",
        payload={"note": note},
        json_output=json_output,
    )


def _simple_request(
    ctx: typer.Context,
    method: str,
    path: str,
    *,
    payload: dict[str, Any] | None = None,
    json_output: bool,
) -> None:
    try:
        with _state(ctx).client() as client:
            result = client.request(method, path, json=payload)
        _emit(result, json_output)
    except DaemonClientError as exc:
        _fail(exc)


def main() -> None:
    app()


if __name__ == "__main__":
    main()
