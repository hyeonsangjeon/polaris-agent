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
from .client import DaemonClient, DaemonClientError, read_token

app = typer.Typer(add_completion=False, no_args_is_help=True, help="Durable local agent runtime.")
daemon_app = typer.Typer(add_completion=False, help="Manage the local daemon.")
backup_app = typer.Typer(add_completion=False, help="Export or restore encrypted local state.")
app.add_typer(daemon_app, name="daemon")
app.add_typer(backup_app, name="backup")
console = Console()
_TERMINAL = {"completed", "failed", "cancelled"}


class State:
    def __init__(self, config_file: Path | None, url: str | None) -> None:
        self.config_file = config_file
        self.config = load_config(config_file)
        self.url = url or f"http://{self.config.daemon.host}:{self.config.daemon.port}"

    def client(self) -> DaemonClient:
        token = secret_from_env(self.config.daemon.api_token_env)
        if token is None:
            token_path = self.config.daemon.token_file or self.config.data_dir / "api-token"
            token = read_token(token_path)
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
    return LaunchdServiceManager(
        data_dir=state.config.data_dir,
        config_file=state.config_file or default_paths().config_file,
        provider_api_key_envs={
            name: spec.api_key_env
            for name, spec in state.config.providers.items()
            if spec.api_key_env is not None
        },
    )


def _backup_manager(state: State) -> BackupManager:
    paths = state.config.paths
    return BackupManager(
        data_dir=state.config.data_dir,
        config_file=state.config_file or default_paths().config_file,
        journal_file=paths.journal_file,
        artifact_dir=paths.artifact_dir,
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
