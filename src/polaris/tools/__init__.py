"""Typed tool contracts and concrete adapters."""

from .defaults import build_default_registry
from .filesystem import (
    FileConflictError,
    FilesystemToolError,
    FilesystemTools,
    PathAccessError,
    create_filesystem_entries,
)
from .http import (
    HTTPFetchError,
    HTTPFetchTool,
    HTTPResponseTooLargeError,
    HTTPSSRFError,
    create_http_fetch_entry,
)
from .registry import (
    AvailabilityCheck,
    DuplicateToolError,
    ReconcileHandler,
    SafetyClass,
    ToolArguments,
    ToolEntry,
    ToolHandler,
    ToolRegistry,
    ToolRegistryError,
    ToolResult,
    ToolResultTooLargeError,
    ToolUnavailableError,
    UnknownToolError,
    registry,
)
from .search import SearchToolError, SearXNGSearchTool, create_search_entry
from .terminal import (
    TerminalPathError,
    TerminalTool,
    TerminalToolError,
    create_terminal_entry,
    is_conservatively_pure_command,
)

__all__ = [
    "AvailabilityCheck",
    "DuplicateToolError",
    "FileConflictError",
    "FilesystemToolError",
    "FilesystemTools",
    "HTTPFetchError",
    "HTTPFetchTool",
    "HTTPResponseTooLargeError",
    "HTTPSSRFError",
    "PathAccessError",
    "ReconcileHandler",
    "SafetyClass",
    "SearchToolError",
    "SearXNGSearchTool",
    "TerminalPathError",
    "TerminalTool",
    "TerminalToolError",
    "ToolArguments",
    "ToolEntry",
    "ToolHandler",
    "ToolRegistry",
    "ToolRegistryError",
    "ToolResult",
    "ToolResultTooLargeError",
    "ToolUnavailableError",
    "UnknownToolError",
    "build_default_registry",
    "create_filesystem_entries",
    "create_http_fetch_entry",
    "create_search_entry",
    "create_terminal_entry",
    "is_conservatively_pure_command",
    "registry",
]
