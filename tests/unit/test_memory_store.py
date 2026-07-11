from __future__ import annotations

import sqlite3
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pytest

from polaris.memory import (
    MemoryClosedError,
    MemoryConflictError,
    MemoryKind,
    MemoryNotFoundError,
    MemoryScope,
    MemoryStore,
    MemoryValidationError,
    MemoryWrite,
    TrustLevel,
)


def test_scope_isolation_and_snapshot_is_frozen(tmp_path: Path) -> None:
    path = tmp_path / "journal.sqlite3"
    first = MemoryScope("profile-a", "subject")
    second = MemoryScope("profile-b", "subject")
    with MemoryStore(path) as store:
        old = store.append(first, "first value")
        store.append(second, "private other-profile value")
        snapshot = store.session_snapshot(first)
        store.replace(first, old.id, "changed", expected_revision=old.revision)
        store.append(first, "new value")

        assert [entry.content for entry in snapshot.entries] == ["first value"]
        assert all(entry.profile_id == "profile-a" for entry in snapshot.entries)
        assert store.recall("private", first) == ()
        with pytest.raises(MemoryNotFoundError):
            store.get(first, store.list(second)[0].id)


def test_provenance_trust_revision_and_reopen(tmp_path: Path) -> None:
    path = tmp_path / "journal.sqlite3"
    scope = MemoryScope("person", "preferences")
    write = MemoryWrite(
        "Prefers concise responses",
        kind=MemoryKind.PREFERENCE,
        trust_level=TrustLevel.VERIFIED,
        provenance_run_id="run-1",
        provenance_session_id="session-1",
        provenance_message_id="message-1",
    )
    with MemoryStore(path) as store:
        entry = store.append(scope, write)
        assert store.schema_version == 2
        assert entry.revision == 1

    with MemoryStore(path) as reopened:
        persisted = reopened.get(scope, entry.id)
        assert persisted.kind is MemoryKind.PREFERENCE
        assert persisted.trust_level is TrustLevel.VERIFIED
        assert persisted.provenance_run_id == "run-1"
        assert persisted.provenance_session_id == "session-1"
        assert persisted.provenance_message_id == "message-1"
        assert reopened.schema_version == 2


def test_optimistic_replace_and_tombstone(tmp_path: Path) -> None:
    scope = MemoryScope("profile", "subject")
    with MemoryStore(tmp_path / "journal.sqlite3") as store:
        entry = store.append(scope, "one")
        revised = store.replace(
            scope,
            entry.id,
            "two",
            expected_revision=1,
            expected_hash=entry.content_hash,
        )
        assert revised.revision == 2
        with pytest.raises(MemoryConflictError):
            store.replace(scope, entry.id, "stale", expected_revision=1)

        removed = store.remove(scope, entry.id, expected_revision=2)
        assert removed.tombstoned
        assert removed.revision == 3
        with pytest.raises(MemoryNotFoundError):
            store.get(scope, entry.id)
        assert store.remove(scope, entry.id, expected_revision=2).revision == 3


def test_append_many_is_atomic(tmp_path: Path) -> None:
    scope = MemoryScope("profile", "subject")
    duplicate_id = "fixed-id"
    with MemoryStore(tmp_path / "journal.sqlite3") as store:
        with pytest.raises(MemoryConflictError):
            store.append_many(
                scope,
                (
                    MemoryWrite("first", entry_id=duplicate_id),
                    MemoryWrite("second", entry_id=duplicate_id),
                ),
            )
        assert store.list(scope) == ()


