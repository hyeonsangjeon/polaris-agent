from __future__ import annotations

import asyncio
import base64
import socket
from pathlib import Path
from typing import cast

import httpx
import pytest

from polaris.tools import (
    FileConflictError,
    FilesystemToolError,
    FilesystemTools,
    HTTPFetchError,
    HTTPFetchTool,
    HTTPSSRFError,
    PathAccessError,
    SearchToolError,
    SearXNGSearchTool,
    TerminalPathError,
    TerminalTool,
    TerminalToolError,
    create_filesystem_entries,
    create_http_fetch_entry,
    create_search_entry,
    create_terminal_entry,
)
from polaris.tools.registry import ToolArguments
from polaris.tools.terminal import _read_limited, is_conservatively_pure_command


def test_filesystem_configuration_and_argument_validation(tmp_path: Path) -> None:
    file_root = tmp_path / "file"
    file_root.write_text("x")
    with pytest.raises(ValueError, match="directories"):
        FilesystemTools([file_root])
    with pytest.raises(ValueError, match="at least one"):
        FilesystemTools([])
    with pytest.raises(ValueError, match="limits"):
        FilesystemTools([tmp_path], max_read_bytes=0)

    tools = FilesystemTools([tmp_path])
    for arguments in ({}, {"path": ""}, {"path": "x\x00y"}):
        with pytest.raises(PathAccessError):
            tools._path_value(arguments)
    with pytest.raises(PathAccessError, match="does not exist"):
        tools._existing_path("missing")


@pytest.mark.asyncio
async def test_filesystem_read_list_and_encoding_boundaries(tmp_path: Path) -> None:
    directory = tmp_path / "directory"
    directory.mkdir()
    binary = tmp_path / "binary"
    binary.write_bytes(b"\xff")
    (tmp_path / "child").mkdir()
    (tmp_path / "link").symlink_to(binary)

    tools = FilesystemTools([tmp_path], max_read_bytes=1, max_list_entries=10)
    with pytest.raises(PathAccessError, match="not a file"):
        await tools.read_file({"path": "directory"})
    with pytest.raises(FilesystemToolError, match="UTF-8"):
        await tools.read_file({"path": "binary"})
    with pytest.raises(FilesystemToolError, match="encoding"):
        await tools.read_file({"path": "binary", "encoding": "latin-1"})
    with pytest.raises(PathAccessError, match="not a directory"):
        await tools.list_directory({"path": "binary"})

    listing = await tools.list_directory({"path": "."})
    assert isinstance(listing, dict)
    entries = listing["entries"]
    assert isinstance(entries, list)
    assert all(isinstance(entry, dict) for entry in entries)
    kinds = {entry["name"]: entry["type"] for entry in entries if isinstance(entry, dict)}
    assert kinds["child"] == "directory"
    assert kinds["link"] == "symlink"

    (tmp_path / "too-large").write_bytes(b"xx")
    with pytest.raises(FilesystemToolError, match="read limit"):
        await tools.read_file({"path": "too-large", "encoding": "base64"})
    limited = FilesystemTools([tmp_path], max_list_entries=1)
    with pytest.raises(FilesystemToolError, match="entry limit"):
        await limited.list_directory({"path": "."})


@pytest.mark.parametrize(
    "arguments",
    [
        {"content": "x", "content_base64": "eA=="},
        {},
        {"content": 1},
        {"content_base64": 1},
        {"content_base64": "%%%"},
    ],
)
def test_filesystem_content_validation(arguments: ToolArguments) -> None:
    with pytest.raises(FilesystemToolError):
        FilesystemTools._content(arguments)


