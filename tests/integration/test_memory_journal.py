from __future__ import annotations

import sqlite3
from pathlib import Path

from polaris.journal import Journal
from polaris.memory import MemoryScope, MemoryStore


def test_memory_uses_journal_path_with_independent_connection(tmp_path: Path) -> None:
    path = tmp_path / "journal.sqlite3"
    journal = Journal(path)
    memory = MemoryStore(path)
    try:
        entry = memory.append(MemoryScope("profile", "subject"), "durable value")
        assert entry.content == "durable value"
        journal_tables = {
            row[0]
            for row in memory._connection.execute(  # noqa: SLF001
                "SELECT name FROM sqlite_master WHERE type = 'table'"
            )
        }
        assert {"runs", "memory_entries", "memory_revisions"} <= journal_tables
        assert journal._connection is not memory._connection  # noqa: SLF001
    finally:
        memory.close()
        journal.close()


def test_two_memory_connections_coordinate_writes(tmp_path: Path) -> None:
    path = tmp_path / "journal.sqlite3"
    scope = MemoryScope("profile", "subject")
    with MemoryStore(path) as first, MemoryStore(path) as second:
        one = first.append(scope, "one")
        two = second.append(scope, "two")
        assert {entry.id for entry in first.list(scope)} == {one.id, two.id}
        assert {entry.id for entry in second.list(scope)} == {one.id, two.id}
    with sqlite3.connect(path) as connection:
        assert connection.execute("PRAGMA integrity_check").fetchone() == ("ok",)
