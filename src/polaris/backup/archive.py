"""Create and restore encrypted Polaris state archives."""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import sqlite3
import struct
import tarfile
import uuid
from collections.abc import Iterator
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path, PurePosixPath
from typing import Any, BinaryIO

from cryptography.exceptions import InvalidTag
from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes
from cryptography.hazmat.primitives.kdf.scrypt import Scrypt

MAGIC = b"POLARIS-BACKUP\x00\x00"
VERSION = 1
SALT_SIZE = 16
NONCE_SIZE = 12
TAG_SIZE = 16
HEADER = struct.Struct(f">{len(MAGIC)}sB{SALT_SIZE}s{NONCE_SIZE}s")
CHUNK_SIZE = 1024 * 1024
MANIFEST_NAME = "manifest.json"
STATE_DIRECTORY = "state"
RESTORE_TRANSACTION_VERSION = 1
RESTORE_PHASES = {
    "prepared",
    "old_moved",
    "data_installed",
    "config_prepared",
    "config_installed",
}


class BackupError(RuntimeError):
    """Base error for backup operations."""


class BackupAuthenticationError(BackupError):
    """The passphrase is wrong or the encrypted archive was modified."""


class BackupFormatError(BackupError):
    """The archive structure or manifest is invalid."""


class ExistingStateError(BackupError):
    """Import would overwrite existing local state."""


@dataclass(frozen=True, slots=True)
class BackupReport:
    path: Path
    files: int
    bytes: int


@dataclass(slots=True)
class _RestoreTransaction:
    target: Path
    old: Path
    replacement: Path
    phase: str
    target_existed: bool
    config_file: Path | None = None
    old_config: Path | None = None
    staged_config: Path | None = None
    config_existed: bool = False


def _derive_key(passphrase: str, salt: bytes) -> bytes:
    if not passphrase:
        raise ValueError("passphrase must not be empty")
    return Scrypt(salt=salt, length=32, n=2**15, r=8, p=1).derive(passphrase.encode("utf-8"))


def _chunks(handle: BinaryIO, *, limit: int | None = None) -> Iterator[bytes]:
    remaining = limit
    while remaining is None or remaining > 0:
        size = CHUNK_SIZE if remaining is None else min(CHUNK_SIZE, remaining)
        chunk = handle.read(size)
        if not chunk:
            break
        if remaining is not None:
            remaining -= len(chunk)
        yield chunk
    if remaining not in {None, 0}:
        raise BackupFormatError("encrypted backup is truncated")


def _encrypt_file(source: Path, destination: Path, passphrase: str) -> None:
    salt = os.urandom(SALT_SIZE)
    nonce = os.urandom(NONCE_SIZE)
    header = HEADER.pack(MAGIC, VERSION, salt, nonce)
    encryptor = Cipher(algorithms.AES(_derive_key(passphrase, salt)), modes.GCM(nonce)).encryptor()
    encryptor.authenticate_additional_data(header)
    with source.open("rb") as input_handle, destination.open("xb") as output_handle:
        os.chmod(destination, 0o600)
        output_handle.write(header)
        for chunk in _chunks(input_handle):
            output_handle.write(encryptor.update(chunk))
        output_handle.write(encryptor.finalize())
        output_handle.write(encryptor.tag)
        output_handle.flush()
        os.fsync(output_handle.fileno())


