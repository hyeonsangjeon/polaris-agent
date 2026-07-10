from __future__ import annotations

import io
import json
import os
import tarfile
from pathlib import Path

import pytest

from polaris.artifacts import (
    ArtifactIntegrityError,
    ArtifactNotFoundError,
    ArtifactStore,
)
from polaris.backup import BackupError, BackupFormatError, BackupManager
from polaris.backup.archive import (
    HEADER,
    MAGIC,
    NONCE_SIZE,
    SALT_SIZE,
    TAG_SIZE,
    VERSION,
    _chunks,
    _decrypt_file,
    _derive_key,
    _sanitize_config,
)
from polaris.journal import Journal


def test_artifact_type_missing_and_existing_tamper_guards(tmp_path: Path) -> None:
    store = ArtifactStore(tmp_path / "artifacts")
    with pytest.raises(TypeError, match="bytes-like"):
        store.put("not bytes")  # type: ignore[arg-type]
    with pytest.raises(TypeError, match="string"):
        store.put_text(b"text")  # type: ignore[arg-type]
    with pytest.raises(ArtifactNotFoundError):
        store.get("0" * 64)

    artifact = store.put(bytearray(b"trusted"))
    store.path_for(artifact.sha256).write_bytes(b"changed")
    with pytest.raises(ArtifactIntegrityError):
        store.put(memoryview(b"trusted"))
    assert store.get(artifact.sha256, verify=False) == b"changed"


