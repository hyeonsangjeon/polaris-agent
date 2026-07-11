"""Scope-bound model tools for explicit curated memory operations."""

from __future__ import annotations

from collections.abc import Mapping

from polaris.tools.registry import JsonValue, SafetyClass, ToolArguments, ToolEntry

from .errors import MemoryConflictError, MemoryNotFoundError
from .models import MemoryEntry, MemoryKind, MemoryScope, MemoryWrite, TrustLevel
from .store import MemoryStore


def _string(arguments: ToolArguments, name: str) -> str:
    value = arguments.get(name)
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{name} must be a non-empty string")
    return value


def _optional_string(arguments: ToolArguments, name: str) -> str | None:
    value = arguments.get(name)
    if value is None:
        return None
    if not isinstance(value, str):
        raise TypeError(f"{name} must be a string")
    return value


def _integer(arguments: ToolArguments, name: str, default: int | None = None) -> int:
    value = arguments.get(name, default)
    if not isinstance(value, int) or isinstance(value, bool):
        raise TypeError(f"{name} must be an integer")
    return value


def _entry_result(entry: MemoryEntry, *, status: str) -> dict[str, JsonValue]:
    return {
        "status": status,
        "id": entry.id,
        "revision": entry.revision,
        "content_hash": entry.content_hash,
        "blocked": entry.blocked_reason is not None,
        "tombstoned": entry.tombstoned,
    }


