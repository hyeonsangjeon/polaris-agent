"""Root-scoped filesystem tools."""

from __future__ import annotations

import asyncio
import base64
import binascii
import contextlib
import hashlib
import os
import tempfile
from collections.abc import Iterable
from pathlib import Path

from .registry import JsonValue, SafetyClass, ToolArguments, ToolEntry, ToolResult


class FilesystemToolError(RuntimeError):
    """Base filesystem adapter error."""


class PathAccessError(FilesystemToolError):
    """A path is invalid or escapes the configured roots."""


class FileConflictError(FilesystemToolError):
    """The current file does not have the expected previous hash."""


def _validated_roots(roots: Iterable[str | os.PathLike[str]]) -> tuple[Path, ...]:
    result: list[Path] = []
    for root in roots:
        resolved = Path(root).expanduser().resolve(strict=True)
        if not resolved.is_dir():
            raise ValueError("filesystem roots must be directories")
        result.append(resolved)
    if not result:
        raise ValueError("at least one filesystem root is required")
    return tuple(dict.fromkeys(result))


def _inside(path: Path, root: Path) -> bool:
    return path == root or path.is_relative_to(root)


def _sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


class FilesystemTools:
    """Read, list, and atomically write files beneath configured roots."""

    def __init__(
        self,
        roots: Iterable[str | os.PathLike[str]],
        *,
        max_read_bytes: int = 10_000_000,
        max_list_entries: int = 1_000,
    ) -> None:
        self.roots = _validated_roots(roots)
        if max_read_bytes <= 0 or max_list_entries <= 0:
            raise ValueError("filesystem limits must be positive")
        self.max_read_bytes = max_read_bytes
        self.max_list_entries = max_list_entries

    @staticmethod
    def _path_value(arguments: ToolArguments) -> str:
        value = arguments.get("path")
        if not isinstance(value, str) or not value or "\x00" in value:
            raise PathAccessError("path must be a non-empty string without NUL")
        return value

    def _lexical_path(self, value: str) -> tuple[Path, Path]:
        candidate = Path(value).expanduser()
        candidates = (
            (candidate,)
            if candidate.is_absolute()
            else tuple(root / candidate for root in self.roots)
        )
        for item in candidates:
            absolute = Path(os.path.abspath(item))
            for root in self.roots:
                if _inside(absolute, root):
                    return absolute, root
        raise PathAccessError("path escapes configured roots")

    def _existing_path(self, value: str) -> Path:
        candidate, root = self._lexical_path(value)
        try:
            resolved = candidate.resolve(strict=True)
        except (FileNotFoundError, OSError) as exc:
            raise PathAccessError("path does not exist") from exc
        if not _inside(resolved, root):
            raise PathAccessError("path escapes configured roots through a symlink")
        return resolved

    def _write_path(self, value: str, *, create_parent: bool = True) -> Path:
        candidate, root = self._lexical_path(value)
        if candidate.is_symlink():
            raise PathAccessError("write target may not be a symlink")
        probe = candidate.parent
        while not probe.exists():
            if probe.is_symlink():
                raise PathAccessError("write path contains a broken symlink")
            if probe == root:
                break
            probe = probe.parent
        try:
            resolved_probe = probe.resolve(strict=True)
        except OSError as exc:
            raise PathAccessError("write parent cannot be resolved") from exc
        if not _inside(resolved_probe, root):
            raise PathAccessError("write path escapes configured roots through a symlink")
        if not create_parent:
            if candidate.exists():
                try:
                    resolved_target = candidate.resolve(strict=True)
                except OSError as exc:
                    raise PathAccessError("write target cannot be resolved") from exc
                if not _inside(resolved_target, root) or not resolved_target.is_file():
                    raise PathAccessError("write target is not a root-scoped file")
                return resolved_target
            return candidate
        try:
            candidate.parent.mkdir(parents=True, exist_ok=True)
            resolved_parent = candidate.parent.resolve(strict=True)
        except OSError as exc:
            raise FilesystemToolError("write parent could not be created") from exc
        if not _inside(resolved_parent, root):
            raise PathAccessError("write path escapes configured roots through a symlink")
        resolved_target = resolved_parent / candidate.name
        if resolved_target.is_symlink():
            raise PathAccessError("write target may not be a symlink")
        if resolved_target.exists():
            try:
                checked = resolved_target.resolve(strict=True)
            except OSError as exc:
                raise PathAccessError("write target cannot be resolved") from exc
            if not _inside(checked, root) or not checked.is_file():
                raise PathAccessError("write target is not a root-scoped file")
        return resolved_target

    @staticmethod
    def _content(arguments: ToolArguments) -> bytes:
        has_text = "content" in arguments
        has_bytes = "content_base64" in arguments
        if has_text == has_bytes:
            raise FilesystemToolError("provide exactly one of content or content_base64")
        if has_text:
            content = arguments["content"]
            if not isinstance(content, str):
                raise FilesystemToolError("content must be a string")
            return content.encode("utf-8")
        encoded = arguments["content_base64"]
        if not isinstance(encoded, str):
            raise FilesystemToolError("content_base64 must be a string")
        try:
            return base64.b64decode(encoded, validate=True)
        except (binascii.Error, ValueError) as exc:
            raise FilesystemToolError("content_base64 is invalid") from exc

    @staticmethod
    def _read_bytes(path: Path, maximum: int) -> bytes:
        try:
            size = path.stat().st_size
            if size > maximum:
                raise FilesystemToolError("file exceeds the configured read limit")
            data = path.read_bytes()
        except FilesystemToolError:
            raise
        except OSError as exc:
            raise FilesystemToolError("file could not be read") from exc
        if len(data) > maximum:
            raise FilesystemToolError("file exceeds the configured read limit")
        return data

    async def read_file(self, arguments: ToolArguments) -> ToolResult:
        path = self._existing_path(self._path_value(arguments))
        if not path.is_file():
            raise PathAccessError("path is not a file")
        data = await asyncio.to_thread(self._read_bytes, path, self.max_read_bytes)
        encoding = arguments.get("encoding", "utf-8")
        if encoding == "utf-8":
            try:
                content = data.decode("utf-8")
            except UnicodeDecodeError as exc:
                raise FilesystemToolError(
                    "file is not valid UTF-8; request base64 encoding"
                ) from exc
        elif encoding == "base64":
            content = base64.b64encode(data).decode("ascii")
        else:
            raise FilesystemToolError("encoding must be utf-8 or base64")
        return {
            "path": str(path),
            "content": content,
            "encoding": encoding,
            "sha256": _sha256(data),
            "size": len(data),
        }

    async def list_directory(self, arguments: ToolArguments) -> ToolResult:
        path = self._existing_path(self._path_value(arguments))
        if not path.is_dir():
            raise PathAccessError("path is not a directory")

        def list_entries() -> list[JsonValue]:
            try:
                children = sorted(path.iterdir(), key=lambda item: item.name)
            except OSError as exc:
                raise FilesystemToolError("directory could not be listed") from exc
            if len(children) > self.max_list_entries:
                raise FilesystemToolError("directory exceeds the configured entry limit")
            entries: list[JsonValue] = []
            for child in children:
                if child.is_symlink():
                    entries.append({"name": child.name, "path": str(child), "type": "symlink"})
                elif child.is_file():
                    data = self._read_bytes(child, self.max_read_bytes)
                    entries.append(
                        {
                            "name": child.name,
                            "path": str(child),
                            "type": "file",
                            "size": len(data),
                            "sha256": _sha256(data),
                        }
                    )
                elif child.is_dir():
                    entries.append({"name": child.name, "path": str(child), "type": "directory"})
                else:
                    entries.append({"name": child.name, "path": str(child), "type": "other"})
            return entries

        entries = await asyncio.to_thread(list_entries)
        return {"path": str(path), "entries": entries, "count": len(entries)}

    @staticmethod
    def _current(path: Path) -> tuple[str | None, int | None]:
        if not path.exists():
            return None, None
        try:
            data = path.read_bytes()
        except OSError as exc:
            raise FilesystemToolError("current file could not be read") from exc
        return _sha256(data), len(data)

    @staticmethod
    def _expected_hash(arguments: ToolArguments) -> tuple[bool, str | None]:
        keys = ("expected_previous_hash", "expected_sha256")
        present = [key for key in keys if key in arguments]
        if len(present) > 1:
            raise FilesystemToolError("provide only one expected previous hash")
        if not present:
            return False, None
        value = arguments[present[0]]
        if value is not None and (
            not isinstance(value, str)
            or len(value) != 64
            or any(character not in "0123456789abcdefABCDEF" for character in value)
        ):
            raise FilesystemToolError("expected previous hash must be SHA-256 or null")
        return True, value.lower() if isinstance(value, str) else None

    @staticmethod
    def _receipt(path: Path, digest: str, size: int) -> dict[str, JsonValue]:
        return {"path": str(path), "sha256": digest, "size": size}

    @staticmethod
    def _atomic_write(path: Path, content: bytes) -> None:
        temporary_name: str | None = None
        try:
            with tempfile.NamedTemporaryFile(
                mode="wb", dir=path.parent, prefix=".polaris-write-", delete=False
            ) as temporary:
                temporary_name = temporary.name
                temporary.write(content)
                temporary.flush()
                os.fsync(temporary.fileno())
            if path.is_symlink():
                raise PathAccessError("write target became a symlink")
            os.replace(temporary_name, path)
            temporary_name = None
            directory_fd = os.open(path.parent, os.O_RDONLY)
            try:
                os.fsync(directory_fd)
            finally:
                os.close(directory_fd)
        except (PathAccessError, FilesystemToolError):
            raise
        except OSError as exc:
            raise FilesystemToolError("atomic write failed") from exc
        finally:
            if temporary_name is not None:
                with contextlib.suppress(FileNotFoundError):
                    os.unlink(temporary_name)

    async def write_file(self, arguments: ToolArguments) -> ToolResult:
        path_value = self._path_value(arguments)
        path = self._write_path(path_value, create_parent=False)
        content = self._content(arguments)
        desired_hash = _sha256(content)
        current_hash, current_size = await asyncio.to_thread(self._current, path)
        receipt = self._receipt(path, desired_hash, len(content))
        if current_hash == desired_hash:
            return {
                **receipt,
                "written": False,
                "found": True,
                "receipt": receipt,
            }
        has_expected, expected = self._expected_hash(arguments)
        if has_expected and current_hash != expected:
            raise FileConflictError("current file hash does not match expected previous hash")
        path = self._write_path(path_value)
        receipt = self._receipt(path, desired_hash, len(content))
        checked_hash, checked_size = await asyncio.to_thread(self._current, path)
        if checked_hash == desired_hash:
            return {
                **receipt,
                "written": False,
                "found": True,
                "receipt": receipt,
            }
        if checked_hash != current_hash:
            raise FileConflictError("current file changed while preparing the atomic write")
        current_size = checked_size
        await asyncio.to_thread(self._atomic_write, path, content)
        return {
            **receipt,
            "written": True,
            "found": current_hash is not None,
            "previous_sha256": current_hash,
            "previous_size": current_size,
            "receipt": receipt,
        }

    async def reconcile_write(self, arguments: ToolArguments) -> ToolResult:
        path = self._write_path(self._path_value(arguments), create_parent=False)
        desired = self._content(arguments)
        desired_hash = _sha256(desired)
        current_hash, current_size = await asyncio.to_thread(self._current, path)
        found = current_hash == desired_hash
        return {
            "found": found,
            "path": str(path),
            "desired_sha256": desired_hash,
            "current_sha256": current_hash,
            "size": current_size,
            "receipt": self._receipt(path, desired_hash, len(desired)) if found else None,
        }

    def entries(self) -> tuple[ToolEntry, ToolEntry, ToolEntry]:
        path_parameter: dict[str, JsonValue] = {
            "type": "object",
            "properties": {"path": {"type": "string", "minLength": 1}},
            "required": ["path"],
            "additionalProperties": False,
        }
        read_schema: dict[str, JsonValue] = {
            "parameters": {
                "type": "object",
                "properties": {
                    "path": {"type": "string", "minLength": 1},
                    "encoding": {"type": "string", "enum": ["utf-8", "base64"]},
                },
                "required": ["path"],
                "additionalProperties": False,
            }
        }
        write_schema: dict[str, JsonValue] = {
            "parameters": {
                "type": "object",
                "properties": {
                    "path": {"type": "string", "minLength": 1},
                    "content": {"type": "string"},
                    "content_base64": {"type": "string"},
                    "expected_previous_hash": {
                        "type": ["string", "null"],
                        "pattern": "^[0-9a-fA-F]{64}$",
                    },
                },
                "required": ["path"],
                "oneOf": [{"required": ["content"]}, {"required": ["content_base64"]}],
                "additionalProperties": False,
            }
        }
        return (
            ToolEntry(
                name="read_file",
                toolset="filesystem",
                schema=read_schema,
                handler=self.read_file,
                description="Read a root-scoped file as UTF-8 or base64.",
                safety_class=SafetyClass.READ_ONLY,
            ),
            ToolEntry(
                name="list_directory",
                toolset="filesystem",
                schema={"parameters": path_parameter},
                handler=self.list_directory,
                description="List one root-scoped directory.",
                safety_class=SafetyClass.READ_ONLY,
            ),
            ToolEntry(
                name="write_file",
                toolset="filesystem",
                schema=write_schema,
                handler=self.write_file,
                description="Atomically write a root-scoped file.",
                safety_class=SafetyClass.RECONCILABLE,
                reconcile_handler=self.reconcile_write,
            ),
        )


def create_filesystem_entries(
    roots: Iterable[str | os.PathLike[str]],
) -> tuple[ToolEntry, ToolEntry, ToolEntry]:
    """Create read, list, and write filesystem entries."""

    return FilesystemTools(roots).entries()
