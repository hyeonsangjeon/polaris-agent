from __future__ import annotations

import base64
import hashlib
from pathlib import Path

import pytest

from polaris.tools import (
    FileConflictError,
    FilesystemTools,
    PathAccessError,
    SafetyClass,
)


@pytest.mark.asyncio
async def test_read_list_hash_and_path_traversal(tmp_path: Path) -> None:
    root = tmp_path / "root"
    root.mkdir()
    file_path = root / "hello.txt"
    file_path.write_text("hello", encoding="utf-8")
    tools = FilesystemTools([root])

    result = await tools.read_file({"path": "hello.txt"})
    assert result == {
        "path": str(file_path),
        "content": "hello",
        "encoding": "utf-8",
        "sha256": hashlib.sha256(b"hello").hexdigest(),
        "size": 5,
    }
    listing = await tools.list_directory({"path": "."})
    assert isinstance(listing, dict)
    assert listing["entries"] == [
        {
            "name": "hello.txt",
            "path": str(file_path),
            "type": "file",
            "size": 5,
            "sha256": hashlib.sha256(b"hello").hexdigest(),
        }
    ]
    with pytest.raises(PathAccessError, match="escapes"):
        await tools.read_file({"path": "../outside.txt"})


@pytest.mark.asyncio
async def test_symlink_escape_is_rejected_for_reads_and_writes(tmp_path: Path) -> None:
    root = tmp_path / "root"
    outside = tmp_path / "outside"
    root.mkdir()
    outside.mkdir()
    (outside / "secret").write_text("secret", encoding="utf-8")
    (root / "escape").symlink_to(outside, target_is_directory=True)
    tools = FilesystemTools([root])

    with pytest.raises(PathAccessError, match="symlink"):
        await tools.read_file({"path": "escape/secret"})
    with pytest.raises(PathAccessError, match="symlink"):
        await tools.write_file({"path": "escape/new", "content": "bad"})
    assert not (outside / "new").exists()


@pytest.mark.asyncio
async def test_atomic_write_reconcile_idempotency_and_binary_content(tmp_path: Path) -> None:
    root = tmp_path / "root"
    root.mkdir()
    tools = FilesystemTools([root])
    arguments = {"path": "nested/data.bin", "content_base64": base64.b64encode(b"\x00x").decode()}

    first = await tools.write_file(arguments)
    assert isinstance(first, dict)
    assert first["written"] is True
    assert first["sha256"] == hashlib.sha256(b"\x00x").hexdigest()
    assert first["size"] == 2
    assert not list((root / "nested").glob(".polaris-write-*"))

    reconciled = await tools.reconcile_write(arguments)
    assert isinstance(reconciled, dict)
    assert reconciled["found"] is True
    assert reconciled["receipt"] == first["receipt"]
    second = await tools.write_file(arguments)
    assert isinstance(second, dict)
    assert second["written"] is False
    assert (root / "nested/data.bin").read_bytes() == b"\x00x"

    read_binary = await tools.read_file({"path": "nested/data.bin", "encoding": "base64"})
    assert isinstance(read_binary, dict)
    assert read_binary["content"] == arguments["content_base64"]

    missing = await tools.reconcile_write({"path": "not-created/data", "content": "x"})
    assert isinstance(missing, dict)
    assert missing["found"] is False
    assert not (root / "not-created").exists()


@pytest.mark.asyncio
async def test_expected_hash_conflict_and_absent_precondition(tmp_path: Path) -> None:
    root = tmp_path / "root"
    root.mkdir()
    target = root / "state"
    target.write_text("old", encoding="utf-8")
    tools = FilesystemTools([root])

    with pytest.raises(FileConflictError):
        await tools.write_file(
            {"path": "state", "content": "new", "expected_previous_hash": "0" * 64}
        )
    assert target.read_text(encoding="utf-8") == "old"
    old_hash = hashlib.sha256(b"old").hexdigest()
    result = await tools.write_file(
        {"path": "state", "content": "new", "expected_previous_hash": old_hash}
    )
    assert isinstance(result, dict)
    assert result["previous_sha256"] == old_hash

    with pytest.raises(FileConflictError):
        await tools.write_file(
            {"path": "state", "content": "other", "expected_previous_hash": None}
        )
    created = await tools.write_file(
        {"path": "absent", "content": "created", "expected_previous_hash": None}
    )
    assert isinstance(created, dict)
    assert created["written"] is True


def test_filesystem_schemas_and_safety_classes(tmp_path: Path) -> None:
    entries = {entry.name: entry for entry in FilesystemTools([tmp_path]).entries()}
    assert entries["read_file"].safety_class is SafetyClass.READ_ONLY
    assert entries["list_directory"].safety_class is SafetyClass.READ_ONLY
    assert entries["write_file"].safety_class is SafetyClass.RECONCILABLE
    assert entries["write_file"].reconcile_handler is not None
    parameters = entries["write_file"].schema["parameters"]
    assert isinstance(parameters, dict)
    assert parameters["oneOf"] == [
        {"required": ["content"]},
        {"required": ["content_base64"]},
    ]
