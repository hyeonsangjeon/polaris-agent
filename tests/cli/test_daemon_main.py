from __future__ import annotations

from importlib import import_module
from pathlib import Path

import pytest
import typer

from polaris.config import AppConfig, DaemonConfig, ToolConfig, save_config
from polaris.daemon.main import is_loopback_host, resolve_api_token, serve, validate_bind

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
    config = AppConfig(
        data_dir=tmp_path,
        tools=ToolConfig(roots=(tmp_path,)),
        daemon=DaemonConfig(token_file=token_file),
    )
    save_config(config, config_file)
    captured: dict[str, object] = {}
    service = object()
    monkeypatch.setattr(daemon_main, "AgentService", lambda _config: service)
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