def test_artifact_record_variants_and_concurrent_insert(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    store = ArtifactStore(tmp_path / "artifacts")
    journal = Journal(tmp_path / "journal.sqlite3")
    run = journal.create_run("test", {}, {})

    text = store.record_artifact(journal, run.id, "text", "hello")
    binary = store.record_artifact(journal, run.id, "bytes", b"bytes")
    assert store.get_text(text.sha256 or "") == "hello"
    assert store.get(binary.sha256 or "") == b"bytes"
    with pytest.raises(TypeError, match="json_value"):
        store.record_artifact(journal, run.id, "invalid", object())

    content = b"raced"
    digest = store._digest(content)
    destination = store.path_for(digest)

    def racing_link(_source: Path, target: Path) -> None:
        target.write_bytes(content)
        raise FileExistsError

    monkeypatch.setattr(os, "link", racing_link)
    raced = store.put(content)
    assert raced.sha256 == digest
    assert destination.read_bytes() == content
    journal.close()


def test_backup_header_truncation_and_passphrase_errors(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="empty"):
        _derive_key("", b"x" * SALT_SIZE)
    with pytest.raises(BackupFormatError, match="truncated"):
        list(_chunks(io.BytesIO(b"x"), limit=2))

    destination = tmp_path / "plain.tar"
    short = tmp_path / "short.backup"
    short.write_bytes(b"x")
    with pytest.raises(BackupFormatError, match="too short"):
        _decrypt_file(short, destination, "password")

    for name, magic, version, message in (
        ("magic", b"X" * len(MAGIC), VERSION, "magic"),
        ("version", MAGIC, VERSION + 1, "version"),
    ):
        archive = tmp_path / f"{name}.backup"
        archive.write_bytes(
            HEADER.pack(magic, version, b"s" * SALT_SIZE, b"n" * NONCE_SIZE)
            + b"t" * TAG_SIZE
        )
        with pytest.raises(BackupFormatError, match=message):
            _decrypt_file(archive, destination, "password")


def test_backup_sanitization_config_errors_and_defaults(tmp_path: Path) -> None:
    sanitized = _sanitize_config(
        {
            "Password": "drop",
            "nested": [{"api-token": "drop", "keep": 1}],
            "headers": {
                "Authorization": "drop",
                "X-API-Key": "drop",
                "Accept": "application/json",
            },
        }
    )
    assert sanitized == {
        "nested": [{"keep": 1}],
        "headers": {"Accept": "application/json"},
    }

    data = tmp_path / "data"
    manager = BackupManager(data_dir=data)
    staged = tmp_path / "staged.json"
    manager._stage_config(staged)
    assert json.loads(staged.read_text()) == {"data_dir": str(data.resolve())}

    data.mkdir()
    manager.config_file.write_text("{")
    with pytest.raises(BackupFormatError, match="configuration"):
        manager._stage_config(tmp_path / "bad.json")


def test_backup_rejects_artifact_symlinks_and_unsafe_tar_members(tmp_path: Path) -> None:
    data = tmp_path / "data"
    artifacts = data / "artifacts"
    artifacts.mkdir(parents=True)
    (artifacts / "link").symlink_to(tmp_path / "outside")
    manager = BackupManager(data_dir=data)
    with pytest.raises(BackupError, match="symlinks"):
        manager._stage_artifacts(tmp_path / "staged")

    archive = tmp_path / "links.tar"
    with tarfile.open(archive, "w") as bundle:
        member = tarfile.TarInfo("state/link")
        member.type = tarfile.SYMTYPE
        member.linkname = "target"
        bundle.addfile(member)
    with pytest.raises(BackupFormatError, match="unsupported archive member"):
        manager._safe_extract(archive, tmp_path / "extract")

    duplicate = tmp_path / "duplicate.tar"
    with tarfile.open(duplicate, "w") as bundle:
        for payload in (b"a", b"b"):
            member = tarfile.TarInfo("same")
            member.size = len(payload)
            bundle.addfile(member, io.BytesIO(payload))
    with pytest.raises(BackupFormatError, match="unsafe archive path"):
        manager._safe_extract(duplicate, tmp_path / "duplicate-extract")


def _manifest_tree(tmp_path: Path) -> tuple[Path, BackupManager]:
    extracted = tmp_path / "extracted"
    state = extracted / "state"
    state.mkdir(parents=True)
    (state / "config.json").write_text("{}")
    (state / "journal.sqlite3").write_bytes(b"db")
    manager = BackupManager(data_dir=tmp_path / "target")
    files = manager._manifest_files(state)
    (extracted / "manifest.json").write_text(
        json.dumps({"format": "polaris-backup", "version": VERSION, "files": files})
    )
    return extracted, manager


def test_backup_manifest_detects_tampering_extras_and_missing_requirements(
    tmp_path: Path,
) -> None:
    extracted, manager = _manifest_tree(tmp_path)
    assert len(manager._verify_manifest(extracted)) == 2

    (extracted / "state" / "config.json").write_text('{"tampered":true}')
    with pytest.raises(BackupFormatError, match="failed verification"):
        manager._verify_manifest(extracted)

    extracted, manager = _manifest_tree(tmp_path / "extra")
    (extracted / "state" / "extra").write_text("unlisted")
    with pytest.raises(BackupFormatError, match="do not match"):
        manager._verify_manifest(extracted)

    extracted, manager = _manifest_tree(tmp_path / "missing")
    manifest_path = extracted / "manifest.json"
    manifest = json.loads(manifest_path.read_text())
    manifest["files"] = [
        item for item in manifest["files"] if item["path"] != "state/config.json"
    ]
    (extracted / "state" / "config.json").unlink()
    manifest_path.write_text(json.dumps(manifest))
    with pytest.raises(BackupFormatError, match="missing required"):
        manager._verify_manifest(extracted)


@pytest.mark.parametrize(
    "manifest",
    [
        None,
        {"format": "wrong", "version": VERSION, "files": []},
        {"format": "polaris-backup", "version": VERSION, "files": ["bad"]},
        {
            "format": "polaris-backup",
            "version": VERSION,
            "files": [{"path": "../escape", "sha256": "0" * 64, "size": 0}],
        },
    ],
)
def test_backup_manifest_shape_and_path_validation(
    tmp_path: Path, manifest: object | None
) -> None:
    extracted = tmp_path / "extracted"
    (extracted / "state").mkdir(parents=True)
    if manifest is not None:
        (extracted / "manifest.json").write_text(json.dumps(manifest))
    with pytest.raises(BackupFormatError):
        BackupManager._verify_manifest(extracted)


def test_backup_rewrite_config_state_detection_and_destination_guards(tmp_path: Path) -> None:
    manager = BackupManager(data_dir=tmp_path / "target")
    config = tmp_path / "config.json"
    config.write_text("[]")
    with pytest.raises(BackupFormatError, match="JSON object"):
        manager._rewrite_config(config)

    config.write_text(json.dumps({"daemon": {}}))
    manager._rewrite_config(config)
    rewritten = json.loads(config.read_text())
    assert rewritten["data_dir"] == str(manager.data_dir)
    assert rewritten["daemon"]["token_file"] == str(manager.data_dir / "api-token")

    assert manager._has_existing_state() is False
    manager.data_dir.mkdir()
    assert manager._has_existing_state() is False
    (manager.data_dir / "state").write_text("x")
    assert manager._has_existing_state() is True

    with pytest.raises(FileNotFoundError):
        manager.import_archive(tmp_path / "missing.backup", "password")
    destination = tmp_path / "exists.backup"
    destination.write_bytes(b"existing")
    with pytest.raises(FileExistsError):
        manager.export(destination, "password")
