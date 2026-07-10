from __future__ import annotations

import hashlib
import os
from pathlib import Path

import pytest

from polaris.artifacts import ArtifactIntegrityError, ArtifactStore
from polaris.journal import Journal


def test_content_addressed_storage_helpers_and_no_rewrite(tmp_path: Path) -> None:
    store = ArtifactStore(tmp_path / "artifacts")
    first = store.put_text("hello")
    modified = store.path_for(first.sha256).stat().st_mtime_ns
    second = store.put(b"hello")

    assert first == second
    assert first.sha256 == hashlib.sha256(b"hello").hexdigest()
    assert store.get_text(first.sha256) == "hello"
    assert store.path_for(first.sha256).stat().st_mtime_ns == modified
    value = {"z": 1, "a": [True, None]}
    json_artifact = store.put_json(value)
    assert store.get_json(json_artifact.sha256) == value


def test_tamper_detection_and_path_safety(tmp_path: Path) -> None:
    store = ArtifactStore(tmp_path / "artifacts")
    artifact = store.put_text("trusted")
    store.path_for(artifact.sha256).write_text("tampered")

    with pytest.raises(ArtifactIntegrityError):
        store.get(artifact.sha256)
    with pytest.raises(ValueError):
        store.get("../../etc/passwd")
    with pytest.raises(ValueError):
        store.path_for("A" * 64)


def test_store_records_journal_artifact(tmp_path: Path) -> None:
    store = ArtifactStore(tmp_path / "artifacts")
    journal = Journal(tmp_path / "journal.sqlite3")
    run = journal.create_run("single", {}, {})
    record = store.record_artifact(
        journal,
        run.id,
        "answer.json",
        {"answer": 42},
        media_type="application/json",
        json_value=True,
    )

    assert record.sha256 is not None
    assert store.verify(record.sha256)
    assert store.get_json(record.sha256) == {"answer": 42}
    assert os.path.isfile(Path(record.uri.removeprefix("file://")))
    journal.close()