def test_event_idempotency_reuses_entry_and_rejects_conflicts(tmp_path: Path) -> None:
    scope = MemoryScope("profile", "telegram:10")
    write = MemoryWrite(
        "Remember this",
        kind=MemoryKind.FACT,
        provenance_session_id="telegram:20",
        provenance_message_id="30",
    )
    with MemoryStore(tmp_path / "journal.sqlite3") as store:
        first = store.append_idempotent(scope, write, "telegram:0:42:memory:add")
        repeated = store.append_idempotent(scope, write, "telegram:0:42:memory:add")

        assert repeated.id == first.id
        assert store.list(scope) == (first,)
        with pytest.raises(MemoryConflictError, match="different memory content"):
            store.append_idempotent(
                scope,
                MemoryWrite("Conflicting retry"),
                "telegram:0:42:memory:add",
            )
        assert store.list(scope) == (first,)


def test_content_only_revision_preserves_attributes_and_explicit_values_replace(
    tmp_path: Path,
) -> None:
    scope = MemoryScope("profile", "subject")
    with MemoryStore(tmp_path / "journal.sqlite3") as store:
        original = store.append(
            scope,
            MemoryWrite(
                "Original",
                kind=MemoryKind.PREFERENCE,
                trust_level=TrustLevel.VERIFIED,
                provenance_run_id="run-1",
                provenance_session_id="session-1",
                provenance_message_id="message-1",
            ),
        )
        preserved = store.replace(
            scope,
            original.id,
            "Revised",
            expected_revision=original.revision,
        )
        assert preserved.kind is MemoryKind.PREFERENCE
        assert preserved.trust_level is TrustLevel.VERIFIED
        assert preserved.provenance_run_id == "run-1"
        assert preserved.provenance_session_id == "session-1"
        assert preserved.provenance_message_id == "message-1"

        replaced = store.replace(
            scope,
            original.id,
            "Explicit",
            expected_revision=preserved.revision,
            kind=MemoryKind.FACT,
            trust_level=TrustLevel.MODEL_INFERRED,
            provenance_run_id=None,
            provenance_session_id="session-2",
            provenance_message_id=None,
        )
        assert replaced.kind is MemoryKind.FACT
        assert replaced.trust_level is TrustLevel.MODEL_INFERRED
        assert replaced.provenance_run_id is None
        assert replaced.provenance_session_id == "session-2"
        assert replaced.provenance_message_id is None


def test_like_recall_is_ranked_deterministically_and_limited(tmp_path: Path) -> None:
    scope = MemoryScope("profile", "subject")
    with MemoryStore(tmp_path / "journal.sqlite3", enable_fts=False) as store:
        store.append(scope, "alpha once")
        repeated = store.append(scope, "alpha alpha twice")
        store.append(scope, "alpha third")
        hits = store.recall("alpha", scope, limit=1)

        assert store.search_backend == "like"
        assert store.fallback_reason == "disabled"
        assert [hit.id for hit in hits] == [repeated.id]
        assert hits[0].search_backend == "like"


def test_fts_recall_reports_backend_when_supported(tmp_path: Path) -> None:
    scope = MemoryScope("profile", "subject")
    with MemoryStore(tmp_path / "journal.sqlite3") as store:
        store.append(scope, "zebra")
        store.append(scope, "zebra zebra")
        hits = store.recall("zebra", scope, limit=1)

        assert len(hits) == 1
        assert hits[0].search_backend == store.search_backend
        assert store.search_backend in {"fts5", "like"}
        if store.search_backend == "like":
            assert store.fallback_reason is not None


def test_concurrent_writers_and_revision_conflict(tmp_path: Path) -> None:
    scope = MemoryScope("profile", "subject")
    with MemoryStore(tmp_path / "journal.sqlite3") as store:
        with ThreadPoolExecutor(max_workers=8) as pool:
            entries = list(pool.map(lambda index: store.append(scope, f"value {index}"), range(24)))
        assert len(store.list(scope)) == 24

        target = entries[0]

        def revise(value: str) -> bool:
            try:
                store.replace(scope, target.id, value, expected_revision=1)
            except MemoryConflictError:
                return False
            return True

        with ThreadPoolExecutor(max_workers=2) as pool:
            outcomes = list(pool.map(revise, ("winner-a", "winner-b")))
        assert outcomes.count(True) == 1
        assert store.get(scope, target.id).revision == 2


