"""Root-scoped asynchronous terminal tool."""

from __future__ import annotations

import asyncio
import math
import os
import re
import shlex
import signal
import time
from collections.abc import Iterable, Mapping
from pathlib import Path

from .registry import SafetyClass, ToolArguments, ToolEntry, ToolResult

_PURE_PROGRAMS = frozenset(
    {
        "basename",
        "cat",
        "cksum",
        "cut",
        "dirname",
        "du",
        "env",
        "false",
        "head",
        "id",
        "printf",
        "pwd",
        "sha256sum",
        "shasum",
        "stat",
        "tail",
        "true",
        "uname",
        "wc",
        "whoami",
    }
)
_SHELL_CONTROL = re.compile(r"[;&|<>`$(){}\n\r]")
_DEFAULT_ENV_ALLOWLIST = frozenset(
    {"HOME", "LANG", "LC_ALL", "LC_CTYPE", "PATH", "PATHEXT", "SYSTEMROOT", "TMPDIR", "WINDIR"}
)


class TerminalToolError(RuntimeError):
    """The terminal invocation was invalid or could not be started."""


class TerminalPathError(TerminalToolError):
    """The requested working directory is outside an allowed root."""


def is_conservatively_pure_command(command: str) -> bool:
    """Return metadata indicating a small, obviously non-mutating command subset."""

    if not command or _SHELL_CONTROL.search(command):
        return False
    try:
        parts = shlex.split(command)
    except ValueError:
        return False
    return bool(parts) and Path(parts[0]).name in _PURE_PROGRAMS


def _validated_roots(roots: Iterable[str | os.PathLike[str]]) -> tuple[Path, ...]:
    result: list[Path] = []
    for root in roots:
        resolved = Path(root).expanduser().resolve(strict=True)
        if not resolved.is_dir():
            raise ValueError("terminal roots must be directories")
        result.append(resolved)
    if not result:
        raise ValueError("at least one terminal root is required")
    return tuple(dict.fromkeys(result))


def _inside(path: Path, roots: tuple[Path, ...]) -> bool:
    return any(path == root or path.is_relative_to(root) for root in roots)


async def _read_limited(stream: asyncio.StreamReader | None, limit: int) -> tuple[bytes, bool]:
    if stream is None:
        return b"", False
    chunks: list[bytes] = []
    retained = 0
    truncated = False
    while True:
        chunk = await stream.read(64 * 1024)
        if not chunk:
            break
        remaining = limit - retained
        if remaining > 0:
            chunks.append(chunk[:remaining])
            retained += min(len(chunk), remaining)
        if len(chunk) > remaining:
            truncated = True
    return b"".join(chunks), truncated


