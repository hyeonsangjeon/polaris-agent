from __future__ import annotations

import sqlite3
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime, timedelta
from pathlib import Path
from time import sleep

import pytest

from polaris.journal import (
    BudgetExceededError,
    InvalidTransitionError,
    Journal,
    JournalConflictError,
    LeaseExpiredError,
    OwnershipError,
    RunStatus,
    SafetyClass,
    StepStatus,
)


def make_run(journal: Journal, **limits: int) -> str:
    return journal.create_run("test", {"task": "journal"}, {}, limits).id


def test_schema_persistence_events_and_transitions(tmp_path: Path) -> None:
    path = tmp_path / "state" / "journal.sqlite3"
    with Journal(path) as journal:
        run = journal.create_run("interactive", {"prompt": "hello"}, {"model": "test"})
        assert journal.schema_version == 3
        journal.mark_run_status(run.id, RunStatus.RUNNING)
        event = journal.append_event(run.id, "custom.event", {"ok": True})
        assert event.type == "custom.event"

    with Journal(path) as journal:
        assert journal.get_run(run.id).status is RunStatus.RUNNING
        assert [event.type for event in journal.materialized_timeline(run.id)] == [
            "run.created",
            "run.status_changed",
            "custom.event",
        ]
        journal.mark_run_status(run.id, RunStatus.COMPLETED)
        with pytest.raises(InvalidTransitionError):
            journal.mark_run_status(run.id, RunStatus.RUNNING)


def test_external_run_key_is_atomic_idempotent_and_rejects_mismatch(
    tmp_path: Path,
) -> None:
    path = tmp_path / "journal.sqlite3"
    journals = (Journal(path), Journal(path))

    def create(index: int) -> str:
        return journals[index].create_run(
            "single",
            {"prompt": "hello"},
            {"provider": "fake"},
            external_key="telegram:update:42",
        ).id

    with ThreadPoolExecutor(max_workers=2) as pool:
        run_ids = list(pool.map(create, range(2)))
    for journal in journals:
        journal.close()
    assert run_ids[0] == run_ids[1]

    with Journal(path) as journal:
        assert len(journal.list_runs()) == 1
        assert (
            journal.create_run(
                "single",
                {"prompt": "hello"},
                {"provider": "fake"},
                external_key="telegram:update:42",
            ).id
            == run_ids[0]
        )
        with pytest.raises(JournalConflictError, match="external key"):
            journal.create_run(
                "single",
                {"prompt": "different"},
                {"provider": "fake"},
                external_key="telegram:update:42",
            )
        with pytest.raises(JournalConflictError, match="external key"):
            journal.create_run(
                "single",
                {"prompt": "hello"},
                {"provider": "fake"},
                {"max_calls": 1},
                external_key="telegram:update:42",
            )
        parent = journal.create_run("single", {"prompt": "parent"}, {})
        with pytest.raises(JournalConflictError, match="external key"):
            journal.create_run(
                "single",
                {"prompt": "hello"},
                {"provider": "fake"},
                parent_run_id=parent.id,
                external_key="telegram:update:42",
            )


def test_concurrent_first_openers_share_one_journal_migration(tmp_path: Path) -> None:
    path = tmp_path / "journal.sqlite3"

    def open_journal(_index: int) -> int:
        with Journal(path) as journal:
            return journal.schema_version

    with ThreadPoolExecutor(max_workers=2) as pool:
        versions = list(pool.map(open_journal, range(2)))

    assert versions == [3, 3]


