from __future__ import annotations

import asyncio
import os
import shlex
import sys
from pathlib import Path

import pytest

from polaris.tools import TerminalTool


def python_command(source: str) -> str:
    return f"{shlex.quote(sys.executable)} -c {shlex.quote(source)}"


@pytest.mark.asyncio
@pytest.mark.skipif(os.name != "posix", reason="process-group semantics require POSIX")
async def test_cancellation_kills_the_entire_process_group(tmp_path: Path) -> None:
    marker = tmp_path / "cancelled-grandchild-ran"
    grandchild = python_command(
        f"import pathlib,time; time.sleep(1); pathlib.Path({str(marker)!r}).write_text('bad')"
    )
    parent = python_command(
        f"import subprocess,time; subprocess.Popen({grandchild!r}, shell=True); time.sleep(30)"
    )
    execution = asyncio.create_task(
        TerminalTool([tmp_path], default_timeout=60).execute({"command": parent})
    )
    await asyncio.sleep(0.2)
    execution.cancel()
    with pytest.raises(asyncio.CancelledError):
        await execution
    await asyncio.sleep(1.2)
    assert not marker.exists()
