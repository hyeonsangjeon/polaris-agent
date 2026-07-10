from __future__ import annotations

from pathlib import Path

import httpx
import pytest

from polaris.cli.client import DaemonClient, DaemonClientError, read_token


def test_client_success_errors_and_context_manager(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("HTTP_PROXY", "http://proxy.invalid:8080")
    monkeypatch.setenv("HTTPS_PROXY", "http://proxy.invalid:8080")

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/ok":
            assert request.headers["authorization"] == "Bearer token"
            return httpx.Response(200, json={"ok": True})
        if request.url.path == "/empty":
            return httpx.Response(204)
        if request.url.path == "/text-error":
            return httpx.Response(500, text="broken")
        return httpx.Response(409, json={"detail": "conflict"})

    with DaemonClient(
        "http://daemon", "token", transport=httpx.MockTransport(handler)
    ) as client:
        assert client._client.trust_env is False
        assert client.request("GET", "/ok") == {"ok": True}
        assert client.request("DELETE", "/empty") is None
        with pytest.raises(DaemonClientError, match="conflict") as conflict:
            client.request("GET", "/conflict")
        assert conflict.value.status_code == 409
        with pytest.raises(DaemonClientError, match="broken"):
            client.request("GET", "/text-error")


def test_read_token_errors_and_success(tmp_path: Path) -> None:
    with pytest.raises(DaemonClientError, match="not found"):
        read_token(tmp_path / "missing")
    token = tmp_path / "token"
    token.write_text("")
    with pytest.raises(DaemonClientError, match="empty"):
        read_token(token)
    token.write_text("value\n")
    assert read_token(token) == "value"
    with pytest.raises(DaemonClientError, match="missing"):
        DaemonClient("http://daemon", "")