class TerminalTool:
    """Execute commands while containing cwd, environment, output, and process lifetime."""

    def __init__(
        self,
        roots: Iterable[str | os.PathLike[str]],
        *,
        environment_allowlist: Iterable[str] = _DEFAULT_ENV_ALLOWLIST,
        default_timeout: float = 30.0,
        max_timeout: float = 300.0,
        default_max_output: int = 1_000_000,
        max_output: int = 10_000_000,
    ) -> None:
        self.roots = _validated_roots(roots)
        self.environment_allowlist = frozenset(environment_allowlist)
        if (
            default_timeout <= 0
            or max_timeout <= 0
            or default_timeout > max_timeout
            or default_max_output <= 0
            or max_output <= 0
            or default_max_output > max_output
        ):
            raise ValueError("terminal timeout and output limits must be positive and ordered")
        self.default_timeout = float(default_timeout)
        self.max_timeout = float(max_timeout)
        self.default_max_output = default_max_output
        self.max_output = max_output

    def _cwd(self, value: object) -> Path:
        if value is None:
            return self.roots[0]
        if not isinstance(value, str) or "\x00" in value:
            raise TerminalPathError("cwd must be a valid string")
        candidate = Path(value).expanduser()
        if not candidate.is_absolute():
            candidate = self.roots[0] / candidate
        try:
            resolved = candidate.resolve(strict=True)
        except (FileNotFoundError, OSError) as exc:
            raise TerminalPathError("cwd does not exist") from exc
        if not resolved.is_dir() or not _inside(resolved, self.roots):
            raise TerminalPathError("cwd escapes configured roots")
        return resolved

    def _environment(self, value: object) -> dict[str, str]:
        environment = {
            key: item for key, item in os.environ.items() if key in self.environment_allowlist
        }
        if value is None:
            return environment
        if not isinstance(value, Mapping):
            raise TerminalToolError("env must be an object")
        for key, item in value.items():
            if not isinstance(key, str) or key not in self.environment_allowlist:
                raise TerminalToolError("env contains a key outside the configured allowlist")
            if not isinstance(item, str) or "\x00" in item:
                raise TerminalToolError("env values must be valid strings")
            environment[key] = item
        return environment

    @staticmethod
    def _number(value: object, default: float, maximum: float, label: str) -> float:
        if value is None:
            return default
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            raise TerminalToolError(f"{label} must be a number")
        number = float(value)
        if not math.isfinite(number) or number <= 0 or number > maximum:
            raise TerminalToolError(f"{label} is outside the configured limit")
        return number

    @staticmethod
    async def _kill_process_group(process: asyncio.subprocess.Process) -> None:
        if process.returncode is not None:
            return
        try:
            if os.name == "posix":
                os.killpg(process.pid, signal.SIGKILL)
            else:
                process.kill()
        except ProcessLookupError:
            pass
        await process.wait()

    async def execute(self, arguments: ToolArguments) -> ToolResult:
        command = arguments.get("command")
        if not isinstance(command, str) or not command.strip() or "\x00" in command:
            raise TerminalToolError("command must be a non-empty string without NUL")
        cwd = self._cwd(arguments.get("cwd"))
        timeout = self._number(
            arguments.get("timeout"), self.default_timeout, self.max_timeout, "timeout"
        )
        output_limit = int(
            self._number(
                arguments.get("max_output"),
                float(self.default_max_output),
                float(self.max_output),
                "max_output",
            )
        )
        environment = self._environment(arguments.get("env"))
        started = time.monotonic()
        try:
            process = await asyncio.create_subprocess_shell(
                command,
                cwd=cwd,
                env=environment,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                start_new_session=os.name == "posix",
            )
        except OSError as exc:
            raise TerminalToolError("command could not be started") from exc

        stdout_task = asyncio.create_task(_read_limited(process.stdout, output_limit))
        stderr_task = asyncio.create_task(_read_limited(process.stderr, output_limit))
        wait_task = asyncio.create_task(process.wait())
        timed_out = False
        try:
            await asyncio.wait_for(asyncio.shield(wait_task), timeout)
        except TimeoutError:
            timed_out = True
            await self._kill_process_group(process)
        except asyncio.CancelledError:
            await asyncio.shield(self._kill_process_group(process))
            await asyncio.shield(asyncio.gather(stdout_task, stderr_task, wait_task))
            raise
        stdout_result, stderr_result = await asyncio.gather(stdout_task, stderr_task)
        await wait_task
        stdout, stdout_truncated = stdout_result
        stderr, stderr_truncated = stderr_result
        return {
            "stdout": stdout.decode("utf-8", errors="replace"),
            "stderr": stderr.decode("utf-8", errors="replace"),
            "exit_code": process.returncode,
            "duration": time.monotonic() - started,
            "timed_out": timed_out,
            "stdout_truncated": stdout_truncated,
            "stderr_truncated": stderr_truncated,
            "metadata": {"conservatively_pure": is_conservatively_pure_command(command)},
        }

    def entry(self) -> ToolEntry:
        return ToolEntry(
            name="terminal",
            toolset="terminal",
            description="Execute a shell command in an allowed working directory.",
            schema={
                "parameters": {
                    "type": "object",
                    "properties": {
                        "command": {"type": "string", "minLength": 1},
                        "cwd": {"type": "string"},
                        "timeout": {"type": "number", "exclusiveMinimum": 0},
                        "env": {
                            "type": "object",
                            "additionalProperties": {"type": "string"},
                        },
                        "max_output": {"type": "integer", "minimum": 1},
                    },
                    "required": ["command"],
                    "additionalProperties": False,
                },
                "x-polaris-pure-command-detection": "conservative-metadata-only",
            },
            handler=self.execute,
            safety_class=SafetyClass.OPAQUE_SIDE_EFFECT,
        )


def create_terminal_entry(
    roots: Iterable[str | os.PathLike[str]],
) -> ToolEntry:
    """Create a terminal registry entry."""

    return TerminalTool(roots).entry()
