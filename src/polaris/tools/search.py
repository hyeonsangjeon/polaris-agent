"""SearXNG JSON search adapter."""

from __future__ import annotations

import json
import math
from collections.abc import Mapping
from urllib.parse import urlsplit

import httpx

from .registry import JsonValue, SafetyClass, ToolArguments, ToolEntry, ToolResult


class SearchToolError(RuntimeError):
    """A configured search request failed or returned invalid data."""


class SearXNGSearchTool:
    """Normalize results from one explicitly configured SearXNG endpoint."""

    def __init__(
        self,
        endpoint: str,
        *,
        timeout: float = 20.0,
        max_response_bytes: int = 2_000_000,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        try:
            parsed = urlsplit(endpoint)
        except ValueError as exc:
            raise ValueError("SearXNG endpoint is invalid") from exc
        if parsed.scheme not in {"http", "https"} or not parsed.hostname:
            raise ValueError("SearXNG endpoint must be an HTTP or HTTPS URL")
        if parsed.username is not None or parsed.password is not None:
            raise ValueError("SearXNG endpoint may not contain credentials")
        if not math.isfinite(timeout) or timeout <= 0 or max_response_bytes <= 0:
            raise ValueError("search limits must be positive")
        self.endpoint = endpoint
        self.timeout = float(timeout)
        self.max_response_bytes = max_response_bytes
        self.transport = transport

    @staticmethod
    def _arguments(arguments: ToolArguments) -> tuple[str, int, list[str] | None]:
        query = arguments.get("query")
        if not isinstance(query, str) or not query.strip() or "\x00" in query:
            raise SearchToolError("query must be a non-empty string without NUL")
        raw_limit = arguments.get("limit", 10)
        if (
            isinstance(raw_limit, bool)
            or not isinstance(raw_limit, int)
            or not 1 <= raw_limit <= 20
        ):
            raise SearchToolError("limit must be an integer from 1 through 20")
        raw_categories = arguments.get("categories")
        if raw_categories is None:
            categories = None
        elif not isinstance(raw_categories, list) or not raw_categories:
            raise SearchToolError("categories must be a non-empty array of strings")
        else:
            categories = []
            for category in raw_categories:
                if not isinstance(category, str) or not category.strip():
                    raise SearchToolError("categories must be a non-empty array of strings")
                categories.append(category.strip())
        return query.strip(), raw_limit, categories

    async def search(self, arguments: ToolArguments) -> ToolResult:
        query, limit, categories = self._arguments(arguments)
        parameters: dict[str, str] = {"q": query, "format": "json"}
        if categories is not None:
            parameters["categories"] = ",".join(categories)
        body = bytearray()
        try:
            async with (
                httpx.AsyncClient(
                    transport=self.transport,
                    timeout=self.timeout,
                    follow_redirects=True,
                    trust_env=False,
                ) as client,
                client.stream("GET", self.endpoint, params=parameters) as response,
            ):
                if response.status_code < 200 or response.status_code >= 300:
                    raise SearchToolError("SearXNG returned a non-success status")
                async for chunk in response.aiter_bytes():
                    if len(body) + len(chunk) > self.max_response_bytes:
                        raise SearchToolError("SearXNG response exceeds the configured byte limit")
                    body.extend(chunk)
        except SearchToolError:
            raise
        except httpx.TimeoutException as exc:
            raise SearchToolError("SearXNG request timed out") from exc
        except httpx.HTTPError as exc:
            raise SearchToolError("SearXNG request failed") from exc
        try:
            payload = json.loads(body)
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise SearchToolError("SearXNG returned invalid JSON") from exc
        if not isinstance(payload, Mapping):
            raise SearchToolError("SearXNG response must be a JSON object")
        raw_results = payload.get("results")
        if not isinstance(raw_results, list):
            raise SearchToolError("SearXNG response has no results array")
        normalized: list[JsonValue] = []
        for item in raw_results[:limit]:
            if not isinstance(item, Mapping):
                raise SearchToolError("SearXNG result must be an object")
            title = item.get("title")
            url = item.get("url")
            snippet = item.get("content", item.get("snippet", ""))
            if (
                not isinstance(title, str)
                or not isinstance(url, str)
                or not isinstance(snippet, str)
            ):
                raise SearchToolError("SearXNG result fields must be strings")
            normalized.append({"title": title, "url": url, "snippet": snippet})
        return {"query": query, "results": normalized, "count": len(normalized)}

    def entry(self) -> ToolEntry:
        return ToolEntry(
            name="search",
            toolset="search",
            schema={
                "parameters": {
                    "type": "object",
                    "properties": {
                        "query": {"type": "string", "minLength": 1},
                        "limit": {"type": "integer", "minimum": 1, "maximum": 20},
                        "categories": {
                            "type": "array",
                            "items": {"type": "string", "minLength": 1},
                            "minItems": 1,
                        },
                    },
                    "required": ["query"],
                    "additionalProperties": False,
                }
            },
            handler=self.search,
            description="Search the configured SearXNG instance.",
            safety_class=SafetyClass.READ_ONLY,
        )


def create_search_entry(
    endpoint: str,
    *,
    timeout: float = 20.0,
    max_response_bytes: int = 2_000_000,
    transport: httpx.AsyncBaseTransport | None = None,
) -> ToolEntry:
    """Create a SearXNG search registry entry."""

    return SearXNGSearchTool(
        endpoint,
        timeout=timeout,
        max_response_bytes=max_response_bytes,
        transport=transport,
    ).entry()
