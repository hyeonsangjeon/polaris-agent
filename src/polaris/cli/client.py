"""Small synchronous client for the local daemon."""

from __future__ import annotations

from collections.abc import Mapping
from pathlib import Path
from typing import Any

import httpx


class DaemonClientError(RuntimeError):
    def __init__(self, message: str, *, status_code: int | None = None) -> None:
        super().__init__(message)
        self.status_code = status_code


class DaemonClient:
    def __init__(
        self,
        base_url: str,
        token: str,
        *,
        transport: httpx.BaseTransport | None = None,
        timeout: float = 30.0,
    ) -> None:
        if not token:
            raise DaemonClientError("API token is missing")
        self._client = httpx.Client(
            base_url=base_url.rstrip("/"),
            headers={"Authorization": f"Bearer {token}"},
            transport=transport,
            timeout=timeout,
            trust_env=False,
        )

    def request(
        self,
        method: str,
        path: str,
        *,
        json: Mapping[str, Any] | None = None,
        params: Mapping[str, Any] | None = None,
    ) -> Any:
        try:
            response = self._client.request(method, path, json=json, params=params)
        except httpx.HTTPError as exc:
            raise DaemonClientError(f"daemon request failed: {exc}") from exc
        if response.is_error:
            try:
                payload = response.json()
                detail = payload.get("detail") or payload.get("error")
            except ValueError:
                detail = response.text
            raise DaemonClientError(
                str(detail or f"HTTP {response.status_code}"),
                status_code=response.status_code,
            )
        if response.status_code == 204:
            return None
        return response.json()

    def close(self) -> None:
        self._client.close()

    def __enter__(self) -> DaemonClient:
        return self

    def __exit__(self, *_args: object) -> None:
        self.close()


def read_token(path: Path) -> str:
    try:
        token = path.expanduser().read_text(encoding="utf-8").strip()
    except FileNotFoundError as exc:
        raise DaemonClientError("API token file not found; run `polaris setup`") from exc
    if not token:
        raise DaemonClientError("API token file is empty; run `polaris setup`")
    return token
