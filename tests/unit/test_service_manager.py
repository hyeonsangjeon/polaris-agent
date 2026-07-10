from __future__ import annotations

import plistlib
import stat
import subprocess
from collections.abc import Sequence
from pathlib import Path

import pytest

from polaris.daemon.service_manager import (
    LABEL,
    LaunchdServiceManager,
    ServiceManagerError,
    UnsupportedServicePlatformError,
)


class RecordingRunner:
    def __init__(self) -> None:
        self.commands: list[list[str]] = []
        self.results: list[subprocess.CompletedProcess[str]] = []

    def __call__(self, arguments: Sequence[str]) -> subprocess.CompletedProcess[str]:
        command = list(arguments)
        self.commands.append(command)
        if self.results:
            return self.results.pop(0)
        return subprocess.CompletedProcess(command, 0, "", "")


def manager(tmp_path: Path, runner: RecordingRunner) -> LaunchdServiceManager:
    return LaunchdServiceManager(
        data_dir=tmp_path / "data",
        runner=runner,
        uid=501,
        home=tmp_path / "home",
        python_executable="/private/venv/bin/python",
        platform_system=lambda: "Darwin",
    )


def test_install_writes_private_secret_free_plist_and_bootstraps(tmp_path: Path) -> None:
    runner = RecordingRunner()
    service = manager(tmp_path, runner)
    path = service.install()

    assert stat.S_IMODE(path.stat().st_mode) == 0o600
    payload = plistlib.loads(path.read_bytes())
    assert payload["Label"] == LABEL
    assert payload["ProgramArguments"] == [
        "/private/venv/bin/python",
        "-m",
        "polaris.daemon.main",
        "--config",
        str((tmp_path / "data" / "config.json").resolve()),
    ]
    assert payload["EnvironmentVariables"] == {"POLARIS_HOME": str((tmp_path / "data").resolve())}
    assert payload["RunAtLoad"] is True
    assert payload["KeepAlive"] == {"SuccessfulExit": False}
    assert payload["ProcessType"] == "Background"
    assert payload["ThrottleInterval"] == 10
    serialized = path.read_text()
    assert "api-token" not in serialized
    assert "bearer" not in serialized.lower()
    assert "secret" not in serialized.lower()
    assert runner.commands == [["launchctl", "bootstrap", "gui/501", str(path)]]
    assert not list(path.parent.glob("*.tmp"))


def test_install_rejects_api_key_environment_without_writing_plist(tmp_path: Path) -> None:
    runner = RecordingRunner()
    service = LaunchdServiceManager(
        data_dir=tmp_path / "data",
        provider_api_key_envs={"foundry": "AZURE_FOUNDRY_API_KEY"},
        runner=runner,
        uid=501,
        home=tmp_path / "home",
        python_executable="/private/venv/bin/python",
        platform_system=lambda: "Darwin",
    )

    with pytest.raises(ServiceManagerError, match="foreground"):
        service.install()

    assert not service.plist_path.exists()
    assert runner.commands == []


def test_start_stop_status_and_uninstall_use_exact_gui_domain(tmp_path: Path) -> None:
    runner = RecordingRunner()
    service = manager(tmp_path, runner)
    service.install()
    runner.commands.clear()
    runner.results.append(subprocess.CompletedProcess([], 113, "", "Could not find service"))
    service.start()
    service.stop()
    service.uninstall()

    assert runner.commands == [
        ["launchctl", "print", f"gui/501/{LABEL}"],
        ["launchctl", "bootstrap", "gui/501", str(service.plist_path)],
        ["launchctl", "kickstart", "-k", f"gui/501/{LABEL}"],
        ["launchctl", "bootout", f"gui/501/{LABEL}"],
        ["launchctl", "bootout", "gui/501", str(service.plist_path)],
    ]
    assert not service.plist_path.exists()


def test_launchctl_failures_and_linux_are_explicit(tmp_path: Path) -> None:
    runner = RecordingRunner()
    runner.results.append(subprocess.CompletedProcess([], 5, "", "permission denied"))
    with pytest.raises(ServiceManagerError, match="permission denied"):
        manager(tmp_path, runner).install()

    with pytest.raises(UnsupportedServicePlatformError, match="systemd"):
        LaunchdServiceManager(
            data_dir=tmp_path,
            platform_system=lambda: "Linux",
        )
