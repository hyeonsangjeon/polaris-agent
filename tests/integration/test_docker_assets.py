from pathlib import Path

ROOT = Path(__file__).parents[2]


def test_dockerfile_is_locked_non_root_signal_safe_and_healthy() -> None:
    dockerfile = (ROOT / "Dockerfile").read_text()
    assert "FROM python:3.12-slim AS builder" in dockerfile
    assert "FROM python:3.12-slim AS runtime" in dockerfile
    assert "uv sync --locked --no-dev" in dockerfile
    assert "USER polaris:polaris" in dockerfile
    assert 'VOLUME ["/data", "/workspace", "/exports"]' in dockerfile
    assert "HEALTHCHECK" in dockerfile
    assert 'ENTRYPOINT ["/app/.venv/bin/polarisd"]' in dockerfile
    assert "api-token" not in dockerfile


def test_compose_uses_local_state_loopback_and_container_hardening() -> None:
    compose = (ROOT / "compose.yaml").read_text()
    assert "127.0.0.1" in compose
    assert "POLARIS_DATA_PATH" in compose
    assert ":/data" in compose
    assert "restart: unless-stopped" in compose
    assert "read_only: true" in compose
    assert "no-new-privileges:true" in compose
    assert "tmpfs:" in compose
    assert "healthcheck:" in compose
    assert "POLARIS_HOME: /data" in compose
    assert "api-token:" not in compose


def test_nas_warning_and_external_ollama_example_are_documented() -> None:
    documentation = (ROOT / "deploy" / "docker" / "README.md").read_text()
    assert "Never put the\nSQLite database on SMB, NFS" in documentation
    assert "exported artifacts may be written" in documentation
    assert "No model or Ollama server is bundled" in documentation
    assert (ROOT / "deploy" / "docker" / "compose.external-ollama.yaml").is_file()