def _decrypt_file(source: Path, destination: Path, passphrase: str) -> None:
    size = source.stat().st_size
    if size < HEADER.size + TAG_SIZE:
        raise BackupFormatError("encrypted backup is too short")
    with source.open("rb") as input_handle:
        header = input_handle.read(HEADER.size)
        try:
            magic, version, salt, nonce = HEADER.unpack(header)
        except struct.error as exc:
            raise BackupFormatError("invalid backup header") from exc
        if magic != MAGIC:
            raise BackupFormatError("backup magic is invalid")
        if version != VERSION:
            raise BackupFormatError(f"unsupported backup version: {version}")
        input_handle.seek(-TAG_SIZE, os.SEEK_END)
        tag = input_handle.read(TAG_SIZE)
        input_handle.seek(HEADER.size)
        ciphertext_size = size - HEADER.size - TAG_SIZE
        decryptor = Cipher(
            algorithms.AES(_derive_key(passphrase, salt)), modes.GCM(nonce, tag)
        ).decryptor()
        decryptor.authenticate_additional_data(header)
        try:
            with destination.open("xb") as output_handle:
                os.chmod(destination, 0o600)
                for chunk in _chunks(input_handle, limit=ciphertext_size):
                    output_handle.write(decryptor.update(chunk))
                output_handle.write(decryptor.finalize())
                output_handle.flush()
                os.fsync(output_handle.fileno())
        except InvalidTag as exc:
            destination.unlink(missing_ok=True)
            raise BackupAuthenticationError(
                "backup authentication failed: wrong passphrase or modified archive"
            ) from exc


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in _chunks(handle):
            digest.update(chunk)
    return digest.hexdigest()


def _write_private(path: Path, payload: bytes) -> None:
    descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(descriptor, "wb") as handle:
        handle.write(payload)
        handle.flush()
        os.fsync(handle.fileno())


def _sanitize_config(value: Any) -> Any:
    if isinstance(value, dict):
        result: dict[str, Any] = {}
        for key, item in value.items():
            lowered = str(key).lower().replace("-", "_")
            if lowered in {
                "api_key",
                "api_token",
                "bearer_token",
                "password",
                "secret",
                "token",
            }:
                continue
            if lowered == "headers" and isinstance(item, dict):
                item = {
                    header: content
                    for header, content in item.items()
                    if str(header).lower() not in {"authorization", "api-key", "x-api-key"}
                }
            result[str(key)] = _sanitize_config(item)
        return result
    if isinstance(value, list):
        return [_sanitize_config(item) for item in value]
    return value


