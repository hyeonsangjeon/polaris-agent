from __future__ import annotations

import os
from importlib import import_module
from pathlib import Path

import pytest
import typer

from polaris.config import (
    AppConfig,
    ChannelsConfig,
    DaemonConfig,
    TelegramConfig,
    ToolConfig,
    save_config,
)
from polaris.daemon.main import is_loopback_host, resolve_api_token, serve, validate_bind
from polaris.secrets import SecretsFile

daemon_main = import_module("polaris.daemon.main")


def test_loopback_bind_is_allowed_without_remote_flag() -> None:
    assert is_loopback_host("127.0.0.1")
    assert is_loopback_host("::1")
    validate_bind("127.0.0.1", allow_remote=False, api_token="token")


def test_non_loopback_requires_flag_and_token() -> None:
    with pytest.raises(ValueError, match="non-loopback"):
        validate_bind("0.0.0.0", allow_remote=False, api_token="token")
    with pytest.raises(ValueError, match="non-loopback"):
        validate_bind("0.0.0.0", allow_remote=True, api_token=None)
    validate_bind("0.0.0.0", allow_remote=True, api_token="token")


def test_resolve_token_prefers_environment(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    token_file = tmp_path / "token"
    token_file.write_text("file-token\n")
    token_file.chmod(0o600)
    config = AppConfig(
        data_dir=tmp_path,
        tools=ToolConfig(roots=(tmp_path,)),
        daemon=DaemonConfig(api_token_env="POLARIS_TEST_TOKEN", token_file=token_file),
    )
    monkeypatch.setenv("POLARIS_TEST_TOKEN", "environment-token")
    assert resolve_api_token(config) == "environment-token"
    monkeypatch.delenv("POLARIS_TEST_TOKEN")
    assert resolve_api_token(config) == "file-token"


def test_resolve_missing_token_and_serve(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    config_file = tmp_path / "config.json"
    token_file = tmp_path / "token"
    token_file.write_text("token\n")
    token_file.chmod(0o600)
    config = AppConfig(
        data_dir=tmp_path,
        tools=ToolConfig(roots=(tmp_path,)),
        daemon=DaemonConfig(token_file=token_file),
    )
    save_config(config, config_file)
    captured: dict[str, object] = {}
    service = object()
    monkeypatch.setattr(
        daemon_main,
        "AgentService",
        lambda _config, *, env, api_token: service,
    )
    monkeypatch.setattr(daemon_main, "create_app", lambda value, token: (value, token))

    def uvicorn_run(application: object, **kwargs: object) -> None:
        captured["app"] = application
        captured.update(kwargs)

    monkeypatch.setattr(daemon_main.uvicorn, "run", uvicorn_run)
    serve(config_file, None, None, False)
    assert captured["host"] == "127.0.0.1"
    assert captured["app"] == (service, "token")

    token_file.unlink()
    assert resolve_api_token(config) is None
    with pytest.raises(typer.Exit):
        serve(config_file, None, None, False)


def test_serve_loads_file_api_token_and_passes_channel_mapping(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    config_file = tmp_path / "config.json"
    secrets_file = tmp_path / "runtime-secrets.env"
    SecretsFile(secrets_file).set("DAEMON_TOKEN", "api-from-secrets")
    SecretsFile(secrets_file).set("TELEGRAM_TOKEN", "telegram-from-secrets")
    config = AppConfig(
        data_dir=tmp_path,
        tools=ToolConfig(roots=(tmp_path,)),
        daemon=DaemonConfig(
            api_token_env="DAEMON_TOKEN",
            token_file=tmp_path / "missing-token",
            secrets_file=secrets_file,
        ),
        channels=ChannelsConfig(
            telegram=TelegramConfig(
                enabled=True,
                token_env="TELEGRAM_TOKEN",
                allowed_user_ids=("1",),
                allowed_chat_ids=("2",),
            ),
        ),
    )
    save_config(config, config_file)
    captured: dict[str, object] = {}

    def agent_service(
        _config: AppConfig, *, env: dict[str, str], api_token: str
    ) -> object:
        captured["env"] = env
        captured["api_token"] = api_token
        return object()

    monkeypatch.setattr(daemon_main, "AgentService", agent_service)
    monkeypatch.setattr(daemon_main, "create_app", lambda service, token: (service, token))
    monkeypatch.setattr(
        daemon_main.uvicorn,
        "run",
        lambda application, **_kwargs: captured.update(application=application),
    )
    serve(config_file, None, None, False)

    env = captured["env"]
    assert isinstance(env, dict)
    assert env["TELEGRAM_TOKEN"] == "telegram-from-secrets"
    assert captured["application"][1] == "api-from-secrets"  # type: ignore[index]
    assert captured["api_token"] == "api-from-secrets"


def test_relative_secrets_override_resolves_under_data_dir(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    config_file = tmp_path / "config.json"
    data_dir = tmp_path / "data"
    secrets_file = data_dir / "private" / "runtime.env"
    SecretsFile(secrets_file).set("DAEMON_TOKEN", "runtime-token")
    config = AppConfig(
        data_dir=data_dir,
        tools=ToolConfig(roots=(tmp_path,)),
        daemon=DaemonConfig(api_token_env="DAEMON_TOKEN"),
    )
    save_config(config, config_file)
    captured: dict[str, object] = {}
    monkeypatch.setenv("POLARIS_SECRETS_FILE", "private/runtime.env")
    monkeypatch.setattr(
        daemon_main,
        "AgentService",
        lambda _config, *, env, api_token: captured.update(
            env=env, api_token=api_token
        ),
    )
    monkeypatch.setattr(daemon_main, "create_app", lambda service, token: (service, token))
    monkeypatch.setattr(daemon_main.uvicorn, "run", lambda *_args, **_kwargs: None)

    serve(config_file, None, None, False)

    assert captured["api_token"] == "runtime-token"
    assert captured["env"] == {"DAEMON_TOKEN": "runtime-token", **dict(os.environ)}


def test_token_file_must_be_private_and_bounded(tmp_path: Path) -> None:
    token_file = tmp_path / "token"
    token_file.write_text("token")
    config = AppConfig(
        data_dir=tmp_path,
        tools=ToolConfig(roots=(tmp_path,)),
        daemon=DaemonConfig(token_file=token_file),
    )

    with pytest.raises(ValueError, match="group/other"):
        resolve_api_token(config, {})

    token_file.chmod(0o600)
    token_file.write_bytes(b"x" * (8 * 1024 + 1))
    with pytest.raises(ValueError, match="8 KiB"):
        resolve_api_token(config, {})