def test_memory_migration_does_not_use_journal_schema_table(tmp_path: Path) -> None:
    path = tmp_path / "journal.sqlite3"
    with sqlite3.connect(path) as connection:
        connection.execute("CREATE TABLE schema_version(version INTEGER PRIMARY KEY)")
        connection.execute("INSERT INTO schema_version(version) VALUES (99)")
    with MemoryStore(path) as store:
        assert store.schema_version == 2
        store.append(MemoryScope("p", "s"), "value")
    with sqlite3.connect(path) as connection:
        assert connection.execute("SELECT version FROM schema_version").fetchone() == (99,)


def test_v1_memory_database_migrates_idempotency_table(tmp_path: Path) -> None:
    path = tmp_path / "journal.sqlite3"
    with MemoryStore(path):
        pass
    with sqlite3.connect(path) as connection:
        connection.execute("DROP TABLE memory_idempotency")
        connection.execute(
            "DELETE FROM memory_schema_migrations WHERE namespace = ?",
            ("polaris.curated_memory",),
        )
        connection.execute(
            """
            INSERT INTO memory_schema_migrations(namespace, version, applied_at)
            VALUES (?, 1, '2026-01-01T00:00:00Z')
            """,
            ("polaris.curated_memory",),
        )

    scope = MemoryScope("profile", "subject")
    with MemoryStore(path) as migrated:
        assert migrated.schema_version == 2
        first = migrated.append_idempotent(scope, MemoryWrite("value"), "event-1")
        assert migrated.append_idempotent(scope, MemoryWrite("value"), "event-1") == first


def test_budgets_validation_and_closed_store(tmp_path: Path) -> None:
    scope = MemoryScope("profile", "subject")
    store = MemoryStore(tmp_path / "journal.sqlite3")
    first = store.append(scope, content="12345")
    store.append(scope, content="x" * 100)

    snapshot = store.session_snapshot(scope, char_budget=5, token_budget=2)
    assert [entry.id for entry in snapshot.entries] == [first.id]
    assert store.render_prompt_context(snapshot, char_budget=1) == ""
    assert store.recall("12345", scope, limit=0) == ()
    assert store.list(scope, limit=1) == (first,)
    with pytest.raises(MemoryValidationError):
        store.recall("", scope)
    with pytest.raises(MemoryValidationError):
        store.list(scope, limit=-1)
    with pytest.raises(MemoryValidationError):
        store.session_snapshot(scope, char_budget=-1)
    with pytest.raises(MemoryValidationError):
        store.render_prompt_context(snapshot, token_budget=-1)
    store.close()
    store.close()
    with pytest.raises(MemoryClosedError):
        store.list(scope)


def test_scope_free_write_validation_and_entry_properties(tmp_path: Path) -> None:
    with pytest.raises(ValueError):
        MemoryScope("", "subject")
    with pytest.raises(TypeError):
        MemoryScope(1, "subject")  # type: ignore[arg-type]
    with pytest.raises(ValueError):
        MemoryWrite(" ")
    with pytest.raises(TypeError):
        MemoryWrite(3)  # type: ignore[arg-type]
    with pytest.raises(ValueError):
        MemoryWrite("ok", entry_id="")

    scope = MemoryScope("profile", "subject")
    with MemoryStore(tmp_path / "journal.sqlite3") as store:
        entry = store.append(scope, MemoryWrite("value", entry_id="entry"))
        hit = store.recall("value", scope)[0]
        assert entry.scope == scope
        assert hit.entry_id == entry.id
        assert hit.id == entry.id
        assert hit.revision == 1
        assert hit.content_hash == entry.content_hash
        with pytest.raises(MemoryValidationError):
            store.append(scope)
        with pytest.raises(MemoryValidationError):
            store.append(scope, "twice", content="twice")
        with pytest.raises(TypeError):
            store.append_many(scope, ("not-a-write",))  # type: ignore[arg-type]
