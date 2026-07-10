"""Bounded, GET-only HTTP fetch adapter."""

from __future__ import annotations

import asyncio
import base64
import ipaddress
import math
import socket
from collections.abc import Sequence
from urllib.parse import urljoin, urlsplit

import httpx

from .registry import SafetyClass, ToolArguments, ToolEntry, ToolResult


class HTTPFetchError(RuntimeError):
    """An HTTP fetch could not be completed safely."""


class HTTPSSRFError(HTTPFetchError):
    """An HTTP target is disallowed by the SSRF policy."""


class HTTPResponseTooLargeError(HTTPFetchError):
    """An HTTP response exceeded the configured byte limit."""


def _blocked_address(address: ipaddress.IPv4Address | ipaddress.IPv6Address) -> bool:
    return (
        address.is_private
        or address.is_loopback
        or address.is_link_local
        or address.is_multicast
        or address.is_reserved
        or address.is_unspecified
    )


class HTTPFetchTool:
    """Fetch public HTTP resources without exposing a write-capable method."""

    def __init__(
        self,
        *,
        allow_private: bool = False,
        timeout: float = 20.0,
        max_response_bytes: int = 2_000_000,
        max_redirects: int = 5,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        if (
            not math.isfinite(timeout)
            or timeout <= 0
            or max_response_bytes <= 0
            or max_redirects < 0
        ):
            raise ValueError("HTTP limits must be positive")
        self.allow_private = allow_private
        self.timeout = float(timeout)
        self.max_response_bytes = max_response_bytes
        self.max_redirects = max_redirects
        self.transport = transport

    async def _validate_url(self, url: str) -> httpx.URL:
        try:
            parsed = urlsplit(url)
        except ValueError as exc:
            raise HTTPFetchError("URL is invalid") from exc
        if parsed.scheme not in {"http", "https"} or not parsed.hostname:
            raise HTTPSSRFError("only HTTP and HTTPS URLs are allowed")
        if parsed.username is not None or parsed.password is not None:
            raise HTTPSSRFError("URL credentials are not allowed")
        if self.allow_private:
            return httpx.URL(url)
        hostname = parsed.hostname.rstrip(".").lower()
        if hostname == "localhost" or hostname.endswith(".localhost"):
            raise HTTPSSRFError("private HTTP targets are disabled")
        try:
            literal = ipaddress.ip_address(hostname)
        except ValueError:
            literal = None
        if literal is not None:
            if _blocked_address(literal):
                raise HTTPSSRFError("private HTTP targets are disabled")
            return httpx.URL(url)
        try:
            loop = asyncio.get_running_loop()
            records: Sequence[
                tuple[int, int, int, str, tuple[object, ...]]
            ] = await loop.run_in_executor(
                None,
                lambda: socket.getaddrinfo(
                    hostname,
                    parsed.port or (443 if parsed.scheme == "https" else 80),
                    type=socket.SOCK_STREAM,
                ),
            )
        except (OSError, UnicodeError, ValueError) as exc:
            raise HTTPFetchError("HTTP target could not be resolved") from exc
        if not records:
            raise HTTPFetchError("HTTP target could not be resolved")
        for record in records:
            try:
                address = ipaddress.ip_address(str(record[4][0]))
            except ValueError as exc:
                raise HTTPFetchError("HTTP target resolved to an invalid address") from exc
            if _blocked_address(address):
                raise HTTPSSRFError("private HTTP targets are disabled")
        return httpx.URL(url)

    async def _fetch_url(self, raw_url: str, timeout: float) -> ToolResult:
        current = await self._validate_url(raw_url)
        redirects = 0
        async with httpx.AsyncClient(
            transport=self.transport,
            timeout=timeout,
            follow_redirects=False,
            trust_env=False,
        ) as client:
            while True:
                async with client.stream("GET", current) as response:
                    if response.is_redirect:
                        location = response.headers.get("location")
                        if location is None:
                            raise HTTPFetchError("redirect response has no location")
                        if redirects >= self.max_redirects:
                            raise HTTPFetchError("HTTP redirect limit exceeded")
                        current = await self._validate_url(urljoin(str(response.url), location))
                        redirects += 1
                        continue
                    body = bytearray()
                    async for chunk in response.aiter_bytes():
                        if len(body) + len(chunk) > self.max_response_bytes:
                            raise HTTPResponseTooLargeError(
                                "HTTP response exceeds the configured byte limit"
                            )
                        body.extend(chunk)
                    content_type = response.headers.get("content-type", "")
                    media_type = content_type.split(";", 1)[0].strip().lower()
                    textual = (
                        media_type.startswith("text/")
                        or media_type.endswith("+json")
                        or media_type.endswith("+xml")
                        or media_type
                        in {
                            "application/json",
                            "application/javascript",
                            "application/xml",
                        }
                    )
                    if textual:
                        encoding = response.encoding or "utf-8"
                        try:
                            content = bytes(body).decode(encoding, errors="replace")
                        except LookupError as exc:
                            raise HTTPFetchError(
                                "HTTP response declared an unknown encoding"
                            ) from exc
                        content_encoding = encoding
                    else:
                        content = base64.b64encode(body).decode("ascii")
                        content_encoding = "base64"
                    return {
                        "status": response.status_code,
                        "final_url": str(response.url),
                        "content_type": content_type,
                        "content": content,
                        "encoding": content_encoding,
                        "size": len(body),
                        "redirects": redirects,
                    }

    async def fetch(self, arguments: ToolArguments) -> ToolResult:
        raw_url = arguments.get("url")
        if not isinstance(raw_url, str) or not raw_url or "\x00" in raw_url:
            raise HTTPFetchError("url must be a non-empty string without NUL")
        timeout_value = arguments.get("timeout")
        if timeout_value is None:
            timeout = self.timeout
        elif (
            isinstance(timeout_value, bool)
            or not isinstance(timeout_value, (int, float))
            or not math.isfinite(timeout_value)
            or timeout_value <= 0
            or timeout_value > self.timeout
        ):
            raise HTTPFetchError("timeout is outside the configured limit")
        else:
            timeout = float(timeout_value)
        try:
            return await asyncio.wait_for(self._fetch_url(raw_url, timeout), timeout)
        except HTTPFetchError:
            raise
        except (TimeoutError, httpx.TimeoutException) as exc:
            raise HTTPFetchError("HTTP request timed out") from exc
        except httpx.HTTPError as exc:
            raise HTTPFetchError("HTTP request failed") from exc

    def entry(self) -> ToolEntry:
        return ToolEntry(
            name="http_fetch",
            toolset="http",
            schema={
                "parameters": {
                    "type": "object",
                    "properties": {
                        "url": {"type": "string", "format": "uri"},
                        "timeout": {"type": "number", "exclusiveMinimum": 0},
                    },
                    "required": ["url"],
                    "additionalProperties": False,
                }
            },
            handler=self.fetch,
            description="Fetch an HTTP or HTTPS URL with GET.",
            safety_class=SafetyClass.READ_ONLY,
        )


def create_http_fetch_entry(
    *,
    allow_private: bool = False,
    timeout: float = 20.0,
    max_response_bytes: int = 2_000_000,
    max_redirects: int = 5,
    transport: httpx.AsyncBaseTransport | None = None,
) -> ToolEntry:
    """Create a GET-only HTTP registry entry."""

    return HTTPFetchTool(
        allow_private=allow_private,
        timeout=timeout,
        max_response_bytes=max_response_bytes,
        max_redirects=max_redirects,
        transport=transport,
    ).entry()