@pytest.mark.asyncio
async def test_filesystem_hash_conflicts_and_write_target_guards(tmp_path: Path) -> None:
    tools = FilesystemTools([tmp_path])
    with pytest.raises(FilesystemToolError, match="only one"):
        await tools.write_file(
            {
                "path": "file",
                "content": "x",
                "expected_previous_hash": None,
                "expected_sha256": None,
            }
        )
    with pytest.raises(FilesystemToolError, match="SHA-256"):
        await tools.write_file(
            {"path": "file", "content": "x", "expected_previous_hash": "bad"}
        )

    directory = tmp_path / "directory"
    directory.mkdir()
    with pytest.raises(PathAccessError, match="root-scoped file"):
        await tools.write_file({"path": "directory", "content": "x"})

    target = tmp_path / "target"
    target.write_text("safe")
    link = tmp_path / "link"
    link.symlink_to(target)
    with pytest.raises(PathAccessError, match="symlink"):
        await tools.write_file({"path": "link", "content": "unsafe"})

    original_current = tools._current
    calls = 0

    def changing_current(path: Path) -> tuple[str | None, int | None]:
        nonlocal calls
        calls += 1
        if calls == 2:
            path.write_text("raced")
        return original_current(path)

    race = tmp_path / "race"
    race.write_text("old")
    tools._current = changing_current  # type: ignore[method-assign]
    with pytest.raises(FileConflictError, match="changed"):
        await tools.write_file({"path": "race", "content": "new"})

    assert len(create_filesystem_entries([tmp_path])) == 3


