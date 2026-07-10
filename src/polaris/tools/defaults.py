"""Default concrete tool registry construction."""

from __future__ import annotations

import os
from collections.abc import Iterable

from .filesystem import FilesystemTools
from .http import HTTPFetchTool
from .registry import ToolRegistry
from .search import SearXNGSearchTool
from .terminal import TerminalTool


def build_default_registry(
    roots: Iterable[str | os.PathLike[str]],
    searxng_url: str | None = None,
    allow_private_http: bool = False,
    *,
    include_network: bool = True,
) -> ToolRegistry:
    """Build an isolated registry containing adapters allowed by the network policy."""

    normalized_roots = tuple(roots)
    registry = ToolRegistry()
    registry.register(TerminalTool(normalized_roots).entry())
    filesystem = FilesystemTools(normalized_roots)
    for entry in filesystem.entries():
        registry.register(entry)
    if include_network:
        registry.register(HTTPFetchTool(allow_private=allow_private_http).entry())
        if searxng_url is not None:
            registry.register(SearXNGSearchTool(searxng_url).entry(), aliases=("web_search",))
    return registry
