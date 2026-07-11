from __future__ import annotations

import json
from pathlib import Path

import pytest

from polaris.memory import MemoryScope, MemoryStore, MemoryTools, build_memory_tools
from polaris.tools.registry import JsonValue, SafetyClass, ToolRegistry


@pytest.mark.asyncio
async def test_bound_tool_schemas_hide_scope_and_add_reconciles(tmp_path: Path) -> None:
    scope = MemoryScope("private-profile", "private-subject")
    with MemoryStore(tmp_path / "journal.sqlite3") as store:
        tools = MemoryTools(store, scope)
        entries = tools.entries()
        serialized = repr(tuple(entry.schema for entry in entries))
        assert "profile_id" not in serialized
        assert "subject_key" not in serialized
        assert entries[0].safety_class is SafetyClass.READ_ONLY
        assert all(
            entry.safety_class is SafetyClass.RECONCILABLE for entry in entries[1:]
        )

        registry = ToolRegistry()
        for entry in entries:
            registry.register(entry)
        arguments = {"content": "A stable fact", "kind": "fact"}
        first = await registry.execute("memory_add", arguments)
        second = await registry.execute("memory_add", arguments)
        reconciled = await registry.reconcile("memory_add", arguments)

        assert isinstance(first, dict) and first["status"] == "applied"
        assert isinstance(second, dict) and second["status"] == "already_applied"
        assert isinstance(reconciled, dict) and reconciled["status"] == "already_applied"
        assert len(store.list(scope)) == 1


@pytest.mark.asyncio
async def test_revise_and_remove_reconcile_by_hash_and_revision(tmp_path: Path) -> None:
    scope = MemoryScope("profile", "subject")
    with MemoryStore(tmp_path / "journal.sqlite3") as store:
        registry = ToolRegistry()
        for entry in MemoryTools(store, scope).entries():
            registry.register(entry)
        added = await registry.execute("memory_add", {"content": "old"})
        assert isinstance(added, dict)
        entry_id = str(added["id"])

        revise: dict[str, JsonValue] = {
            "id": entry_id,
            "content": "new",
            "expected_revision": 1,
        }
        revised = await registry.execute("memory_revise", revise)
        duplicate = await registry.execute("memory_revise", revise)
        assert isinstance(revised, dict) and revised["revision"] == 2
        assert isinstance(duplicate, dict) and duplicate["status"] == "already_applied"

        remove: dict[str, JsonValue] = {"id": entry_id, "expected_revision": 2}
        removed = await registry.execute("memory_remove", remove)
        reconciled = await registry.reconcile("memory_remove", remove)
        assert isinstance(removed, dict) and removed["tombstoned"] is True
        assert isinstance(reconciled, dict) and reconciled["status"] == "already_applied"

        definitions = registry.get_definitions(toolsets=("memory",))
        assert "profile" not in json.dumps(definitions)


@pytest.mark.asyncio
async def test_search_and_reconciliation_state_machine(tmp_path: Path) -> None:
    scope = MemoryScope("profile", "subject")
    with MemoryStore(tmp_path / "journal.sqlite3") as store:
        tools = build_memory_tools(store, scope)
        assert tools.scope == scope
        registry = ToolRegistry()
        for entry in tools.entries():
            registry.register(entry)

        missing_add = await registry.reconcile("memory_add", {"content": "missing"})
        missing_revise: dict[str, JsonValue] = {
            "id": "missing",
            "content": "new",
            "expected_revision": 1,
        }
        missing_remove: dict[str, JsonValue] = {"id": "missing", "expected_revision": 1}
        assert missing_add == {"status": "not_applied"}
        assert await registry.reconcile("memory_revise", missing_revise) == {
            "status": "not_applied"
        }
        assert await registry.reconcile("memory_remove", missing_remove) == {
            "status": "not_applied"
        }

        added = await registry.execute(
            "memory_add",
            {
                "content": "searchable",
                "trust_level": "verified",
                "provenance_run_id": "run",
                "provenance_session_id": "session",
                "provenance_message_id": "message",
            },
        )
        assert isinstance(added, dict)
        entry_id = str(added["id"])
        search = await registry.execute("memory_search", {"query": "searchable", "limit": 1})
        assert isinstance(search, dict)
        assert isinstance(search["hits"], list) and len(search["hits"]) == 1

        pending_revise: dict[str, JsonValue] = {
            "id": entry_id,
            "content": "changed",
            "expected_revision": 1,
        }
        assert await registry.reconcile("memory_revise", pending_revise) == {
            "status": "not_applied",
            "revision": 1,
        }
        await registry.execute("memory_revise", pending_revise)
        conflicting_revise: dict[str, JsonValue] = {
            "id": entry_id,
            "content": "different",
            "expected_revision": 1,
        }
        assert await registry.reconcile("memory_revise", conflicting_revise) == {
            "status": "conflict",
            "revision": 2,
        }
        pending_remove: dict[str, JsonValue] = {"id": entry_id, "expected_revision": 2}
        assert await registry.reconcile("memory_remove", pending_remove) == {
            "status": "not_applied",
            "revision": 2,
        }
        conflict_remove: dict[str, JsonValue] = {"id": entry_id, "expected_revision": 1}
        assert await registry.reconcile("memory_remove", conflict_remove) == {
            "status": "conflict",
            "revision": 2,
        }


def test_memory_tool_factory_validates_binding(tmp_path: Path) -> None:
    scope = MemoryScope("profile", "subject")
    with MemoryStore(tmp_path / "journal.sqlite3") as store:
        with pytest.raises(TypeError):
            MemoryTools(object(), scope)  # type: ignore[arg-type]
        with pytest.raises(TypeError):
            MemoryTools(store, object())  # type: ignore[arg-type]
