"""Manage the per-user Polaris daemon with macOS launchd."""

from __future__ import annotations

import os
import plistlib
import subprocess
import sys
import uuid
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from platform import system

from polaris.secrets import SecretsFile, SecretsFileError

LABEL = "com.hyeonsangjeon.polaris.daemon"
PLIST_NAME = f"{LABEL}.plist"


class ServiceManagerError(RuntimeError):
    """A local service operation failed."""


class UnsupportedServicePlatformError(ServiceManagerError):
    """The current operating system has no supported service manager."""


Runner = Callable[[Sequence[str]], subprocess.CompletedProcess[str]]


@dataclass(frozen=True, slots=True)
class ServiceStatus:
    loaded: bool
    detail: str


def _default_runner(arguments: Sequence[str]) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        arguments,
        capture_output=True,
        text=True,
        check=False,
    )


class LaunchdServiceManager:
    """Install and control an owner-only LaunchAgent."""

    def __init__(
        self,
        *,
        data_dir: Path,
        config_file: Path | None = None,
        runner: Runner = _default_runner,
        uid: int | None = None,
        home: Path | None = None,
        python_executable: str | None = None,
        provider_api_key_envs: Mapping[str, str] | None = None,
        secrets_file: Path | None = None,
        platform_system: Callable[[], str] = system,
    ) -> None:
        if platform_system() != "Darwin":
            raise UnsupportedServicePlatformError(
                "daemon service management is unsupported on this platform; "
                "systemd support is planned for v0.2"
            )
        self.data_dir = data_dir.expanduser().resolve()
        self.config_file = (
            config_file.expanduser().resolve()
            if config_file is not None
            else self.data_dir / "config.json"
        )
        self.runner = runner
        self.uid = os.getuid() if uid is None else uid
        self.home = (Path.home() if home is None else home).expanduser().resolve()
        self.python_executable = python_executable or sys.executable
        self.provider_api_key_envs = dict(provider_api_key_envs or {})
        if secrets_file is None:
            self.secrets_file = None
        else:
            expanded = secrets_file.expanduser()
            self.secrets_file = (
                expanded if expanded.is_absolute() else self.data_dir / expanded
            ).absolute()

    @property
    def domain(self) -> str:
        return f"gui/{self.uid}"

    @property
    def target(self) -> str:
        return f"{self.domain}/{LABEL}"

    @property
    def plist_path(self) -> Path:
        return self.home / "Library" / "LaunchAgents" / PLIST_NAME

    def plist_payload(self) -> dict[str, object]:
        logs = self.data_dir / "logs"
        environment = {"POLARIS_HOME": str(self.data_dir)}
        if self.secrets_file is not None:
            environment["POLARIS_SECRETS_FILE"] = str(self.secrets_file)
        return {
            "Label": LABEL,
            "ProgramArguments": [
                self.python_executable,
                "-m",
                "polaris.daemon.main",
                "--config",
                str(self.config_file),
            ],
            "EnvironmentVariables": environment,
            "RunAtLoad": True,
            "KeepAlive": {"SuccessfulExit": False},
            "StandardOutPath": str(logs / "daemon.stdout.log"),
            "StandardErrorPath": str(logs / "daemon.stderr.log"),
            "ProcessType": "Background",
            "ThrottleInterval": 10,
        }

    def install(self) -> Path:
        required = set(self.provider_api_key_envs.values())
        if self.secrets_file is None:
            missing = sorted(required)
        else:
            try:
                missing = list(
                    SecretsFile(self.secrets_file).check(
                        required,
                        missing_ok=not required,
                    )
                )
            except SecretsFileError as exc:
                if required:
                    names = ", ".join(sorted(required))
                    commands = "; ".join(
                        f"polaris secrets set {name}" for name in sorted(required)
                    )
                    raise ServiceManagerError(
                        f"{exc}. Required names: {names}. Run: {commands}"
                    ) from exc
                raise ServiceManagerError(str(exc)) from exc
        if missing:
            names = ", ".join(missing)
            commands = "; ".join(f"polaris secrets set {name}" for name in missing)
            raise ServiceManagerError(
                f"runtime secrets file is missing required names: {names}. Run: {commands}"
            )
        self._write_plist()
        self._run("bootstrap", self.domain, str(self.plist_path))
        return self.plist_path

    def uninstall(self) -> None:
        if self.plist_path.exists():
            self._run("bootout", self.domain, str(self.plist_path))
            self.plist_path.unlink()
            self._fsync_directory(self.plist_path.parent)

    def start(self) -> None:
        status = self.status()
        if not status.loaded:
            if not self.plist_path.exists():
                raise ServiceManagerError(f"LaunchAgent is not installed at {self.plist_path}")
            self._run("bootstrap", self.domain, str(self.plist_path))
        self._run("kickstart", "-k", self.target)

    def stop(self) -> None:
        self._run("bootout", self.target)

    def status(self) -> ServiceStatus:
        result = self._invoke("print", self.target)
        if result.returncode == 0:
            return ServiceStatus(True, result.stdout.strip())
        detail = (result.stderr or result.stdout).strip()
        if result.returncode in {3, 113} or "Could not find service" in detail:
            return ServiceStatus(False, detail or "service is not loaded")
        raise ServiceManagerError(
            f"launchctl print failed ({result.returncode}): {detail or 'unknown error'}"
        )

    def _write_plist(self) -> None:
        parent = self.plist_path.parent
        logs = self.data_dir / "logs"
        parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        self.data_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
        logs.mkdir(parents=True, exist_ok=True, mode=0o700)
        parent.chmod(0o700)
        self.data_dir.chmod(0o700)
        logs.chmod(0o700)
        payload = plistlib.dumps(self.plist_payload(), fmt=plistlib.FMT_XML, sort_keys=True)
        temporary = parent / f".{PLIST_NAME}.{uuid.uuid4().hex}.tmp"
        descriptor = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        try:
            with os.fdopen(descriptor, "wb") as handle:
                handle.write(payload)
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temporary, self.plist_path)
            self.plist_path.chmod(0o600)
            self._fsync_directory(parent)
        finally:
            temporary.unlink(missing_ok=True)

    def _run(self, *arguments: str) -> subprocess.CompletedProcess[str]:
        result = self._invoke(*arguments)
        if result.returncode != 0:
            detail = (result.stderr or result.stdout).strip()
            raise ServiceManagerError(
                f"launchctl {arguments[0]} failed ({result.returncode}): "
                f"{detail or 'unknown error'}"
            )
        return result

    def _invoke(self, *arguments: str) -> subprocess.CompletedProcess[str]:
        try:
            return self.runner(["launchctl", *arguments])
        except OSError as exc:
            raise ServiceManagerError(f"could not execute launchctl: {exc}") from exc

    @staticmethod
    def _fsync_directory(path: Path) -> None:
        descriptor = os.open(path, os.O_RDONLY)
        try:
            os.fsync(descriptor)
        finally:
            os.close(descriptor)


__all__ = [
    "LABEL",
    "PLIST_NAME",
    "LaunchdServiceManager",
    "ServiceManagerError",
    "ServiceStatus",
    "UnsupportedServicePlatformError",
]
