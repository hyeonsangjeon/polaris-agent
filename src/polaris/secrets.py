"""Owner-only runtime secret storage."""

from __future__ import annotations

import errno
import os
import re
import stat
import time
import uuid
from collections.abc import Iterator, Mapping, Set
from contextlib import contextmanager, suppress
from pathlib import Path

MAX_SECRETS_FILE_SIZE = 64 * 1024
_ENV_NAME = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")


class SecretsFileError(ValueError):
    """A runtime secrets file is missing, unsafe, or malformed."""


class _SecretValues(dict[str, str]):
    def __repr__(self) -> str:
        names = ", ".join(repr(name) for name in sorted(self))
        return f"<runtime secrets: {names}>"


def validate_secret_name(name: str) -> str:
    if _ENV_NAME.fullmatch(name) is None:
        raise SecretsFileError("secret name must be a valid environment variable name")
    return name


def validate_secret_value(value: str) -> str:
    if "\x00" in value or "\r" in value or "\n" in value:
        raise SecretsFileError("secret value must be a single NUL-free line")
    if "$(" in value or "`" in value:
        raise SecretsFileError("secret value must not contain command expansion syntax")
    return value


def parse_secrets(payload: bytes) -> dict[str, str]:
    """Parse a bounded, non-interpolating KEY=VALUE payload."""
    if len(payload) > MAX_SECRETS_FILE_SIZE:
        raise SecretsFileError("runtime secrets file exceeds 64 KiB")
    try:
        text = payload.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise SecretsFileError("runtime secrets file is not valid UTF-8") from exc
    if "\x00" in text:
        raise SecretsFileError("runtime secrets file contains a NUL byte")
    if "\r" in text:
        raise SecretsFileError("runtime secrets file must use LF line endings")

    values: dict[str, str] = _SecretValues()
    for line_number, line in enumerate(text.split("\n"), 1):
        if not line or line.lstrip().startswith("#"):
            continue
        if line.startswith("export ") or "=" not in line:
            raise SecretsFileError(f"invalid assignment on line {line_number}")
        name, value = line.split("=", 1)
        try:
            validate_secret_name(name)
            validate_secret_value(value)
        except SecretsFileError as exc:
            raise SecretsFileError(f"invalid secret on line {line_number}: {exc}") from exc
        if name in values:
            raise SecretsFileError(f"duplicate secret name {name!r}")
        values[name] = value
    return values


def _serialize(values: Mapping[str, str]) -> bytes:
    checked: dict[str, str] = {}
    for name, value in values.items():
        validate_secret_name(name)
        validate_secret_value(value)
        if name in checked:
            raise SecretsFileError(f"duplicate secret name {name!r}")
        checked[name] = value
    payload = "".join(f"{name}={checked[name]}\n" for name in sorted(checked)).encode("utf-8")
    if len(payload) > MAX_SECRETS_FILE_SIZE:
        raise SecretsFileError("runtime secrets file exceeds 64 KiB")
    return payload


