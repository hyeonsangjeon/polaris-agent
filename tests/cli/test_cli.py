from __future__ import annotations

import json
import stat
from importlib import import_module
from pathlib import Path
from typing import Any

import pytest
import typer
from typer.testing import CliRunner

from polaris.cli.client import DaemonClientError
from polaris.cli.main import State, app
from polaris.config import (
    AppConfig,
    DaemonConfig,
    ProviderSpec,
    ToolConfig,
    load_config,
    save_config,
)

runner = CliRunner()
cli_main = import_module("polaris.cli.main")


class FakeClient:
    def __init__(self, responses: list[Any]) -> None:
        self.responses = responses
        self.requests: list[tuple[str, str, object]] = []

    def __enter__(self) -> FakeClient:
        return self

    def __exit__(self, *_args: object) -> None:
        pass

    def request(self, method: str, path: str, **kwargs: object) -> Any:
        self.requests.append((method, path, kwargs.get("json")))
        return self.responses.pop(0)


def test_setup_creates_private_token_and_secret_free_config(tmp_path: Path) -> None:
    config_file = tmp_path / "config.json"
    data_dir = tmp_path / "data"
    result = runner.invoke(
        app,
        [
            "setup",
            "--config",
            str(config_file),
            "--data-dir",
            str(data_dir),
            "--root",
            str(tmp_path),
            "--json",
        ],
    )
    assert result.exit_code == 0, result.output
    token_file = data_dir / "api-token"
    assert stat.S_IMODE(token_file.stat().st_mode) == 0o600
    token = token_file.read_text().strip()
    assert token
    assert token not in config_file.read_text()
    assert load_config(config_file).daemon.token_file == token_file


