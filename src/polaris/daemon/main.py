"""Daemon command entry point."""
# ruff: noqa: B008

from __future__ import annotations

import ipaddress
import os
import stat
from pathlib import Path

import typer
import uvicorn

from ..config import AppConfig, load_config, secret_from_env
from ..secrets import SecretsFileError, runtime_environment
from ..service import AgentService
from .app import create_app

app = typer.Typer(add_completion=False, help="Run the Polaris local HTTP daemon.")
_MAX_API_TOKEN_BYTES = 8 * 1024


def is_loopback_host(host: str) -> bool:
    normalized = host.strip().lower().rstrip(".")
    if normalized == "localhost":
        return True
    try:
        return ipaddress.ip_address(normalized).is_loopback
    except ValueError:
        return False


def resolve_api_token(config: AppConfig, env: dict[str, str] | None = None) -> str | None:
    from_environment = secret_from_env(config.daemon.api_token_env, env)
    if from_environment:
        return from_environment
    token_file = config.paths.token_file
    try:
        token = _read_private_api_token(token_file)
    except FileNotFoundError:
        return None
    return token or None


def _read_private_api_token(path: Path) -> str:
    before = path.lstat()
    _validate_token_metadata(before)
    descriptor = os.open(path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0))
    try:
        current = os.fstat(descriptor)
        _validate_token_metadata(current)
        if (before.st_dev, before.st_ino) != (current.st_dev, current.st_ino):
            raise ValueError("API token file changed while it was being opened")
        payload = os.read(descriptor, _MAX_API_TOKEN_BYTES + 1)
        if len(payload) > _MAX_API_TOKEN_BYTES:
            raise ValueError("API token file exceeds 8 KiB")
    finally:
        os.close(descriptor)
    try:
        token = payload.decode("utf-8").strip()
    except UnicodeDecodeError as exc:
        raise ValueError("API token file is not valid UTF-8") from exc
    if "\n" in token or "\r" in token or "\x00" in token:
        raise ValueError("API token file must contain one NUL-free line")
    return token


def _validate_token_metadata(metadata: os.stat_result) -> None:
    if not stat.S_ISREG(metadata.st_mode):
        raise ValueError("API token file must be a regular file, not a symlink")
    getuid = getattr(os, "geteuid", None)
    if getuid is not None and metadata.st_uid != getuid():
        raise ValueError("API token file must be owned by the current user")
    mode = stat.S_IMODE(metadata.st_mode)
    if mode & 0o077:
        raise ValueError(f"API token file must not grant group/other access ({mode:04o})")


def validate_bind(host: str, *, allow_remote: bool, api_token: str | None) -> None:
    if not is_loopback_host(host) and (not allow_remote or not api_token):
        raise ValueError(
            "non-loopback bind requires --allow-remote and a configured API token"
        )


@app.command()
def serve(
    config_file: Path | None = typer.Option(None, "--config", exists=True, dir_okay=False),
    host: str | None = typer.Option(None, "--host"),
    port: int | None = typer.Option(None, "--port", min=1, max=65535),
    allow_remote: bool = typer.Option(False, "--allow-remote"),
) -> None:
    config = load_config(config_file)
    secrets_file = config.resolve_secrets_file(os.environ.get("POLARIS_SECRETS_FILE"))
    try:
        env = runtime_environment(secrets_file)
    except SecretsFileError as exc:
        typer.echo(f"Error: {exc}", err=True)
        raise typer.Exit(2) from exc
    bind_host = host or config.daemon.host
    bind_port = port or config.daemon.port
    try:
        token = resolve_api_token(config, env)
    except (OSError, ValueError) as exc:
        typer.echo(f"Error: {exc}", err=True)
        raise typer.Exit(2) from exc
    try:
        validate_bind(bind_host, allow_remote=allow_remote, api_token=token)
    except ValueError as exc:
        typer.echo(f"Error: {exc}", err=True)
        raise typer.Exit(2) from exc
    if token is None:
        typer.echo("Error: no API token configured; run `polaris setup`", err=True)
        raise typer.Exit(2)
    config.paths.ensure()
    service = AgentService(config, env=env, api_token=token)
    uvicorn.run(
        create_app(service, token),
        host=bind_host,
        port=bind_port,
        timeout_graceful_shutdown=int(config.daemon.graceful_timeout_seconds),
    )


def main() -> None:
    app()


if __name__ == "__main__":
    main()
