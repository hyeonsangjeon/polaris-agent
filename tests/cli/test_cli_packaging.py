from __future__ import annotations

from importlib import import_module
from pathlib import Path

import pytest
from typer.testing import CliRunner

from polaris.backup import BackupReport
from polaris.cli.main import app

runner = CliRunner()
cli_main = import_module("polaris.cli.main")


class FakeServiceManager:
    def __init__(self, path: Path) -> None:
        self.path = path
        self.calls: list[str] = []

    def install(self) -> Path:
        self.calls.append("install")
        return self.path

    def start(self) -> None:
        self.calls.append("start")

    def stop(self) -> None:
        self.calls.append("stop")

    def uninstall(self) -> None:
        self.calls.append("uninstall")


class FakeBackupManager:
    def __init__(self) -> None:
        self.calls: list[tuple[object, ...]] = []

    def export(self, path: Path, passphrase: str) -> BackupReport:
        self.calls.append(("export", path, passphrase))
        return BackupReport(path, 3, 100)

    def import_archive(self, path: Path, passphrase: str, *, force: bool) -> BackupReport:
        self.calls.append(("import", path, passphrase, force))
        return BackupReport(path, 3, 100)


def test_daemon_packaging_commands_call_manager(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    service = FakeServiceManager(tmp_path / "agent.plist")
    monkeypatch.setattr(cli_main, "_service_manager", lambda _state: service)
    for command in ("install", "start", "stop", "uninstall"):
        result = runner.invoke(app, ["daemon", command])
        assert result.exit_code == 0, result.output
    assert service.calls == ["install", "start", "stop", "uninstall"]


def test_backup_commands_use_environment_passphrase_without_echo(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    backup = FakeBackupManager()
    archive = tmp_path / "state.polaris-backup"
    archive.touch()
    monkeypatch.setattr(cli_main, "_backup_manager", lambda _state: backup)
    monkeypatch.setenv("TEST_BACKUP_PASSWORD", "top-secret-password")

    exported = runner.invoke(
        app,
        [
            "backup",
            "export",
            str(tmp_path / "new.polaris-backup"),
            "--passphrase-env",
            "TEST_BACKUP_PASSWORD",
        ],
    )
    imported = runner.invoke(
        app,
        [
            "backup",
            "import",
            str(archive),
            "--force",
            "--passphrase-env",
            "TEST_BACKUP_PASSWORD",
        ],
    )
    assert exported.exit_code == imported.exit_code == 0
    assert "top-secret-password" not in exported.output + imported.output
    assert backup.calls == [
        ("export", tmp_path / "new.polaris-backup", "top-secret-password"),
        ("import", archive, "top-secret-password", True),
    ]