def test_v1_database_migrates_external_keys_without_rebuilding_runs(
    tmp_path: Path,
) -> None:
    path = tmp_path / "journal.sqlite3"
    connection = sqlite3.connect(path)
    connection.executescript(
        """
        CREATE TABLE schema_version (
            version INTEGER PRIMARY KEY,
            applied_at TEXT NOT NULL
        );
        INSERT INTO schema_version VALUES (1, '2026-01-01T00:00:00Z');
        CREATE TABLE runs (
            id TEXT PRIMARY KEY,
            mode TEXT NOT NULL,
            request_json TEXT NOT NULL,
            config_json TEXT NOT NULL,
            status TEXT NOT NULL,
            parent_run_id TEXT REFERENCES runs(id),
            call_limit INTEGER,
            token_limit INTEGER,
            micro_usd_limit INTEGER,
            wall_seconds_limit REAL,
            reserved_calls INTEGER NOT NULL DEFAULT 0,
            reserved_tokens INTEGER NOT NULL DEFAULT 0,
            reserved_micro_usd INTEGER NOT NULL DEFAULT 0,
            reserved_wall_seconds REAL NOT NULL DEFAULT 0,
            used_calls INTEGER NOT NULL DEFAULT 0,
            used_tokens INTEGER NOT NULL DEFAULT 0,
            used_micro_usd INTEGER NOT NULL DEFAULT 0,
            used_wall_seconds REAL NOT NULL DEFAULT 0,
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL
        );
        INSERT INTO runs(
            id, mode, request_json, config_json, status, created_at, updated_at
        ) VALUES (
            'run_existing', 'single', '{"prompt":"old"}', '{}', 'created',
            '2026-01-01T00:00:00Z', '2026-01-01T00:00:00Z'
        );
        CREATE TABLE events (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            run_id TEXT NOT NULL REFERENCES runs(id),
            step_id TEXT,
            type TEXT NOT NULL,
            payload_json TEXT NOT NULL,
            created_at TEXT NOT NULL
        );
        """
    )
    connection.close()

    with Journal(path) as journal:
        assert journal.schema_version == 3
        assert journal.get_run("run_existing").request["prompt"] == "old"
        created = journal.create_run(
            "single",
            {"prompt": "new"},
            {},
            external_key="slack:event:E1",
        )
        assert (
            journal.create_run(
                "single",
                {"prompt": "new"},
                {},
                external_key="slack:event:E1",
            ).id
            == created.id
        )

    connection = sqlite3.connect(path)
    assert connection.execute(
        "SELECT run_id FROM run_external_keys WHERE external_key = 'slack:event:E1'"
    ).fetchone() == (created.id,)
    connection.close()


def test_v2_external_key_identity_migrates_budget_and_parent(tmp_path: Path) -> None:
    path = tmp_path / "journal.sqlite3"
    connection = sqlite3.connect(path)
    connection.executescript(
        """
        CREATE TABLE schema_version (
            version INTEGER PRIMARY KEY,
            applied_at TEXT NOT NULL
        );
        INSERT INTO schema_version VALUES (2, '2026-01-01T00:00:00Z');
        CREATE TABLE runs (
            id TEXT PRIMARY KEY,
            parent_run_id TEXT,
            call_limit INTEGER,
            token_limit INTEGER,
            micro_usd_limit INTEGER,
            wall_seconds_limit REAL
        );
        INSERT INTO runs VALUES ('run-parent', NULL, NULL, NULL, NULL, NULL);
        INSERT INTO runs VALUES ('run-child', 'run-parent', 2, 300, 400, 5.0);
        CREATE TABLE run_external_keys (
            external_key TEXT NOT NULL UNIQUE,
            run_id TEXT NOT NULL REFERENCES runs(id),
            mode_hash TEXT NOT NULL,
            request_hash TEXT NOT NULL,
            config_hash TEXT NOT NULL,
            created_at TEXT NOT NULL
        );
        INSERT INTO run_external_keys VALUES (
            'event-1', 'run-child', 'mode', 'request', 'config',
            '2026-01-01T00:00:00Z'
        );
        """
    )
    connection.close()

    with Journal(path) as journal:
        assert journal.schema_version == 3

    connection = sqlite3.connect(path)
    migrated = connection.execute(
        """
        SELECT budget_hash, parent_run_id
        FROM run_external_keys WHERE external_key = 'event-1'
        """
    ).fetchone()
    assert migrated is not None
    assert migrated[0]
    assert migrated[1] == "run-parent"
    connection.close()


