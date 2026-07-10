from __future__ import annotations

import asyncio
import json
from pathlib import Path

import httpx
import pytest

from polaris.tools import (
    HTTPFetchError,
    HTTPFetchTool,
    HTTPResponseTooLargeError,
    HTTPSSRFError,
    SafetyClass,
    SearchToolError,
    SearXNGSearchTool,
    build_default_registry,
)


@pytest.mark.asyncio
async def test_http_get_status_content_type_and_redirect() -> None:
    methods: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        methods.append(request.method)
        if request.url.path == "/start":
            return httpx.Response(302, headers={"location": "/final"})
        return httpx.Response(
            418,
            content="short",
            headers={"content-type": "text/plain; charset=utf-8"},
        )

    tool = HTTPFetchTool(allow_private=True, transport=httpx.MockTransport(handler))
    result = await tool.fetch({"url": "http://127.0.0.1/start"})
    assert result == {
        "status": 418,
        "final_url": "http://127.0.0.1/final",
        "content_type": "text/plain; charset=utf-8",
        "content": "short",
        "encoding": "utf-8",
        "size": 5,
        "redirects": 1,
    }
    assert methods == ["GET", "GET"]


@pytest.mark.asyncio
async def test_http_size_and_ssrf_guards() -> None:
    called = False

    def handler(_: httpx.Request) -> httpx.Response:
        nonlocal called
        called = True
        return httpx.Response(200, content=b"12345", headers={"content-type": "text/plain"})

    blocked = HTTPFetchTool(transport=httpx.MockTransport(handler))
    for url in ("file:///etc/passwd", "http://127.0.0.1/", "http://169.254.1.1/"):
        with pytest.raises(HTTPSSRFError):
            await blocked.fetch({"url": url})
    assert not called

    bounded = HTTPFetchTool(
        allow_private=True,
        max_response_bytes=4,
        transport=httpx.MockTransport(handler),
    )
    with pytest.raises(HTTPResponseTooLargeError):
        await bounded.fetch({"url": "http://127.0.0.1/"})

    async def slow(_: httpx.Request) -> httpx.Response:
        await asyncio.sleep(1)
        return httpx.Response(200)

    timed = HTTPFetchTool(
        allow_private=True,
        timeout=0.05,
        transport=httpx.MockTransport(slow),
    )
    with pytest.raises(HTTPFetchError, match="timed out"):
        await timed.fetch({"url": "http://127.0.0.1/"})


@pytest.mark.asyncio
async def test_searxng_normalizes_results_and_parameters() -> None:
    observed: dict[str, str] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        observed.update(dict(request.url.params))
        return httpx.Response(
            200,
            json={
                "results": [
                    {"title": "One", "url": "https://one.test", "content": "first"},
                    {"title": "Two", "url": "https://two.test", "snippet": "second"},
                ]
            },
        )

    tool = SearXNGSearchTool(
        "http://searx.local/search",
        transport=httpx.MockTransport(handler),
    )
    result = await tool.search({"query": "  stars  ", "limit": 1, "categories": ["news", "it"]})
    assert result == {
        "query": "stars",
        "results": [{"title": "One", "url": "https://one.test", "snippet": "first"}],
        "count": 1,
    }
    assert observed == {"q": "stars", "format": "json", "categories": "news,it"}


@pytest.mark.asyncio
async def test_searxng_errors_are_explicit() -> None:
    status_tool = SearXNGSearchTool(
        "https://search.test/",
        transport=httpx.MockTransport(lambda _: httpx.Response(503)),
    )
    with pytest.raises(SearchToolError, match="non-success"):
        await status_tool.search({"query": "x"})
    with pytest.raises(SearchToolError, match="limit"):
        await status_tool.search({"query": "x", "limit": 21})

    invalid_tool = SearXNGSearchTool(
        "https://search.test/",
        transport=httpx.MockTransport(
            lambda _: httpx.Response(200, content=json.dumps({"wrong": []}))
        ),
    )
    with pytest.raises(SearchToolError, match="results array"):
        await invalid_tool.search({"query": "x"})


def test_http_search_schemas_safety_and_default_registry(tmp_path: Path) -> None:
    http_entry = HTTPFetchTool().entry()
    search_entry = SearXNGSearchTool("https://search.test/").entry()
    assert http_entry.safety_class is SafetyClass.READ_ONLY
    assert search_entry.safety_class is SafetyClass.READ_ONLY
    http_parameters = http_entry.schema["parameters"]
    assert isinstance(http_parameters, dict)
    http_properties = http_parameters["properties"]
    assert isinstance(http_properties, dict)
    assert set(http_properties) == {"url", "timeout"}
    search_parameters = search_entry.schema["parameters"]
    assert isinstance(search_parameters, dict)
    properties = search_parameters["properties"]
    assert isinstance(properties, dict)
    assert properties["limit"] == {"type": "integer", "minimum": 1, "maximum": 20}

    registry = build_default_registry([tmp_path], "https://search.test/")
    assert registry.names() == (
        "http_fetch",
        "list_directory",
        "read_file",
        "search",
        "terminal",
        "write_file",
    )
    assert registry.aliases()["web_search"] == "search"
    assert registry.get_entry("terminal").safety_class is SafetyClass.OPAQUE_SIDE_EFFECT