class BackupManager:
    """Export and import config, a live SQLite snapshot, and artifacts."""

    def __init__(
        self,
        *,
        data_dir: Path,
        config_file: Path | None = None,
        journal_file: Path | None = None,
        artifact_dir: Path | None = None,
        secrets_file: Path | None = None,
    ) -> None:
        self.data_dir = data_dir.expanduser().resolve()
        self.config_file = (
            config_file.expanduser().resolve()
            if config_file is not None
            else self.data_dir / "config.json"
        )
        self.journal_file = (
            journal_file.expanduser().resolve()
            if journal_file is not None
            else self.data_dir / "journal.sqlite3"
        )
        self.artifact_dir = (
            artifact_dir.expanduser().resolve()
            if artifact_dir is not None
            else self.data_dir / "artifacts"
        )
        self.secrets_file = (
            secrets_file.expanduser().resolve()
            if secrets_file is not None
            else self.data_dir / "runtime-secrets.env"
        )
        self._restore_marker = self.data_dir.parent / (
            f".{self.data_dir.name}.restore-transaction.json"
        )
        self._recover_restore_transaction()

    def export(self, destination: Path, passphrase: str) -> BackupReport:
        destination = destination.expanduser().resolve()
        destination.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        if destination.exists():
            raise FileExistsError(f"backup already exists at {destination}")
        workspace = destination.parent / f".polaris-export-{uuid.uuid4().hex}"
        workspace.mkdir(mode=0o700)
        encrypted = destination.parent / f".{destination.name}.{uuid.uuid4().hex}.tmp"
        try:
            state = workspace / STATE_DIRECTORY
            state.mkdir(mode=0o700)
            self._stage_config(state / "config.json")
            self._snapshot_database(state / "journal.sqlite3")
            self._stage_artifacts(state / "artifacts")
            files = self._manifest_files(state)
            manifest = {
                "format": "polaris-backup",
                "version": VERSION,
                "created_at": datetime.now(UTC).isoformat(),
                "credentials_included": False,
                "credentials_notice": (
                    "API tokens and environment secrets are excluded; credentials "
                    "must be re-established after import."
                ),
                "files": files,
            }
            _write_private(
                workspace / MANIFEST_NAME,
                json.dumps(manifest, indent=2, sort_keys=True).encode("utf-8") + b"\n",
            )
            archive = workspace / "payload.tar"
            with tarfile.open(archive, "w") as bundle:
                bundle.add(workspace / MANIFEST_NAME, arcname=MANIFEST_NAME, recursive=False)
                bundle.add(state, arcname=STATE_DIRECTORY, recursive=True)
            _encrypt_file(archive, encrypted, passphrase)
            os.replace(encrypted, destination)
            destination.chmod(0o600)
            self._fsync_directory(destination.parent)
            return BackupReport(
                destination,
                len(files),
                self._total_size(files),
            )
        finally:
            encrypted.unlink(missing_ok=True)
            shutil.rmtree(workspace, ignore_errors=True)

    def import_archive(self, source: Path, passphrase: str, *, force: bool = False) -> BackupReport:
        source = source.expanduser().resolve()
        if not source.is_file():
            raise FileNotFoundError(f"backup does not exist at {source}")
        if self._has_existing_state() and not force:
            raise ExistingStateError("local state already exists; use --force to replace it")
        self.data_dir.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        workspace = self.data_dir.parent / f".polaris-import-{uuid.uuid4().hex}"
        workspace.mkdir(mode=0o700)
        archive = workspace / "payload.tar"
        extracted = workspace / "extracted"
        extracted.mkdir(mode=0o700)
        try:
            _decrypt_file(source, archive, passphrase)
            self._safe_extract(archive, extracted)
            files = self._verify_manifest(extracted)
            replacement = extracted / STATE_DIRECTORY
            self._rewrite_config(replacement / "config.json")
            self._restrict_tree(replacement)
            self._replace_state(replacement)
            return BackupReport(
                source,
                len(files),
                self._total_size(files),
            )
        finally:
            if not self._path_exists(self._restore_marker):
                shutil.rmtree(workspace, ignore_errors=True)

    def _stage_config(self, destination: Path) -> None:
        if self.config_file.exists() and not self._is_secrets_file(self.config_file):
            try:
                raw = json.loads(self.config_file.read_text(encoding="utf-8"))
            except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
                raise BackupFormatError(f"could not read configuration: {exc}") from exc
            payload = _sanitize_config(raw)
        else:
            payload = {"data_dir": str(self.data_dir)}
        _write_private(
            destination,
            json.dumps(payload, indent=2, sort_keys=True).encode("utf-8") + b"\n",
        )

    def _snapshot_database(self, destination: Path) -> None:
        target = sqlite3.connect(destination)
        try:
            if self.journal_file.exists() and not self._is_secrets_file(self.journal_file):
                source = sqlite3.connect(f"file:{self.journal_file}?mode=ro", uri=True, timeout=30)
                try:
                    source.backup(target)
                finally:
                    source.close()
            target.commit()
        except sqlite3.Error as exc:
            raise BackupError(f"could not snapshot SQLite journal: {exc}") from exc
        finally:
            target.close()
        destination.chmod(0o600)

    def _stage_artifacts(self, destination: Path) -> None:
        destination.mkdir(mode=0o700)
        if not self.artifact_dir.exists():
            return
        for source in sorted(self.artifact_dir.rglob("*")):
            if source.is_symlink():
                raise BackupError(f"artifact symlinks are not supported: {source}")
            if self._is_secrets_file(source):
                continue
            relative = source.relative_to(self.artifact_dir)
            target = destination / relative
            if source.is_dir():
                target.mkdir(parents=True, exist_ok=True, mode=0o700)
            elif source.is_file():
                target.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
                with source.open("rb") as input_handle, target.open("xb") as output_handle:
                    shutil.copyfileobj(input_handle, output_handle, CHUNK_SIZE)
                target.chmod(0o600)
            else:
                raise BackupError(f"unsupported artifact type: {source}")

    def _is_secrets_file(self, path: Path) -> bool:
        if path.resolve() == self.secrets_file:
            return True
        try:
            return path.samefile(self.secrets_file)
        except FileNotFoundError:
            return False

    @staticmethod
    def _manifest_files(state: Path) -> list[dict[str, object]]:
        files: list[dict[str, object]] = []
        for path in sorted(state.rglob("*")):
            if path.is_file():
                relative = path.relative_to(state).as_posix()
                files.append(
                    {
                        "path": f"{STATE_DIRECTORY}/{relative}",
                        "sha256": _sha256(path),
                        "size": path.stat().st_size,
                        "provenance": relative.split("/", 1)[0],
                    }
                )
        return files

    @staticmethod
    def _safe_extract(archive: Path, destination: Path) -> None:
        seen: set[str] = set()
        try:
            with tarfile.open(archive, "r") as bundle:
                for member in bundle:
                    pure = PurePosixPath(member.name)
                    if (
                        pure.is_absolute()
                        or not pure.parts
                        or ".." in pure.parts
                        or member.name != pure.as_posix()
                        or member.name in seen
                    ):
                        raise BackupFormatError(f"unsafe archive path: {member.name!r}")
                    seen.add(member.name)
                    if member.issym() or member.islnk() or member.isdev() or member.isfifo():
                        raise BackupFormatError(f"unsupported archive member: {member.name!r}")
                    target = destination.joinpath(*pure.parts)
                    if not target.resolve().is_relative_to(destination.resolve()):
                        raise BackupFormatError(f"archive path escapes state: {member.name!r}")
                    if member.isdir():
                        target.mkdir(parents=True, exist_ok=True, mode=0o700)
                    elif member.isfile():
                        target.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
                        input_handle = bundle.extractfile(member)
                        if input_handle is None:
                            raise BackupFormatError(
                                f"could not read archive member: {member.name!r}"
                            )
                        with input_handle, target.open("xb") as output_handle:
                            shutil.copyfileobj(input_handle, output_handle, CHUNK_SIZE)
                        target.chmod(0o600)
                    else:
                        raise BackupFormatError(f"unsupported archive member: {member.name!r}")
        except (tarfile.TarError, OSError) as exc:
            if isinstance(exc, BackupFormatError):
                raise
            raise BackupFormatError(f"invalid backup archive: {exc}") from exc

    @staticmethod
    def _verify_manifest(extracted: Path) -> list[dict[str, object]]:
        manifest_path = extracted / MANIFEST_NAME
        state = extracted / STATE_DIRECTORY
        try:
            manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
            files = manifest["files"]
        except (OSError, UnicodeDecodeError, json.JSONDecodeError, KeyError, TypeError) as exc:
            raise BackupFormatError(f"backup manifest is invalid: {exc}") from exc
        if (
            manifest.get("format") != "polaris-backup"
            or manifest.get("version") != VERSION
            or not isinstance(files, list)
            or not state.is_dir()
        ):
            raise BackupFormatError("backup manifest format is invalid")
        expected: set[str] = set()
        for item in files:
            if not isinstance(item, dict):
                raise BackupFormatError("backup manifest contains an invalid file entry")
            path_value = item.get("path")
            digest = item.get("sha256")
            size = item.get("size")
            if (
                not isinstance(path_value, str)
                or not isinstance(digest, str)
                or not isinstance(size, int)
                or path_value in expected
            ):
                raise BackupFormatError("backup manifest contains an invalid file entry")
            pure = PurePosixPath(path_value)
            if (
                pure.is_absolute()
                or not pure.parts
                or pure.parts[0] != STATE_DIRECTORY
                or ".." in pure.parts
            ):
                raise BackupFormatError("backup manifest contains an unsafe path")
            path = extracted.joinpath(*pure.parts)
            if not path.is_file() or path.stat().st_size != size or _sha256(path) != digest:
                raise BackupFormatError(f"backup file failed verification: {path_value}")
            expected.add(path_value)
        actual = {
            f"{STATE_DIRECTORY}/{path.relative_to(state).as_posix()}"
            for path in state.rglob("*")
            if path.is_file()
        }
        if actual != expected:
            raise BackupFormatError("backup contents do not match the manifest")
        required = {f"{STATE_DIRECTORY}/config.json", f"{STATE_DIRECTORY}/journal.sqlite3"}
        if not required.issubset(expected):
            raise BackupFormatError("backup is missing required state files")
        return files

    def _rewrite_config(self, path: Path) -> None:
        try:
            config = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise BackupFormatError(f"backup configuration is invalid: {exc}") from exc
        if not isinstance(config, dict):
            raise BackupFormatError("backup configuration must be a JSON object")
        config["data_dir"] = str(self.data_dir)
        daemon = config.get("daemon")
        if isinstance(daemon, dict):
            daemon["token_file"] = str(self.data_dir / "api-token")
        temporary = path.with_name(f".{path.name}.{uuid.uuid4().hex}.tmp")
        _write_private(
            temporary,
            json.dumps(config, indent=2, sort_keys=True).encode("utf-8") + b"\n",
        )
        os.replace(temporary, path)

    def _has_existing_state(self) -> bool:
        self._recover_restore_transaction()
        if self.config_file.exists():
            return True
        if not self.data_dir.exists():
            return False
        try:
            next(self.data_dir.iterdir())
        except StopIteration:
            return False
        return True

    def _replace_state(self, replacement: Path) -> None:
        old_data = self.data_dir.parent / f".{self.data_dir.name}.backup-{uuid.uuid4().hex}"
        external_config = self.config_file != self.data_dir / "config.json"
        transaction = _RestoreTransaction(
            target=self.data_dir,
            old=old_data,
            replacement=replacement.resolve(),
            phase="prepared",
            target_existed=self.data_dir.exists(),
        )
        if external_config:
            transaction.config_file = self.config_file
            transaction.old_config = self.config_file.parent / (
                f".{self.config_file.name}.backup-{uuid.uuid4().hex}"
            )
            transaction.staged_config = self.config_file.parent / (
                f".{self.config_file.name}.{uuid.uuid4().hex}.tmp"
            )
            transaction.config_existed = self.config_file.exists()

        self._fsync_tree(transaction.replacement)
        self._persist_restore_transaction(transaction)
        try:
            if transaction.target_existed:
                self._rename(self.data_dir, old_data)
                self._fsync_directory(self.data_dir.parent)
            transaction.phase = "old_moved"
            self._persist_restore_transaction(transaction)

            self._rename(transaction.replacement, self.data_dir)
            self._fsync_directory(self.data_dir.parent)
            transaction.phase = "data_installed"
            self._persist_restore_transaction(transaction)

            if external_config:
                self._install_external_config(transaction)
        except OSError as exc:
            try:
                self._rollback_restore_transaction(transaction)
            except OSError as rollback_exc:
                raise BackupError(
                    "could not install imported state and rollback is incomplete: "
                    f"{rollback_exc}"
                ) from exc
            raise BackupError(f"could not atomically install imported state: {exc}") from exc

        try:
            self._finalize_restore_transaction(transaction)
        except OSError as exc:
            raise BackupError(
                f"imported state is durable but restore cleanup is incomplete: {exc}"
            ) from exc

    def _install_external_config(self, transaction: _RestoreTransaction) -> None:
        config_file = transaction.config_file
        old_config = transaction.old_config
        staged_config = transaction.staged_config
        if config_file is None or old_config is None or staged_config is None:
            raise OSError("restore transaction has incomplete external config paths")

        config_file.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        if transaction.config_existed and not self._path_exists(old_config):
            self._copy_file_durable(config_file, old_config)
            self._fsync_directory(config_file.parent)
        staged_config.unlink(missing_ok=True)
        self._copy_file_durable(self.data_dir / "config.json", staged_config)
        transaction.phase = "config_prepared"
        self._persist_restore_transaction(transaction)
        self._rename(staged_config, config_file)
        self._fsync_directory(config_file.parent)
        transaction.phase = "config_installed"
        self._persist_restore_transaction(transaction)

    def _rollback_restore_transaction(self, transaction: _RestoreTransaction) -> None:
        self._restore_external_config(transaction)

        if self._path_exists(transaction.old):
            if self._path_exists(transaction.target):
                self._remove_tree(transaction.target)
                self._fsync_directory(transaction.target.parent)
            self._rename(transaction.old, transaction.target)
            self._fsync_directory(transaction.target.parent)
        elif not self._path_exists(transaction.target) and self._path_exists(
            transaction.replacement
        ):
            self._rename(transaction.replacement, transaction.target)
            self._fsync_directory(transaction.target.parent)

        if self._path_exists(transaction.target):
            self._finalize_restore_transaction(transaction)

    def _recover_restore_transaction(self) -> None:
        if not self._path_exists(self._restore_marker):
            return
        transaction = self._read_restore_transaction()
        target_exists = self._path_exists(transaction.target)
        old_exists = self._path_exists(transaction.old)
        replacement_exists = self._path_exists(transaction.replacement)

        try:
            if not target_exists and old_exists:
                self._restore_external_config(transaction)
                self._rename(transaction.old, transaction.target)
                self._fsync_directory(transaction.target.parent)
            elif target_exists:
                self._require_directory(transaction.target, "restore target")
                if transaction.phase != "prepared" and transaction.config_file is not None:
                    self._install_external_config(transaction)
            elif replacement_exists:
                self._require_directory(transaction.replacement, "staged replacement")
                self._fsync_tree(transaction.replacement)
                self._rename(transaction.replacement, transaction.target)
                self._fsync_directory(transaction.target.parent)
                transaction.phase = "data_installed"
                self._persist_restore_transaction(transaction)
                if transaction.config_file is not None:
                    self._install_external_config(transaction)
            else:
                raise BackupError(
                    "interrupted restore has no target, previous state, or staged replacement"
                )
            self._finalize_restore_transaction(transaction)
        except OSError as exc:
            raise BackupError(f"could not recover interrupted restore: {exc}") from exc

    def _restore_external_config(self, transaction: _RestoreTransaction) -> None:
        config_file = transaction.config_file
        old_config = transaction.old_config
        staged_config = transaction.staged_config
        if config_file is None:
            return
        if old_config is not None and self._path_exists(old_config):
            self._rename(old_config, config_file)
            self._fsync_directory(config_file.parent)
        elif (
            not transaction.config_existed
            and transaction.phase in {"config_prepared", "config_installed"}
            and self._path_exists(config_file)
        ):
            config_file.unlink()
            self._fsync_directory(config_file.parent)
        if staged_config is not None:
            staged_config.unlink(missing_ok=True)

    def _finalize_restore_transaction(self, transaction: _RestoreTransaction) -> None:
        self._require_directory(transaction.target, "restore target")
        if self._path_exists(transaction.old):
            self._remove_tree(transaction.old)
            self._fsync_directory(transaction.old.parent)
        if self._path_exists(transaction.replacement):
            self._remove_tree(transaction.replacement)
        self._cleanup_restore_workspace(transaction.replacement)

        for path in (transaction.staged_config, transaction.old_config):
            if path is not None:
                path.unlink(missing_ok=True)
        if (
            transaction.config_file is not None
            and self._path_exists(transaction.config_file.parent)
        ):
            self._require_directory(transaction.config_file.parent, "config directory")
            self._fsync_directory(transaction.config_file.parent)

        self._restore_marker.unlink()
        self._fsync_directory(self._restore_marker.parent)

    def _persist_restore_transaction(self, transaction: _RestoreTransaction) -> None:
        payload: dict[str, object] = {
            "version": RESTORE_TRANSACTION_VERSION,
            "target": str(transaction.target),
            "old": str(transaction.old),
            "replacement": str(transaction.replacement),
            "phase": transaction.phase,
            "target_existed": transaction.target_existed,
        }
        if transaction.config_file is not None:
            payload.update(
                {
                    "config_file": str(transaction.config_file),
                    "old_config": str(transaction.old_config),
                    "staged_config": str(transaction.staged_config),
                    "config_existed": transaction.config_existed,
                }
            )
        temporary = self._restore_marker.parent / (
            f".{self._restore_marker.name}.{uuid.uuid4().hex}.tmp"
        )
        try:
            _write_private(
                temporary,
                json.dumps(payload, sort_keys=True).encode("utf-8") + b"\n",
            )
            os.replace(temporary, self._restore_marker)
            self._restore_marker.chmod(0o600)
            self._fsync_directory(self._restore_marker.parent)
        finally:
            temporary.unlink(missing_ok=True)

    def _read_restore_transaction(self) -> _RestoreTransaction:
        try:
            marker_stat = self._restore_marker.lstat()
            if not self._restore_marker.is_file() or self._restore_marker.is_symlink():
                raise BackupError("restore transaction marker is not a regular file")
            if marker_stat.st_mode & 0o077:
                raise BackupError("restore transaction marker is not owner-only")
            payload = json.loads(self._restore_marker.read_text(encoding="utf-8"))
        except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise BackupError(f"restore transaction marker is invalid: {exc}") from exc
        if not isinstance(payload, dict):
            raise BackupError("restore transaction marker is invalid")

        target = self._transaction_path(payload, "target")
        old = self._transaction_path(payload, "old")
        replacement = self._transaction_path(payload, "replacement")
        phase = payload.get("phase")
        target_existed = payload.get("target_existed")
        if (
            payload.get("version") != RESTORE_TRANSACTION_VERSION
            or target != self.data_dir
            or old.parent != self.data_dir.parent
            or not self._has_uuid_suffix(old.name, f".{self.data_dir.name}.backup-")
            or replacement.name != STATE_DIRECTORY
            or replacement.parent.name != "extracted"
            or replacement.parent.parent.parent != self.data_dir.parent
            or not self._has_uuid_suffix(
                replacement.parent.parent.name, ".polaris-import-"
            )
            or phase not in RESTORE_PHASES
            or not isinstance(target_existed, bool)
        ):
            raise BackupError("restore transaction marker does not match this data directory")

        config_file: Path | None = None
        old_config: Path | None = None
        staged_config: Path | None = None
        config_existed = False
        if "config_file" in payload:
            config_file = self._transaction_path(payload, "config_file")
            old_config = self._transaction_path(payload, "old_config")
            staged_config = self._transaction_path(payload, "staged_config")
            config_existed_value = payload.get("config_existed")
            if (
                config_file != self.config_file
                or config_file == self.data_dir / "config.json"
                or old_config.parent != config_file.parent
                or staged_config.parent != config_file.parent
                or not old_config.name.startswith(f".{config_file.name}.backup-")
                or not staged_config.name.startswith(f".{config_file.name}.")
                or not isinstance(config_existed_value, bool)
            ):
                raise BackupError("restore transaction external config paths are invalid")
            config_existed = config_existed_value

        return _RestoreTransaction(
            target=target,
            old=old,
            replacement=replacement,
            phase=phase,
            target_existed=target_existed,
            config_file=config_file,
            old_config=old_config,
            staged_config=staged_config,
            config_existed=config_existed,
        )

    @staticmethod
    def _transaction_path(payload: dict[str, object], key: str) -> Path:
        value = payload.get(key)
        if not isinstance(value, str):
            raise BackupError(f"restore transaction marker is missing {key}")
        path = Path(value)
        if not path.is_absolute() or ".." in path.parts:
            raise BackupError(f"restore transaction marker has unsafe {key}")
        return path

    @staticmethod
    def _copy_file_durable(source: Path, destination: Path) -> None:
        try:
            with source.open("rb") as input_handle, destination.open("xb") as output_handle:
                shutil.copyfileobj(input_handle, output_handle, CHUNK_SIZE)
                destination.chmod(0o600)
                output_handle.flush()
                os.fsync(output_handle.fileno())
        except OSError:
            destination.unlink(missing_ok=True)
            raise

    @staticmethod
    def _has_uuid_suffix(value: str, prefix: str) -> bool:
        suffix = value.removeprefix(prefix)
        return (
            value.startswith(prefix)
            and len(suffix) == 32
            and all(character in "0123456789abcdef" for character in suffix)
        )

    @staticmethod
    def _rename(source: Path, destination: Path) -> None:
        os.replace(source, destination)

    @staticmethod
    def _path_exists(path: Path) -> bool:
        return os.path.lexists(path)

    @staticmethod
    def _require_directory(path: Path, description: str) -> None:
        if path.is_symlink() or not path.is_dir():
            raise BackupError(f"{description} is not a directory: {path}")

    @classmethod
    def _remove_tree(cls, path: Path) -> None:
        cls._require_directory(path, "restore state")
        shutil.rmtree(path)

    def _cleanup_restore_workspace(self, replacement: Path) -> None:
        extracted = replacement.parent
        workspace = extracted.parent
        if (
            replacement.name == STATE_DIRECTORY
            and extracted.name == "extracted"
            and workspace.parent == self.data_dir.parent
            and workspace.name.startswith(".polaris-import-")
            and self._path_exists(workspace)
        ):
            self._remove_tree(workspace)

    @classmethod
    def _fsync_tree(cls, root: Path) -> None:
        cls._require_directory(root, "staged replacement")
        directories = [root]
        for path in root.rglob("*"):
            if path.is_symlink():
                raise BackupError(f"staged replacement contains a symlink: {path}")
            if path.is_file():
                cls._fsync_file(path)
            elif path.is_dir():
                directories.append(path)
            else:
                raise BackupError(f"staged replacement contains an unsupported entry: {path}")
        for directory in sorted(directories, key=lambda item: len(item.parts), reverse=True):
            cls._fsync_directory(directory)

    @staticmethod
    def _fsync_file(path: Path) -> None:
        descriptor = os.open(path, os.O_RDONLY)
        try:
            os.fsync(descriptor)
        finally:
            os.close(descriptor)

    @staticmethod
    def _restrict_tree(root: Path) -> None:
        for path in [root, *root.rglob("*")]:
            if path.is_dir():
                path.chmod(0o700)
            elif path.is_file():
                path.chmod(0o600)

    @staticmethod
    def _total_size(files: list[dict[str, object]]) -> int:
        total = 0
        for item in files:
            size = item.get("size")
            if isinstance(size, int):
                total += size
        return total

    @staticmethod
    def _fsync_directory(path: Path) -> None:
        descriptor = os.open(path, os.O_RDONLY)
        try:
            os.fsync(descriptor)
        finally:
            os.close(descriptor)


def export_backup(
    destination: Path,
    passphrase: str,
    *,
    data_dir: Path,
    config_file: Path | None = None,
    journal_file: Path | None = None,
    artifact_dir: Path | None = None,
    secrets_file: Path | None = None,
) -> BackupReport:
    return BackupManager(
        data_dir=data_dir,
        config_file=config_file,
        journal_file=journal_file,
        artifact_dir=artifact_dir,
        secrets_file=secrets_file,
    ).export(destination, passphrase)


def import_backup(
    source: Path,
    passphrase: str,
    *,
    data_dir: Path,
    config_file: Path | None = None,
    journal_file: Path | None = None,
    artifact_dir: Path | None = None,
    secrets_file: Path | None = None,
    force: bool = False,
) -> BackupReport:
    return BackupManager(
        data_dir=data_dir,
        config_file=config_file,
        journal_file=journal_file,
        artifact_dir=artifact_dir,
        secrets_file=secrets_file,
    ).import_archive(source, passphrase, force=force)