def test_two_claimers_only_claim_once(tmp_path: Path) -> None:
    path = tmp_path / "journal.sqlite3"
    with Journal(path) as setup:
        run_id = make_run(setup)
        step_id = setup.create_step(
            run_id, "tool", "read", {"path": "a"}, SafetyClass.READ_ONLY
        ).id

    def claim(worker: str) -> str | None:
        with Journal(path) as journal:
            step = journal.claim_ready_step(worker, 30, run_id)
            return None if step is None else step.id

    with ThreadPoolExecutor(max_workers=2) as pool:
        claimed = list(pool.map(claim, ("one", "two")))
    assert sorted(item for item in claimed if item is not None) == [step_id]


def test_ownership_heartbeat_and_transitions(tmp_path: Path) -> None:
    with Journal(tmp_path / "journal.sqlite3") as journal:
        run_id = make_run(journal)
        step = journal.create_step(
            run_id, "tool", "read", {}, SafetyClass.READ_ONLY
        )
        claimed = journal.claim_ready_step("owner", 30)
        assert claimed is not None and claimed.id == step.id
        with pytest.raises(OwnershipError):
            journal.heartbeat(step.id, "other", 30)
        heartbeat = journal.heartbeat(step.id, "owner", 30)
        assert heartbeat.lease_expires_at is not None
        executing = journal.mark_executing(step.id, "owner")
        assert executing.status is StepStatus.EXECUTING
        with pytest.raises(InvalidTransitionError):
            journal.mark_executing(step.id, "owner")
        committed = journal.commit_step(step.id, "owner", {"result": 1})
        assert committed.status is StepStatus.COMMITTED
        with pytest.raises(InvalidTransitionError):
            journal.fail_step(step.id, "owner", {"message": "late"})


@pytest.mark.parametrize(
    ("safety", "expected"),
    [
        (SafetyClass.READ_ONLY, StepStatus.READY),
        (SafetyClass.IDEMPOTENT, StepStatus.READY),
        (SafetyClass.RECONCILABLE, StepStatus.UNCERTAIN),
        (SafetyClass.OPAQUE_SIDE_EFFECT, StepStatus.UNCERTAIN),
    ],
)
def test_expired_lease_recovery(
    tmp_path: Path, safety: SafetyClass, expected: StepStatus
) -> None:
    with Journal(tmp_path / f"{safety}.sqlite3") as journal:
        run_id = make_run(journal)
        step = journal.create_step(run_id, "tool", safety.value, {}, safety)
        journal.claim_ready_step("worker", 0.01)
        future = datetime.now(UTC) + timedelta(seconds=1)
        reclaimed = journal.reclaim_expired_leases(future)
        assert [item.id for item in reclaimed] == [step.id]
        assert reclaimed[0].status is expected


def test_expired_owner_cannot_act(tmp_path: Path) -> None:
    with Journal(tmp_path / "journal.sqlite3") as journal:
        run_id = make_run(journal)
        step = journal.create_step(run_id, "tool", "read", {}, SafetyClass.READ_ONLY)
        journal.claim_ready_step("worker", 0.01)
        sleep(0.02)
        with pytest.raises(LeaseExpiredError):
            journal.heartbeat(step.id, "worker", 30)


def test_atomic_budget_reservations(tmp_path: Path) -> None:
    path = tmp_path / "journal.sqlite3"
    with Journal(path) as setup:
        run_id = make_run(setup, max_calls=1, max_tokens=10, max_micro_usd=100)

    def reserve() -> str | None:
        with Journal(path) as journal:
            try:
                return journal.reserve_budget(
                    run_id, calls=1, tokens=10, micro_usd=100
                )
            except BudgetExceededError:
                return None

    with ThreadPoolExecutor(max_workers=2) as pool:
        reservations = list(pool.map(lambda _: reserve(), range(2)))
    assert len([item for item in reservations if item]) == 1

    reservation = next(item for item in reservations if item)
    with Journal(path) as journal:
        budget = journal.settle_budget(
            run_id,
            reservation,
            actual_calls=1,
            actual_tokens=7,
            actual_micro_usd=90,
        )
        assert budget.reserved_calls == 0
        assert budget.used_calls == 1
        with pytest.raises(BudgetExceededError):
            journal.reserve_budget(run_id, calls=1)