class MemoryTools:
    """Factory for tools permanently bound to one non-model-controlled scope."""

    def __init__(self, store: MemoryStore, scope: MemoryScope) -> None:
        if not isinstance(store, MemoryStore):
            raise TypeError("store must be MemoryStore")
        if not isinstance(scope, MemoryScope):
            raise TypeError("scope must be MemoryScope")
        self._store = store
        self._scope = scope

    @property
    def scope(self) -> MemoryScope:
        return self._scope

    def entries(self) -> tuple[ToolEntry, ...]:
        return (
            ToolEntry(
                name="memory_search",
                toolset="memory",
                description="Search explicitly curated memory for the current scope.",
                schema={
                    "parameters": {
                        "type": "object",
                        "properties": {
                            "query": {"type": "string"},
                            "limit": {"type": "integer", "minimum": 1, "maximum": 50},
                        },
                        "required": ["query"],
                        "additionalProperties": False,
                    }
                },
                handler=self._search,
                safety_class=SafetyClass.READ_ONLY,
                max_result_size=50000,
            ),
            ToolEntry(
                name="memory_add",
                toolset="memory",
                description="Explicitly add one curated memory in the current scope.",
                schema=self._write_schema(add=True),
                handler=self._add,
                safety_class=SafetyClass.RECONCILABLE,
                reconcile_handler=self._reconcile_add,
            ),
            ToolEntry(
                name="memory_revise",
                toolset="memory",
                description="Optimistically revise one curated memory.",
                schema=self._write_schema(add=False),
                handler=self._revise,
                safety_class=SafetyClass.RECONCILABLE,
                reconcile_handler=self._reconcile_revise,
            ),
            ToolEntry(
                name="memory_remove",
                toolset="memory",
                description="Optimistically tombstone one curated memory.",
                schema={
                    "parameters": {
                        "type": "object",
                        "properties": {
                            "id": {"type": "string"},
                            "expected_revision": {"type": "integer", "minimum": 1},
                            "expected_hash": {"type": "string"},
                        },
                        "required": ["id", "expected_revision"],
                        "additionalProperties": False,
                    }
                },
                handler=self._remove,
                safety_class=SafetyClass.RECONCILABLE,
                reconcile_handler=self._reconcile_remove,
            ),
        )

    @staticmethod
    def _write_schema(*, add: bool) -> Mapping[str, JsonValue]:
        kind_values: list[JsonValue] = [kind.value for kind in MemoryKind]
        trust_values: list[JsonValue] = [level.value for level in TrustLevel]
        properties: dict[str, JsonValue] = {
            "content": {"type": "string"},
            "kind": {"type": "string", "enum": kind_values},
            "trust_level": {
                "type": "string",
                "enum": trust_values,
            },
            "provenance_run_id": {"type": "string"},
            "provenance_session_id": {"type": "string"},
            "provenance_message_id": {"type": "string"},
        }
        required: list[JsonValue] = ["content"]
        if not add:
            properties.update(
                {
                    "id": {"type": "string"},
                    "expected_revision": {"type": "integer", "minimum": 1},
                    "expected_hash": {"type": "string"},
                }
            )
            required.extend(("id", "expected_revision"))
        return {
            "parameters": {
                "type": "object",
                "properties": properties,
                "required": required,
                "additionalProperties": False,
            }
        }

    def _write_from_arguments(self, arguments: ToolArguments) -> MemoryWrite:
        return MemoryWrite(
            content=_string(arguments, "content"),
            kind=MemoryKind(str(arguments.get("kind", MemoryKind.FACT.value))),
            trust_level=TrustLevel(
                str(arguments.get("trust_level", TrustLevel.USER_ASSERTED.value))
            ),
            provenance_run_id=_optional_string(arguments, "provenance_run_id"),
            provenance_session_id=_optional_string(arguments, "provenance_session_id"),
            provenance_message_id=_optional_string(arguments, "provenance_message_id"),
        )

    async def _search(self, arguments: ToolArguments) -> JsonValue:
        hits = self._store.recall(
            _string(arguments, "query"),
            self._scope,
            _integer(arguments, "limit", 10),
        )
        return {
            "search_backend": self._store.search_backend,
            "hits": [
                {
                    "id": hit.id,
                    "content": hit.content,
                    "kind": hit.entry.kind.value,
                    "trust_level": hit.entry.trust_level.value,
                    "revision": hit.revision,
                    "content_hash": hit.content_hash,
                    "blocked": hit.blocked_reason is not None,
                    "score": hit.score,
                }
                for hit in hits
            ],
        }

    async def _add(self, arguments: ToolArguments) -> JsonValue:
        write = self._write_from_arguments(arguments)
        entry, created = self._store.append_reconciled(self._scope, write)
        return _entry_result(entry, status="applied" if created else "already_applied")

    async def _reconcile_add(self, arguments: ToolArguments) -> JsonValue:
        write = self._write_from_arguments(arguments)
        existing = self._store.find_by_hash(
            self._scope, self._store.content_hash(write.content)
        )
        if existing is None:
            return {"status": "not_applied"}
        return _entry_result(existing, status="already_applied")

    async def _revise(self, arguments: ToolArguments) -> JsonValue:
        entry_id = _string(arguments, "id")
        write = self._write_from_arguments(arguments)
        current = self._store.get(self._scope, entry_id)
        desired_hash = self._store.content_hash(write.content)
        if current.content_hash == desired_hash:
            return _entry_result(current, status="already_applied")
        try:
            entry = self._store.replace(
                self._scope,
                entry_id,
                write,
                expected_revision=_integer(arguments, "expected_revision"),
                expected_hash=_optional_string(arguments, "expected_hash"),
            )
        except MemoryConflictError:
            current = self._store.get(self._scope, entry_id)
            if current.content_hash != desired_hash:
                raise
            return _entry_result(current, status="already_applied")
        return _entry_result(entry, status="applied")

    async def _reconcile_revise(self, arguments: ToolArguments) -> JsonValue:
        entry_id = _string(arguments, "id")
        try:
            current = self._store.get(self._scope, entry_id, include_tombstone=True)
        except MemoryNotFoundError:
            return {"status": "not_applied"}
        desired = self._store.content_hash(_string(arguments, "content"))
        if not current.tombstoned and current.content_hash == desired:
            return _entry_result(current, status="already_applied")
        expected_revision = _integer(arguments, "expected_revision")
        if current.revision == expected_revision:
            return {"status": "not_applied", "revision": current.revision}
        return {"status": "conflict", "revision": current.revision}

    async def _remove(self, arguments: ToolArguments) -> JsonValue:
        entry_id = _string(arguments, "id")
        current = self._store.get(self._scope, entry_id, include_tombstone=True)
        if current.tombstoned:
            return _entry_result(current, status="already_applied")
        entry = self._store.remove(
            self._scope,
            entry_id,
            expected_revision=_integer(arguments, "expected_revision"),
            expected_hash=_optional_string(arguments, "expected_hash"),
        )
        return _entry_result(entry, status="applied")

    async def _reconcile_remove(self, arguments: ToolArguments) -> JsonValue:
        try:
            current = self._store.get(
                self._scope, _string(arguments, "id"), include_tombstone=True
            )
        except MemoryNotFoundError:
            return {"status": "not_applied"}
        if current.tombstoned:
            return _entry_result(current, status="already_applied")
        expected_revision = _integer(arguments, "expected_revision")
        if current.revision == expected_revision:
            return {"status": "not_applied", "revision": current.revision}
        return {"status": "conflict", "revision": current.revision}


def build_memory_tools(store: MemoryStore, scope: MemoryScope) -> MemoryTools:
    """Construct a scope-bound memory tool factory."""

    return MemoryTools(store, scope)