@pytest.mark.asyncio
async def test_http_url_dns_and_redirect_guards(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    tool = HTTPFetchTool()
    for url in (
        "mailto:user@example.com",
        "http://user:password@example.com",
        "http://localhost/",
        "http://name.localhost/",
    ):
        with pytest.raises(HTTPSSRFError):
            await tool.fetch({"url": url})

    monkeypatch.setattr(socket, "getaddrinfo", lambda *_args, **_kwargs: [])
    with pytest.raises(HTTPFetchError, match="resolved"):
        await tool.fetch({"url": "http://empty.example/"})

    monkeypatch.setattr(
        socket,
        "getaddrinfo",
        lambda *_args, **_kwargs: [
            (socket.AF_INET, socket.SOCK_STREAM, 6, "", ("127.0.0.1", 80))
        ],
    )
    with pytest.raises(HTTPSSRFError):
        await tool.fetch({"url": "http://private.example/"})

    no_location = HTTPFetchTool(
        allow_private=True,
        transport=httpx.MockTransport(lambda _: httpx.Response(302)),
    )
    with pytest.raises(HTTPFetchError, match="no location"):
        await no_location.fetch({"url": "http://example.test/"})

    redirect_limit = HTTPFetchTool(
        allow_private=True,
        max_redirects=0,
        transport=httpx.MockTransport(
            lambda _: httpx.Response(302, headers={"location": "/again"})
        ),
    )
    with pytest.raises(HTTPFetchError, match="redirect limit"):
        await redirect_limit.fetch({"url": "http://example.test/"})


@pytest.mark.asyncio
async def test_http_binary_encoding_inputs_and_transport_errors() -> None:
    binary = HTTPFetchTool(
        allow_private=True,
        transport=httpx.MockTransport(
            lambda _: httpx.Response(
                200, content=b"\x00\xff", headers={"content-type": "application/octet-stream"}
            )
        ),
    )
    result = await binary.fetch({"url": "https://example.test/"})
    assert isinstance(result, dict)
    assert result["content"] == base64.b64encode(b"\x00\xff").decode()
    assert result["encoding"] == "base64"

    for arguments in (
        {},
        {"url": ""},
        {"url": "x\x00y"},
        {"url": "https://example.test", "timeout": True},
        {"url": "https://example.test", "timeout": 21},
    ):
        with pytest.raises(HTTPFetchError):
            await binary.fetch(cast(ToolArguments, arguments))

    def broken(request: httpx.Request) -> httpx.Response:
        raise httpx.ReadError("broken", request=request)

    failed = HTTPFetchTool(
        allow_private=True, transport=httpx.MockTransport(broken)
    )
    with pytest.raises(HTTPFetchError, match="failed"):
        await failed.fetch({"url": "https://example.test/"})

    for kwargs in (
        {"timeout": 0},
        {"timeout": float("nan")},
        {"max_response_bytes": 0},
        {"max_redirects": -1},
    ):
        with pytest.raises(ValueError):
            HTTPFetchTool(**kwargs)
    assert create_http_fetch_entry().name == "http_fetch"


@pytest.mark.asyncio
async def test_search_validation_response_and_transport_boundaries() -> None:
    for endpoint in ("ftp://example.test", "http://user:pass@example.test"):
        with pytest.raises(ValueError):
            SearXNGSearchTool(endpoint)
    with pytest.raises(ValueError):
        SearXNGSearchTool("https://example.test", timeout=0)

    tool = SearXNGSearchTool(
        "https://example.test",
        transport=httpx.MockTransport(lambda _: httpx.Response(200, json={"results": []})),
    )
    for arguments in (
        {},
        {"query": "\x00"},
        {"query": "q", "limit": True},
        {"query": "q", "categories": []},
        {"query": "q", "categories": [""]},
    ):
        with pytest.raises(SearchToolError):
            await tool.search(cast(ToolArguments, arguments))

    oversized = SearXNGSearchTool(
        "https://example.test",
        max_response_bytes=2,
        transport=httpx.MockTransport(lambda _: httpx.Response(200, content=b"{}x")),
    )
    with pytest.raises(SearchToolError, match="byte limit"):
        await oversized.search({"query": "q"})

    for payload, message in (
        (b"{", "invalid JSON"),
        (b"[]", "JSON object"),
        (b'{"results":[1]}', "result must be"),
        (b'{"results":[{"title":1,"url":"u"}]}', "fields"),
    ):
        invalid = SearXNGSearchTool(
            "https://example.test",
            transport=httpx.MockTransport(
                lambda _, body=payload: httpx.Response(200, content=body)
            ),
        )
        with pytest.raises(SearchToolError, match=message):
            await invalid.search({"query": "q"})
    assert create_search_entry("https://example.test").name == "search"


def test_terminal_configuration_purity_cwd_env_and_number_validation(tmp_path: Path) -> None:
    file_root = tmp_path / "file"
    file_root.write_text("x")
    with pytest.raises(ValueError):
        TerminalTool([])
    with pytest.raises(ValueError):
        TerminalTool([file_root])
    with pytest.raises(ValueError):
        TerminalTool([tmp_path], default_timeout=2, max_timeout=1)

    tool = TerminalTool(
        [tmp_path], environment_allowlist={"ALLOWED"}, default_timeout=1, max_timeout=2
    )
    assert tool._cwd(None) == tmp_path
    for cwd in (1, "missing"):
        with pytest.raises(TerminalPathError):
            tool._cwd(cwd)
    env_values: tuple[object, ...] = (
        [],
        {"NO": "x"},
        {"ALLOWED": 1},
        {"ALLOWED": "x\x00y"},
    )
    for env in env_values:
        with pytest.raises(TerminalToolError):
            tool._environment(env)
    for value in (True, "1", 0, 3, float("nan")):
        with pytest.raises(TerminalToolError):
            tool._number(value, 1, 2, "limit")

    assert not is_conservatively_pure_command("")
    assert not is_conservatively_pure_command("'unterminated")
    assert is_conservatively_pure_command("/usr/bin/printf ok")
    assert create_terminal_entry([tmp_path]).name == "terminal"


@pytest.mark.asyncio
async def test_terminal_reader_and_start_failure(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    assert await _read_limited(None, 1) == (b"", False)

    async def fail_start(*_args: object, **_kwargs: object) -> None:
        raise OSError("cannot start")

    monkeypatch.setattr(asyncio, "create_subprocess_shell", fail_start)
    tool = TerminalTool([tmp_path])
    with pytest.raises(TerminalToolError, match="could not be started"):
        await tool.execute({"command": "true"})