class SecretsFile:
    """Read and atomically update an owner-only runtime environment file."""

    def __init__(self, path: str | Path, *, lock_timeout_seconds: float = 10.0) -> None:
        if lock_timeout_seconds < 0:
            raise ValueError("lock_timeout_seconds must be non-negative")
        expanded = Path(path).expanduser()
        self.path = expanded if expanded.is_absolute() else Path.cwd() / expanded
        self.lock_timeout_seconds = lock_timeout_seconds

    def __repr__(self) -> str:
        return f"{type(self).__name__}(path={self.path!r})"

    def read(self, *, missing_ok: bool = False) -> dict[str, str]:
        try:
            before = self.path.lstat()
        except FileNotFoundError:
            if missing_ok:
                return _SecretValues()
            raise SecretsFileError(f"runtime secrets file does not exist at {self.path}") from None
        except OSError as exc:
            raise SecretsFileError(f"runtime secrets file cannot be inspected: {exc}") from exc
        self._validate_metadata(before)

        flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0)
        try:
            descriptor = os.open(self.path, flags)
        except OSError as exc:
            raise SecretsFileError(f"runtime secrets file cannot be opened safely: {exc}") from exc
        try:
            current = os.fstat(descriptor)
            self._validate_metadata(current)
            if (before.st_dev, before.st_ino) != (current.st_dev, current.st_ino):
                raise SecretsFileError("runtime secrets file changed while it was being opened")
            chunks: list[bytes] = []
            remaining = MAX_SECRETS_FILE_SIZE + 1
            while remaining:
                chunk = os.read(descriptor, remaining)
                if not chunk:
                    break
                chunks.append(chunk)
                remaining -= len(chunk)
            payload = b"".join(chunks)
        finally:
            os.close(descriptor)
        return parse_secrets(payload)

    def names(self, *, missing_ok: bool = False) -> tuple[str, ...]:
        return tuple(sorted(self.read(missing_ok=missing_ok)))

    def set(self, name: str, value: str) -> None:
        validate_secret_name(name)
        validate_secret_value(value)
        with self._exclusive_lock():
            values = self.read(missing_ok=True)
            values[name] = value
            self._write(values)

    def remove(self, name: str) -> bool:
        validate_secret_name(name)
        with self._exclusive_lock():
            values = self.read(missing_ok=True)
            if name not in values:
                return False
            del values[name]
            self._write(values)
            return True

    def check(
        self,
        required: Set[str] = frozenset(),
        *,
        missing_ok: bool = False,
    ) -> tuple[str, ...]:
        values = self.read(missing_ok=missing_ok)
        present = {name for name, value in values.items() if value}
        return tuple(sorted(required - present))

    def _validate_metadata(self, metadata: os.stat_result) -> None:
        if not stat.S_ISREG(metadata.st_mode):
            raise SecretsFileError("runtime secrets file must be a regular file, not a symlink")
        getuid = getattr(os, "geteuid", None)
        if getuid is not None and metadata.st_uid != getuid():
            raise SecretsFileError("runtime secrets file must be owned by the current user")
        mode = stat.S_IMODE(metadata.st_mode)
        if os.name != "nt" and mode != 0o600:
            raise SecretsFileError(
                f"runtime secrets file permissions must be 0600, not {mode:04o}"
            )
        if metadata.st_size > MAX_SECRETS_FILE_SIZE:
            raise SecretsFileError("runtime secrets file exceeds 64 KiB")

    def _write(self, values: Mapping[str, str]) -> None:
        payload = _serialize(values)
        parent = self.path.parent
        parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        temporary = parent / f".{self.path.name}.{uuid.uuid4().hex}.tmp"
        descriptor = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        try:
            if hasattr(os, "fchmod"):
                os.fchmod(descriptor, 0o600)
            with os.fdopen(descriptor, "wb") as handle:
                handle.write(payload)
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temporary, self.path)
            self._fsync_directory(parent)
        finally:
            temporary.unlink(missing_ok=True)

    @property
    def _lock_path(self) -> Path:
        return self.path.parent / f".{self.path.name}.lock"

    @contextmanager
    def _exclusive_lock(self) -> Iterator[None]:
        parent = self.path.parent
        parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        flags = os.O_RDWR | os.O_CREAT | getattr(os, "O_NOFOLLOW", 0)
        try:
            descriptor = os.open(self._lock_path, flags, 0o600)
        except OSError as exc:
            raise SecretsFileError(f"runtime secrets lock file cannot be opened: {exc}") from exc
        acquired = False
        try:
            if hasattr(os, "fchmod"):
                os.fchmod(descriptor, 0o600)
            metadata = os.fstat(descriptor)
            if not stat.S_ISREG(metadata.st_mode):
                raise SecretsFileError("runtime secrets lock must be a regular file")
            getuid = getattr(os, "geteuid", None)
            if getuid is not None and metadata.st_uid != getuid():
                raise SecretsFileError("runtime secrets lock must be owned by the current user")
            if os.name != "nt" and stat.S_IMODE(metadata.st_mode) != 0o600:
                raise SecretsFileError("runtime secrets lock permissions must be 0600")
            if metadata.st_size == 0:
                os.write(descriptor, b"\0")
                os.fsync(descriptor)

            deadline = time.monotonic() + self.lock_timeout_seconds
            while True:
                try:
                    self._try_lock(descriptor)
                    acquired = True
                    break
                except OSError as exc:
                    if exc.errno not in {errno.EACCES, errno.EAGAIN, errno.EDEADLK}:
                        raise SecretsFileError(
                            f"runtime secrets file cannot be locked: {exc}"
                        ) from exc
                    remaining = deadline - time.monotonic()
                    if remaining <= 0:
                        raise SecretsFileError(
                            "timed out waiting for runtime secrets file lock"
                        ) from None
                    time.sleep(min(0.05, remaining))
            yield
        finally:
            if acquired:
                with suppress(OSError):
                    self._unlock(descriptor)
            os.close(descriptor)

    @staticmethod
    def _try_lock(descriptor: int) -> None:
        if os.name == "nt":
            import msvcrt

            os.lseek(descriptor, 0, os.SEEK_SET)
            msvcrt.locking(descriptor, msvcrt.LK_NBLCK, 1)  # type: ignore[attr-defined]
            return
        import fcntl

        fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)

    @staticmethod
    def _unlock(descriptor: int) -> None:
        if os.name == "nt":
            import msvcrt

            os.lseek(descriptor, 0, os.SEEK_SET)
            msvcrt.locking(descriptor, msvcrt.LK_UNLCK, 1)  # type: ignore[attr-defined]
            return
        import fcntl

        fcntl.flock(descriptor, fcntl.LOCK_UN)

    @staticmethod
    def _fsync_directory(path: Path) -> None:
        if os.name == "nt":
            return
        descriptor = os.open(path, os.O_RDONLY)
        try:
            os.fsync(descriptor)
        finally:
            os.close(descriptor)


def runtime_environment(
    secrets_file: str | Path,
    environ: Mapping[str, str] | None = None,
) -> dict[str, str]:
    """Load file values, then overlay the process environment."""
    values = SecretsFile(secrets_file).read(missing_ok=True)
    values.update(os.environ if environ is None else environ)
    return values


__all__ = [
    "MAX_SECRETS_FILE_SIZE",
    "SecretsFile",
    "SecretsFileError",
    "parse_secrets",
    "runtime_environment",
    "validate_secret_name",
    "validate_secret_value",
]
