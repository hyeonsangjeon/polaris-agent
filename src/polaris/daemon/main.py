"""Daemon command entry point."""
# ruff: noqa: B008

from __future__ import annotations

import ipaddress
from pathlib import Path

import typer
import uvicorn

from ..config import AppConfig, load_config, secret_from_env
from ..service import AgentService
from .app import create_app

app = typer.Typer(add_completion=False, help="Run the Polaris local HTTP daemon.")


def is_loopback_host(host: str) -> bool:
    normalized = host.strip().lower().rstrip(".")
    if normalized == "localhost":
        return True
    try:
        return ipaddress.ip_address(normalized).is_loopback
    except ValueError:
        return False


def resolve_api_token(config: AppConfig) -> str | None:
    from_environment = secret_from_env(config.daemon.api_token_env)
    if from_environment:
        return from_environment
    token_file = config.daemon.token_file or config.data_dir / "api-token"
    try:
        token = token_file.expanduser().read_text(encoding="utf-8").strip()
    except FileNotFoundError:
        return None
    return token or None


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
    bind_host = host or config.daemon.host
    bind_port = port or config.daemon.port
    token = resolve_api_token(config)
    try:
        validate_bind(bind_host, allow_remote=allow_remote, api_token=token)
    except ValueError as exc:
        typer.echo(f"Error: {exc}", err=True)
        raise typer.Exit(2) from exc
    if token is None:
        typer.echo("Error: no API token configured; run `polaris setup`", err=True)
        raise typer.Exit(2)
    config.paths.ensure()
    service = AgentService(config)
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
