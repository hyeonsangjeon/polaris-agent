from __future__ import annotations

import os
import shlex
import sys
from pathlib import Path

import pytest

from polaris.tools import SafetyClass, TerminalPathError, TerminalTool, TerminalToolError
from polaris.tools.terminal import is_conservatively_pure_command


def python_command(source: str) -> str:
    return f"{shlex.quote(sys.executable)} -c {shlex.quote(source)}"


@pytest.mark.asyncio
async def test_terminal_reports_output_exit_duration_and_truncation(tmp_path: Path) -> None:
    tool = TerminalTool([tmp_path], default_max_output=4, max_output=100)
    result = await tool.execute(
        {
            "command": python_command(
                "import sys; print('abcdef', end=''); print('error', file=sys.stderr); sys.exit(3)"
            ),
            "cwd": str(tmp_path),
        }
    )
    assert isinstance(result, dict)
    assert result["stdout"] == "abcd"
    assert result["stderr"] == "erro"
    assert result["exit_code"] == 3
    assert result["stdout_truncated"] is True
    assert result["stderr_truncated"] is True
    assert isinstance(result["duration"], float)


@pytest.mark.asyncio
async def test_terminal_timeout_kills_process_group(tmp_path: Path) -> None:
    marker = tmp_path / "grandchild-ran"
    grandchild = python_command(
        f"import pathlib,time; time.sleep(1); pathlib.Path({str(marker)!r}).write_text('bad')"
    )
    parent = python_command(
        f"import subprocess,time; subprocess.Popen({grandchild!r}, shell=True); time.sleep(10)"
    )
    tool = TerminalTool([tmp_path], default_timeout=0.1, max_timeout=1)
    result = await tool.execute({"command": parent})
    assert isinstance(result, dict)
    assert result["timed_out"] is True
    assert isinstance(result["exit_code"], int)
    await __import__("asyncio").sleep(1.2)
    assert not marker.exists()


@pytest.mark.asyncio
async def test_terminal_cwd_environment_and_validation(tmp_path: Path) -> None:
    outside = tmp_path.parent
    tool = TerminalTool([tmp_path], environment_allowlist={"POLARIS_ALLOWED"})
    result = await tool.execute(
        {
            "command": python_command(
                "import os; print(os.getcwd()); print(os.environ['POLARIS_ALLOWED'])"
            ),
            "env": {"POLARIS_ALLOWED": "visible"},
        }
    )
    assert isinstance(result, dict)
    assert result["stdout"] == f"{tmp_path}\nvisible\n"

    with pytest.raises(TerminalPathError):
        await tool.execute({"command": "pwd", "cwd": str(outside)})
    with pytest.raises(TerminalToolError):
        await tool.execute({"command": ""})
    with pytest.raises(TerminalToolError):
        await tool.execute({"command": "echo\x00bad"})
    with pytest.raises(TerminalToolError):
        await tool.execute({"command": "true", "env": {"SECRET": "do-not-print"}})


def test_terminal_schema_safety_and_pure_metadata(tmp_path: Path) -> None:
    entry = TerminalTool([tmp_path]).entry()
    assert entry.safety_class is SafetyClass.OPAQUE_SIDE_EFFECT
    assert entry.schema["parameters"] == {
        "type": "object",
        "properties": {
            "command": {"type": "string", "minLength": 1},
            "cwd": {"type": "string"},
            "timeout": {"type": "number", "exclusiveMinimum": 0},
            "env": {"type": "object", "additionalProperties": {"type": "string"}},
            "max_output": {"type": "integer", "minimum": 1},
        },
        "required": ["command"],
        "additionalProperties": False,
    }
    assert is_conservatively_pure_command("pwd")
    assert not is_conservatively_pure_command("pwd > output")
    assert not is_conservatively_pure_command("python -c pass")
    assert os.name in {"posix", "nt"}
