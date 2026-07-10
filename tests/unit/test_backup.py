from __future__ import annotations

import io
import json
import os
import sqlite3
import stat
import tarfile
from pathlib import Path

import pytest

from polaris.backup import (
    BackupAuthenticationError,
    BackupFormatError,
    BackupManager,
    ExistingStateError,
)
from polaris.backup.archive import _encrypt_file


class SimulatedRestoreCrash(BaseException):
    pass


def source_state(tmp_path: Path) -> tuple[BackupManager, sqlite3.Connection]:
    data = tmp_path / "source"
    artifacts = data / "artifacts" / "ab"
    artifacts.mkdir(parents=True)
    (artifacts / "result.txt").write_text("artifact content")
    (data / "api-token").write_text("never-back-this-up")
    config = data / "config.json"
    config.write_text(
        json.dumps(
            {
                "data_dir": str(data),
                "token": "embedded-token",
                "daemon": {"token_file": str(data / "api-token")},
                "providers": {
                    "local": {
                        "api_key_env": "MODEL_API_KEY",
                        "headers": {"Authorization": "embedded-header"},
                    }
                },
            }
        )
    )
    connection = sqlite3.connect(data / "journal.sqlite3")
    connection.execute("PRAGMA journal_mode=WAL")
    connection.execute("CREATE TABLE live (value TEXT NOT NULL)")
    connection.execute("INSERT INTO live VALUES ('committed while open')")
    connection.commit()
    return BackupManager(data_dir=data), connection


def test_archive_roundtrip_live_sqlite_artifacts_and_token_exclusion(
    tmp_path: Path,
) -> None:
    source, connection = source_state(tmp_path)
    backup = tmp_path / "state.polaris-backup"
    try:
        report = source.export(backup, "correct horse battery staple")
    finally:
        connection.close()

    target_data = tmp_path / "restored"
    target = BackupManager(data_dir=target_data)
    imported = target.import_archive(backup, "correct horse battery staple")

    assert report.files == imported.files
    assert stat.S_IMODE(backup.stat().st_mode) == 0o600
    assert (target_data / "artifacts" / "ab" / "result.txt").read_text() == "artifact content"
    with sqlite3.connect(target_data / "journal.sqlite3") as database:
        assert database.execute("SELECT value FROM live").fetchone() == ("committed while open",)
    restored_config = (target_data / "config.json").read_text()
    assert "never-back-this-up" not in restored_config
    assert "embedded-token" not in restored_config
    assert "embedded-header" not in restored_config
    assert "MODEL_API_KEY" in restored_config
    assert not (target_data / "api-token").exists()
    assert stat.S_IMODE((target_data / "journal.sqlite3").stat().st_mode) == 0o600


def test_wrong_password_tamper_and_existing_state_fail_explicitly(tmp_path: Path) -> None:
    source, connection = source_state(tmp_path)
    backup = tmp_path / "state.polaris-backup"
    try:
        source.export(backup, "right password")
    finally:
        connection.close()

    target = BackupManager(data_dir=tmp_path / "target")
    with pytest.raises(BackupAuthenticationError, match="authentication"):
        target.import_archive(backup, "wrong password")

    tampered = tmp_path / "tampered.polaris-backup"
    payload = bytearray(backup.read_bytes())
    payload[-20] ^= 1
    tampered.write_bytes(payload)
    with pytest.raises(BackupAuthenticationError, match="modified"):
        target.import_archive(tampered, "right password")

    existing = tmp_path / "existing"
    existing.mkdir()
    (existing / "keep").write_text("state")
    with pytest.raises(ExistingStateError, match="--force"):
        BackupManager(data_dir=existing).import_archive(backup, "right password")


