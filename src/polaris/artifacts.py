"""Content-addressed durable artifact storage."""

from __future__ import annotations

import hashlib
import json
import os
import re
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Any

from polaris.journal import canonical_json

if TYPE_CHECKING:
    from polaris.journal import ArtifactRecord, Journal

_SHA256 = re.compile(r"^[0-9a-f]{64}$")


class ArtifactError(RuntimeError):
    """Base error for artifact storage."""


class ArtifactNotFoundError(ArtifactError):
    """The requested artifact does not exist."""


class ArtifactIntegrityError(ArtifactError):
    """Stored content does not match its content address."""


@dataclass(frozen=True, slots=True)
class StoredArtifact:
    """A stored blob and its stable content address."""

    sha256: str
    size_bytes: int
    uri: str


class ArtifactStore:
    """Filesystem store keyed exclusively by the SHA-256 of content."""

    def __init__(self, root: str | Path) -> None:
        self.root = Path(root).expanduser().resolve()
        self.root.mkdir(parents=True, exist_ok=True)

    @staticmethod
    def _digest(data: bytes) -> str:
        return hashlib.sha256(data).hexdigest()

    def path_for(self, sha256: str) -> Path:
        if not isinstance(sha256, str) or _SHA256.fullmatch(sha256) is None:
            raise ValueError("sha256 must be 64 lowercase hexadecimal characters")
        path = self.root / sha256[:2] / sha256[2:4] / sha256
        if not path.resolve().is_relative_to(self.root):
            raise ValueError("artifact path escapes store root")
        return path

    def put(self, data: bytes | bytearray | memoryview) -> StoredArtifact:
        if not isinstance(data, (bytes, bytearray, memoryview)):
            raise TypeError("artifact data must be bytes-like")
        content = bytes(data)
        digest = self._digest(content)
        destination = self.path_for(digest)
        if destination.exists():
            self._verify_path(destination, digest)
            return StoredArtifact(digest, len(content), destination.as_uri())

        destination.parent.mkdir(parents=True, exist_ok=True)
        temporary = destination.parent / f".{digest}.{uuid.uuid4().hex}.tmp"
        try:
            with temporary.open("xb") as handle:
                handle.write(content)
                handle.flush()
                os.fsync(handle.fileno())
            try:
                os.link(temporary, destination)
            except FileExistsError:
                self._verify_path(destination, digest)
            self._fsync_directory(destination.parent)
        finally:
            temporary.unlink(missing_ok=True)
        return StoredArtifact(digest, len(content), destination.as_uri())

    def put_text(self, text: str) -> StoredArtifact:
        if not isinstance(text, str):
            raise TypeError("text must be a string")
        return self.put(text.encode("utf-8"))

    def put_json(self, value: object) -> StoredArtifact:
        return self.put_text(canonical_json(value))

    def get(self, sha256: str, *, verify: bool = True) -> bytes:
        path = self.path_for(sha256)
        try:
            content = path.read_bytes()
        except FileNotFoundError as exc:
            raise ArtifactNotFoundError(f"artifact {sha256!r} was not found") from exc
        if verify and self._digest(content) != sha256:
            raise ArtifactIntegrityError(f"artifact {sha256!r} failed SHA-256 verification")
        return content

    def get_text(self, sha256: str, *, verify: bool = True) -> str:
        return self.get(sha256, verify=verify).decode("utf-8")

    def get_json(self, sha256: str, *, verify: bool = True) -> Any:
        return json.loads(self.get_text(sha256, verify=verify))

    def verify(self, sha256: str) -> bool:
        self.get(sha256, verify=True)
        return True

    def record_artifact(
        self,
        journal: Journal,
        run_id: str,
        name: str,
        data: bytes | bytearray | memoryview | str | object,
        *,
        step_id: str | None = None,
        media_type: str | None = None,
        json_value: bool = False,
        metadata: object | None = None,
    ) -> ArtifactRecord:
        if json_value:
            stored = self.put_json(data)
        elif isinstance(data, str):
            stored = self.put_text(data)
        elif isinstance(data, (bytes, bytearray, memoryview)):
            stored = self.put(data)
        else:
            raise TypeError("non-bytes artifact data requires json_value=True")
        return journal.record_artifact(
            run_id,
            name,
            stored.uri,
            step_id=step_id,
            media_type=media_type,
            sha256=stored.sha256,
            size_bytes=stored.size_bytes,
            metadata=metadata,
        )

    def _verify_path(self, path: Path, expected: str) -> None:
        try:
            actual = self._digest(path.read_bytes())
        except FileNotFoundError as exc:
            raise ArtifactNotFoundError(f"artifact {expected!r} was not found") from exc
        if actual != expected:
            raise ArtifactIntegrityError(f"artifact {expected!r} failed SHA-256 verification")

    @staticmethod
    def _fsync_directory(path: Path) -> None:
        try:
            descriptor = os.open(path, os.O_RDONLY)
        except OSError:
            return
        try:
            os.fsync(descriptor)
        finally:
            os.close(descriptor)


__all__ = [
    "ArtifactError",
    "ArtifactIntegrityError",
    "ArtifactNotFoundError",
    "ArtifactStore",
    "StoredArtifact",
]
