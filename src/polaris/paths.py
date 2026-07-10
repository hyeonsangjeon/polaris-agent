"""Filesystem locations used by the local daemon."""

from __future__ import annotations

import os
from contextlib import suppress
from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True, slots=True)
class PolarisPaths:
    data_dir: Path
    config_file: Path
    journal_file: Path
    artifact_dir: Path
    token_file: Path

    @classmethod
    def from_env(cls, env: dict[str, str] | None = None) -> PolarisPaths:
        values = os.environ if env is None else env
        home_override = values.get("POLARIS_HOME")
        if home_override:
            data_dir = Path(home_override).expanduser()
        elif values.get("XDG_DATA_HOME"):
            data_dir = Path(values["XDG_DATA_HOME"]).expanduser() / "polaris"
        else:
            data_dir = Path.home() / ".local" / "share" / "polaris"
        data_dir = data_dir.resolve()

        if values.get("POLARIS_CONFIG"):
            config_file = Path(values["POLARIS_CONFIG"]).expanduser().resolve()
        elif home_override:
            config_file = data_dir / "config.json"
        else:
            config_home = Path(
                values.get("XDG_CONFIG_HOME", str(Path.home() / ".config"))
            ).expanduser()
            config_file = (config_home / "polaris" / "config.json").resolve()
        return cls(
            data_dir=data_dir,
            config_file=config_file,
            journal_file=data_dir / "journal.sqlite3",
            artifact_dir=data_dir / "artifacts",
            token_file=data_dir / "api-token",
        )

    def ensure(self) -> None:
        self.data_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
        self.config_file.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        self.artifact_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
        for directory in (self.data_dir, self.config_file.parent, self.artifact_dir):
            with suppress(OSError):
                directory.chmod(0o700)


def default_paths() -> PolarisPaths:
    return PolarisPaths.from_env()