def test_import_rejects_traversal_even_inside_authenticated_envelope(tmp_path: Path) -> None:
    raw = tmp_path / "malicious.tar"
    with tarfile.open(raw, "w") as bundle:
        content = b"escape"
        member = tarfile.TarInfo("../escape")
        member.size = len(content)
        bundle.addfile(member, io.BytesIO(content))
    encrypted = tmp_path / "malicious.polaris-backup"
    _encrypt_file(raw, encrypted, "password")

    with pytest.raises(BackupFormatError, match="unsafe archive path"):
        BackupManager(data_dir=tmp_path / "target").import_archive(encrypted, "password")
    assert not (tmp_path / "escape").exists()


def _replacement_tree(tmp_path: Path) -> Path:
    replacement = tmp_path / f".polaris-import-{'1' * 32}" / "extracted" / "state"
    replacement.mkdir(parents=True)
    (replacement / "config.json").write_text("{}")
    (replacement / "journal.sqlite3").write_bytes(b"new journal")
    artifacts = replacement / "artifacts" / "nested"
    artifacts.mkdir(parents=True)
    (artifacts / "result.txt").write_text("new artifact")
    return replacement


def test_restore_recovers_crash_after_moving_old_state(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    data = tmp_path / "data"
    data.mkdir()
    (data / "state.txt").write_text("old state")
    replacement = _replacement_tree(tmp_path)
    manager = BackupManager(data_dir=data)

    def crash_after_old_move(source: Path, destination: Path) -> None:
        os.replace(source, destination)
        if source == data:
            raise SimulatedRestoreCrash

    monkeypatch.setattr(manager, "_rename", crash_after_old_move)
    with pytest.raises(SimulatedRestoreCrash):
        manager._replace_state(replacement)

    marker = manager._restore_marker
    transaction = json.loads(marker.read_text())
    assert transaction["phase"] == "prepared"
    assert transaction["target"] == str(data)
    assert transaction["old"]
    assert transaction["replacement"] == str(replacement)
    assert stat.S_IMODE(marker.stat().st_mode) == 0o600
    assert not data.exists()

    recovered = BackupManager(data_dir=data)
    assert (recovered.data_dir / "state.txt").read_text() == "old state"
    assert not marker.exists()
    assert not replacement.parents[1].exists()
    assert not list(tmp_path.glob(".data.backup-*"))


def test_restore_recovers_crash_after_installing_new_state(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    data = tmp_path / "data"
    data.mkdir()
    (data / "state.txt").write_text("old state")
    replacement = _replacement_tree(tmp_path)
    external_config = tmp_path / "external-config.json"
    external_config.write_text('{"old": true}')
    manager = BackupManager(data_dir=data, config_file=external_config)

    def crash_after_new_install(source: Path, destination: Path) -> None:
        os.replace(source, destination)
        if destination == data and source == replacement:
            raise SimulatedRestoreCrash

    monkeypatch.setattr(manager, "_rename", crash_after_new_install)
    with pytest.raises(SimulatedRestoreCrash):
        manager._replace_state(replacement)

    marker = manager._restore_marker
    assert json.loads(marker.read_text())["phase"] == "old_moved"
    assert (data / "journal.sqlite3").read_bytes() == b"new journal"
    assert list(tmp_path.glob(".data.backup-*"))

    recovered = BackupManager(data_dir=data, config_file=external_config)
    assert (recovered.data_dir / "journal.sqlite3").read_bytes() == b"new journal"
    assert json.loads(external_config.read_text()) == {}
    assert not marker.exists()
    assert not replacement.parents[1].exists()
    assert not list(tmp_path.glob(".data.backup-*"))


def test_restore_fsyncs_every_staged_file_and_directory(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    replacement = _replacement_tree(tmp_path)
    files: list[Path] = []
    directories: list[Path] = []
    monkeypatch.setattr(BackupManager, "_fsync_file", staticmethod(files.append))
    monkeypatch.setattr(BackupManager, "_fsync_directory", staticmethod(directories.append))

    BackupManager._fsync_tree(replacement)

    assert set(files) == {path for path in replacement.rglob("*") if path.is_file()}
    assert set(directories) == {
        replacement,
        *(path for path in replacement.rglob("*") if path.is_dir()),
    }
    assert directories[-1] == replacement
