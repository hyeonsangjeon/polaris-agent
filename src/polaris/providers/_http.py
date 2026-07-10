"""Shared HTTP transport helpers for providers."""

from __future__ import annotations

from typing import NoReturn

import httpx

from .base import (
    ProviderAuthenticationError,
    ProviderProtocolError,
    ProviderRateLimitError,
    ProviderTransportError,
)


def endpoint(base_url: str, path: str) -> str:
    return f"{base_url.rstrip('/')}/{path.lstrip('/')}"


def map_http_status(response: httpx.Response, operation: str) -> None:
    if response.is_success:
        return
    status = response.status_code
    message = f"{operation} failed with HTTP {status}"
    if status in {401, 403}:
        raise ProviderAuthenticationError(message)
    if status == 429:
        raise ProviderRateLimitError(message)
    raise ProviderProtocolError(message)


def map_transport_error(exc: httpx.HTTPError, operation: str) -> NoReturn:
    raise ProviderTransportError(f"{operation} transport failed: {type(exc).__name__}") from exc