def test_state_client_prefers_daemon_token_environment(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    config_file = tmp_path / "config.json"
    config = AppConfig(
        data_dir=tmp_path,
        tools=ToolConfig(roots=(tmp_path,)),
        daemon=DaemonConfig(
            api_token_env="POLARIS_TEST_DAEMON_TOKEN",
            token_file=tmp_path / "missing-token",
        ),
    )
    save_config(config, config_file)
    monkeypatch.setenv("POLARIS_TEST_DAEMON_TOKEN", "environment-token")
    captured: dict[str, str] = {}

    def client(base_url: str, token: str) -> object:
        captured.update(base_url=base_url, token=token)
        return object()

    monkeypatch.setattr(cli_main, "DaemonClient", client)

    State(config_file, None).client()

    assert captured == {
        "base_url": "http://127.0.0.1:8765",
        "token": "environment-token",
    }


def test_daemon_install_rejects_provider_api_key_environment(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    config_file = tmp_path / "config.json"
    provider = ProviderSpec.model_validate(
        {
            "kind": "foundry_router",
            "model": "model-router",
            "base_url": "https://resource.services.ai.azure.com/openai/v1",
            "api_key_env": "AZURE_FOUNDRY_API_KEY",
            "api_mode": "responses",
            "azure_auth": "api_key",
        }
    )
    save_config(
        AppConfig(
            data_dir=tmp_path,
            providers={"foundry-router": provider},
            tools=ToolConfig(roots=(tmp_path,)),
        ),
        config_file,
    )
    captured: dict[str, object] = {}

    class FakeServiceManager:
        def __init__(self, **kwargs: object) -> None:
            captured.update(kwargs)

        def install(self) -> Path:
            raise cli_main.ServiceManagerError(
                "launchd cannot inherit provider API-key environment variables; "
                "run in the foreground or use Entra/Managed Identity"
            )

    monkeypatch.setattr(cli_main, "LaunchdServiceManager", FakeServiceManager)

    result = runner.invoke(app, ["--config", str(config_file), "daemon", "install"])

    assert result.exit_code == 1
    assert captured["provider_api_key_envs"] == {
        "foundry-router": "AZURE_FOUNDRY_API_KEY"
    }
    assert "launchd cannot inherit provider API-key environment variables" in result.output
    assert "foreground" in result.output
    assert "Entra/Managed Identity" in result.output


def test_doctor_and_run_use_daemon_client(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    config_file = tmp_path / "config.json"
    setup = runner.invoke(
        app,
        [
            "setup",
            "--config",
            str(config_file),
            "--data-dir",
            str(tmp_path / "data"),
            "--root",
            str(tmp_path),
        ],
    )
    assert setup.exit_code == 0
    fake = FakeClient(
        [
            {"fake": {"ok": True}},
            {"id": "run-1", "status": "created"},
        ]
    )
    monkeypatch.setattr(State, "client", lambda self: fake)
    doctor = runner.invoke(app, ["--config", str(config_file), "doctor", "--json"])
    run = runner.invoke(
        app,
        [
            "--config",
            str(config_file),
            "run",
            "question",
            "--provider",
            "fake",
            "--json",
        ],
    )
    assert doctor.exit_code == 0
    assert json.loads(doctor.output)["fake"]["ok"] is True
    assert run.exit_code == 0
    assert fake.requests[-1][1] == "/v1/runs/single"
    payload = fake.requests[-1][2]
    assert isinstance(payload, dict)
    assert "profile_id" not in payload
    assert "subject_key" not in payload


def test_relative_secrets_override_and_daemon_token_env_use_data_dir(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    config_file = tmp_path / "config.json"
    data_dir = tmp_path / "data"
    config = AppConfig(
        data_dir=data_dir,
        tools=ToolConfig(roots=(tmp_path,)),
        daemon=DaemonConfig(api_token_env="DAEMON_API_TOKEN"),
    )
    save_config(config, config_file)
    monkeypatch.setenv("POLARIS_SECRETS_FILE", "private/runtime.env")
    state = State(config_file, None)

    assert cli_main._runtime_secrets_path(config) == data_dir / "private" / "runtime.env"
    manager = cli_main._service_manager(state)
    assert manager.secrets_file == data_dir / "private" / "runtime.env"
    assert manager.provider_api_key_envs["daemon:api-token"] == "DAEMON_API_TOKEN"
    assert "DAEMON_API_TOKEN" in cli_main._required_secret_names(config)


def configured(tmp_path: Path) -> Path:
    config_file = tmp_path / "config.json"
    result = runner.invoke(
        app,
        [
            "setup",
            "--config",
            str(config_file),
            "--data-dir",
            str(tmp_path / "data"),
            "--root",
            str(tmp_path),
        ],
    )
    assert result.exit_code == 0
    return config_file


def test_fanout_wait_renders_replay(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    config_file = configured(tmp_path)
    fake = FakeClient(
        [
            {"id": "run-1", "status": "created"},
            {"id": "run-1", "status": "running"},
            {"id": "run-1", "status": "completed"},
            {"final_output": "report"},
        ]
    )
    monkeypatch.setattr(State, "client", lambda self: fake)
    monkeypatch.setattr(cli_main.time, "sleep", lambda _seconds: None)
    result = runner.invoke(
        app,
        [
            "--config",
            str(config_file),
            "run",
            "question",
            "--mode",
            "fan-out",
            "--worker",
            "fake:analyst",
            "--verifier",
            "fake",
            "--synthesizer",
            "fake",
            "--call-limit",
            "10",
            "--wait",
            "--json",
        ],
    )
    assert result.exit_code == 0, result.output
    assert json.loads(result.output)["replay"]["final_output"] == "report"
    assert fake.requests[0][1] == "/v1/runs/fanout"


def test_memory_cron_and_channel_subcommands(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    config_file = configured(tmp_path)
    fake = FakeClient(
        [
            {"id": "memory-1"},
            [{"id": "memory-1"}],
            {"id": "memory-1", "revision": 2},
            {"id": "job-1"},
            [{"id": "job-1"}],
            {"telegram_enabled": False},
            [],
        ]
    )
    monkeypatch.setattr(State, "client", lambda self: fake)

    commands = [
        ["memory", "add", "remember this", "--json"],
        ["memory", "list", "--json"],
        ["memory", "revise", "memory-1", "revised", "--revision", "1", "--json"],
        [
            "cron",
            "once",
            "one",
            "2027-01-01T00:00:00Z",
            "say hello",
            "--json",
        ],
        ["cron", "list", "--json"],
        ["channels", "status", "--json"],
        ["channels", "unknown", "--json"],
    ]
    for command in commands:
        result = runner.invoke(app, ["--config", str(config_file), *command])
        assert result.exit_code == 0, result.output

    paths = [request[1] for request in fake.requests]
    assert paths[0] == "/v1/memory"
    assert paths[1].startswith("/v1/memory?")
    assert paths[2] == "/v1/memory/memory-1"
    assert fake.requests[2][2] == {
        "profile_id": "default",
        "subject_key": "local",
        "content": "revised",
        "expected_revision": 1,
    }
    assert paths[3] == "/v1/jobs"
    assert paths[4] == "/v1/jobs"
    assert paths[5] == "/v1/channels/status"
    assert paths[6].startswith("/v1/channels/outbox/unknown")


def test_listing_and_simple_commands(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    config_file = configured(tmp_path)
    fake = FakeClient(
        [
            [{"id": "run-1", "mode": "single", "status": "created", "created_at": "now"}],
            {"id": "run-1"},
            {"decision": "approved"},
            {"decision": "rejected"},
            {"id": "run-1", "status": "running"},
            {"final_output": "done"},
            {"fake": ["model"]},
            {"tools": ["read_file"]},
        ]
    )
    monkeypatch.setattr(State, "client", lambda self: fake)
    commands = [
        ["runs"],
        ["show", "run-1", "--json"],
        ["approve", "approval-1", "--reason", "safe", "--json"],
        ["deny", "approval-2", "--json"],
        ["resume", "run-1", "--json"],
        ["replay", "run-1", "--json"],
        ["models", "--json"],
        ["tools", "--json"],
    ]
    for command in commands:
        result = runner.invoke(app, ["--config", str(config_file), *command])
        assert result.exit_code == 0, result.output
    assert fake.requests[2][2] == {
        "decision": "approved",
        "reason": "safe",
        "decided_by": "cli",
    }


def test_expected_cli_failures(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    config_file = configured(tmp_path)
    unhealthy = FakeClient([{"fake": {"ok": False}}])
    monkeypatch.setattr(State, "client", lambda self: unhealthy)
    assert (
        runner.invoke(app, ["--config", str(config_file), "doctor"]).exit_code == 1
    )

    missing_options = runner.invoke(
        app,
        ["--config", str(config_file), "run", "question", "--mode", "fan-out"],
    )
    assert missing_options.exit_code != 0

    failed = FakeClient([{"id": "run-2", "status": "failed"}])
    monkeypatch.setattr(State, "client", lambda self: failed)
    assert (
        runner.invoke(
            app,
            ["--config", str(config_file), "run", "question", "--provider", "fake"],
        ).exit_code
        == 1
    )


def test_daemon_status_success_and_failure(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    config_file = configured(tmp_path)
    monkeypatch.setattr(cli_main, "http_health", lambda _url: {"status": "ok"})
    result = runner.invoke(
        app, ["--config", str(config_file), "daemon", "status", "--json"]
    )
    assert result.exit_code == 0

    def unavailable(_url: str) -> object:
        raise DaemonClientError("offline")

    monkeypatch.setattr(cli_main, "http_health", unavailable)
    result = runner.invoke(app, ["--config", str(config_file), "daemon", "status"])
    assert result.exit_code == 1


def test_worker_parser_and_emit_helpers() -> None:
    assert cli_main._worker("provider:role", 2)["id"] == "worker-2"
    with pytest.raises(typer.BadParameter):
        cli_main._worker("invalid", 1)


def test_foundry_router_mode_uses_dedicated_endpoint(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    config_file = configured(tmp_path)
    fake = FakeClient([{"id": "run-router", "status": "created"}])
    monkeypatch.setattr(State, "client", lambda self: fake)
    result = runner.invoke(
        app,
        [
            "--config",
            str(config_file),
            "run",
            "question",
            "--mode",
            "foundry-router",
            "--provider",
            "router",
            "--call-limit",
            "3",
            "--json",
        ],
    )
    assert result.exit_code == 0, result.output
    assert fake.requests[0][1] == "/v1/runs/foundry-router"
