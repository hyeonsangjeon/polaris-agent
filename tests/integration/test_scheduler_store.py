from __future__ import annotations

import sqlite3
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from polaris.journal import Journal
from polaris.scheduler import (
    CatchupPolicy,
    DeliveryStatus,
    JobPayload,
    JobRunStatus,
    JobState,
    SchedulerClosedError,
    SchedulerConflictError,
    SchedulerNotFoundError,
    SchedulerOwnershipError,
    SchedulerStore,
    SchedulerValidationError,
    ScheduleSpec,
)


def dt(value: str) -> datetime:
    return datetime.fromisoformat(value).astimezone(UTC)


def payload(*, delivery: bool = False) -> JobPayload:
    target = {"channel": "test"} if delivery else None
    return JobPayload("single", {"prompt": "hello"}, target)


def interval_job(
    store: SchedulerStore,
    *,
    job_id: str,
    policy: CatchupPolicy,
    maximum: int = 1,
    grace_seconds: float = 0,
) -> None:
    store.create_job(
        ScheduleSpec.interval(60, start_at=dt("2026-01-01T00:01:00+00:00")),
        payload(),
        job_id=job_id,
        catchup_policy=policy,
        max_catchup=maximum,
        grace_seconds=grace_seconds,
        now=dt("2026-01-01T00:00:00+00:00"),
    )


def test_reopen_migrations_and_shared_journal_file(tmp_path: Path) -> None:
    path = tmp_path / "journal.sqlite3"
    with Journal(path), SchedulerStore(path) as store:
        assert store.schema_version == 2
        store.create_job(
            ScheduleSpec.once("2026-01-01T00:01:00+00:00"),
            payload(),
            job_id="one",
            now=dt("2026-01-01T00:00:00+00:00"),
        )
    with SchedulerStore(path) as reopened:
        assert reopened.get_job("one").schedule.timezone == "UTC"
        assert len(reopened.list_jobs()) == 1
    connection = sqlite3.connect(path)
    assert connection.execute("PRAGMA journal_mode").fetchone()[0] == "wal"
    assert connection.execute(
        "SELECT COUNT(*) FROM scheduler_schema_migrations WHERE scope = 'scheduler'"
    ).fetchone()[0] == 2
    connection.close()


def test_v1_run_payload_is_snapshotted_during_migration(tmp_path: Path) -> None:
    path = tmp_path / "journal.sqlite3"
    connection = sqlite3.connect(path)
    connection.executescript(
        """
        CREATE TABLE scheduler_schema_migrations (
            scope TEXT NOT NULL,
            version INTEGER NOT NULL,
            applied_at TEXT NOT NULL,
            PRIMARY KEY (scope, version)
        );
        INSERT INTO scheduler_schema_migrations
            VALUES ('scheduler', 1, '2026-01-01T00:00:00+00:00');
        CREATE TABLE jobs (
            id TEXT PRIMARY KEY,
            name TEXT NOT NULL,
            schedule_kind TEXT NOT NULL,
            schedule_json TEXT NOT NULL,
            payload_json TEXT NOT NULL,
            timezone TEXT NOT NULL,
            catchup_policy TEXT NOT NULL,
            max_catchup INTEGER NOT NULL,
            grace_seconds REAL NOT NULL,
            state TEXT NOT NULL,
            next_run_at TEXT,
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL,
            version INTEGER NOT NULL
        );
        INSERT INTO jobs VALUES (
            'job', 'job', 'once',
            '{"cron":null,"interval_seconds":null,"kind":"once","once_at":"2026-01-01T00:00:00+00:00","start_at":null,"timezone":"UTC"}',
            '{"delivery":{"channel":"before"},"mode":"single","request":{"prompt":"original"}}',
            'UTC', 'fire_once', 1, 0, 'completed', NULL,
            '2026-01-01T00:00:00+00:00', '2026-01-01T00:00:00+00:00', 1
        );
        CREATE TABLE job_runs (
            id TEXT PRIMARY KEY,
            job_id TEXT NOT NULL REFERENCES jobs(id),
            scheduled_for TEXT NOT NULL,
            status TEXT NOT NULL,
            attempt INTEGER NOT NULL DEFAULT 1,
            owner TEXT,
            lease_expires_at TEXT,
            polaris_run_id TEXT,
            cancel_requested INTEGER NOT NULL DEFAULT 0,
            execution_error TEXT,
            delivery_status TEXT NOT NULL,
            delivery_error TEXT,
            claimed_at TEXT NOT NULL,
            started_at TEXT,
            completed_at TEXT,
            updated_at TEXT NOT NULL,
            UNIQUE (job_id, scheduled_for)
        );
        INSERT INTO job_runs VALUES (
            'run', 'job', '2026-01-01T00:00:00+00:00', 'succeeded', 1,
            NULL, NULL, 'agent-run', 0, NULL, 'pending', NULL,
            '2026-01-01T00:00:00+00:00', '2026-01-01T00:00:00+00:00',
            '2026-01-01T00:00:01+00:00', '2026-01-01T00:00:01+00:00'
        );
        """
    )
    connection.close()

    with SchedulerStore(path) as store:
        assert store.schema_version == 2
        migrated = store.get_run("run")
        assert migrated.payload == JobPayload(
            "single",
            {"prompt": "original"},
            {"channel": "before"},
        )
        store.update_job("job", payload=JobPayload.single({"prompt": "changed"}))
        assert store.get_run("run").payload == migrated.payload

    connection = sqlite3.connect(path)
    with pytest.raises(sqlite3.IntegrityError, match="immutable"):
        connection.execute(
            "UPDATE job_runs SET payload_json = '{}' WHERE id = 'run'"
        )
    connection.close()


def test_crud_pause_resume_and_soft_delete(tmp_path: Path) -> None:
    with SchedulerStore(tmp_path / "journal.sqlite3") as store:
        job = store.create(
            ScheduleSpec.interval(10),
            payload(),
            job_id="job",
            name="original",
            now=dt("2026-01-01T00:00:00+00:00"),
        )
        assert job.next_run_at == dt("2026-01-01T00:00:10+00:00")
        assert store.pause("job").state is JobState.PAUSED
        assert store.resume("job").state is JobState.SCHEDULED
        assert store.update("job", name="changed").name == "changed"
        assert store.delete("job").state is JobState.CANCELLED


def test_fire_once_skip_and_bounded_catchup(tmp_path: Path) -> None:
    with SchedulerStore(tmp_path / "journal.sqlite3") as store:
        interval_job(store, job_id="once", policy=CatchupPolicy.FIRE_ONCE)
        interval_job(store, job_id="skip", policy=CatchupPolicy.SKIP)
        interval_job(store, job_id="bounded", policy=CatchupPolicy.BOUNDED, maximum=3)
        runs = store.claim_due_runs(
            dt("2026-01-01T00:10:20+00:00"),
            "owner",
            30,
            10,
        )
        scheduled = {
            run.job_id: run.scheduled_for for run in runs if run.job_id != "bounded"
        }
        assert scheduled == {"once": dt("2026-01-01T00:10:00+00:00")}
        bounded = [run.scheduled_for for run in runs if run.job_id == "bounded"]
        assert bounded == [
            dt("2026-01-01T00:08:00+00:00"),
            dt("2026-01-01T00:09:00+00:00"),
            dt("2026-01-01T00:10:00+00:00"),
        ]
        assert store.list_runs(job_id="skip") == ()
        assert all(
            store.get_job(job_id).next_run_at == dt("2026-01-01T00:11:00+00:00")
            for job_id in ("once", "skip", "bounded")
        )


def test_skip_executes_latest_occurrence_within_grace(tmp_path: Path) -> None:
    with SchedulerStore(tmp_path / "journal.sqlite3") as store:
        interval_job(
            store,
            job_id="in-grace",
            policy=CatchupPolicy.SKIP,
            grace_seconds=30,
        )
        interval_job(
            store,
            job_id="missed",
            policy=CatchupPolicy.SKIP,
            grace_seconds=19,
        )

        runs = store.claim_due_runs(
            dt("2026-01-01T00:10:20+00:00"),
            "owner",
            30,
            10,
        )

        assert [(run.job_id, run.scheduled_for) for run in runs] == [
            ("in-grace", dt("2026-01-01T00:10:00+00:00"))
        ]
        assert store.list_runs(job_id="missed") == ()


def test_two_connections_cannot_duplicate_due_occurrence(tmp_path: Path) -> None:
    path = tmp_path / "journal.sqlite3"
    first = SchedulerStore(path)
    second = SchedulerStore(path)
    first.create_job(
        ScheduleSpec.once("2026-01-01T00:00:00+00:00"),
        payload(),
        job_id="job",
        now=dt("2025-12-31T23:00:00+00:00"),
    )

    def claim(store: SchedulerStore, owner: str) -> int:
        return len(store.claim_due_runs(dt("2026-01-01T00:00:00+00:00"), owner, 30, 5))

    with ThreadPoolExecutor(max_workers=2) as executor:
        counts = list(executor.map(claim, (first, second), ("one", "two")))
    assert sum(counts) == 1
    assert len(first.list_runs(job_id="job")) == 1
    assert first.get_job("job").state is JobState.COMPLETED
    first.close()
    second.close()


def test_unique_job_and_occurrence_constraint(tmp_path: Path) -> None:
    path = tmp_path / "journal.sqlite3"
    with SchedulerStore(path) as store:
        store.create_job(
            ScheduleSpec.once("2026-01-01T00:00:00+00:00"),
            payload(),
            job_id="job",
            now=dt("2025-01-01T00:00:00+00:00"),
        )
        with pytest.raises(SchedulerConflictError):
            store.create_job(
                ScheduleSpec.once("2026-01-01T00:00:00+00:00"),
                payload(),
                job_id="job",
            )
        store.claim_due_runs(dt("2026-01-01T00:00:00+00:00"), "owner", 30)
    connection = sqlite3.connect(path)
    with pytest.raises(sqlite3.IntegrityError):
        connection.execute(
            """
            INSERT INTO job_runs(
                id, job_id, scheduled_for, status, attempt, delivery_status,
                claimed_at, updated_at
            )
            SELECT 'duplicate', job_id, scheduled_for, status, attempt, delivery_status,
                claimed_at, updated_at FROM job_runs LIMIT 1
            """
        )
    connection.close()


def test_heartbeat_ownership_expiry_and_explicit_retry(tmp_path: Path) -> None:
    with SchedulerStore(tmp_path / "journal.sqlite3") as store:
        store.create_job(
            ScheduleSpec.once("2026-01-01T00:00:00+00:00"),
            payload(),
            now=dt("2025-01-01T00:00:00+00:00"),
        )
        run = store.claim_due_runs(dt("2026-01-01T00:00:00+00:00"), "owner", 10)[0]
        heartbeat = store.heartbeat(
            run.id,
            "owner",
            20,
            now=dt("2026-01-01T00:00:05+00:00"),
        )
        assert heartbeat.lease_expires_at == dt("2026-01-01T00:00:25+00:00")
        with pytest.raises(SchedulerOwnershipError):
            store.heartbeat(run.id, "other", 20, now=dt("2026-01-01T00:00:06+00:00"))
        with pytest.raises(SchedulerOwnershipError):
            store.heartbeat(run.id, "owner", 20, now=dt("2026-01-01T00:00:25+00:00"))
        stale = store.recover_stale_runs(dt("2026-01-01T00:00:25+00:00"))
        assert stale[0].status is JobRunStatus.INTERRUPTED
        assert store.claim_due_runs(dt("2026-01-02T00:00:00+00:00"), "other", 10) == ()
        with pytest.raises(SchedulerValidationError):
            store.create_retry(run.id, "other", 10, approved=False)
        retry = store.create_retry(
            run.id,
            "other",
            10,
            approved=True,
            now=dt("2026-01-02T00:00:00+00:00"),
        )
        assert retry.attempt == 2
        assert retry.scheduled_for == run.scheduled_for
        assert retry.status is JobRunStatus.CLAIMED


def test_two_connections_cannot_retry_same_occurrence(tmp_path: Path) -> None:
    path = tmp_path / "journal.sqlite3"
    first = SchedulerStore(path)
    second = SchedulerStore(path)
    first.create_job(
        ScheduleSpec.once("2026-01-01T00:00:00+00:00"),
        payload(),
        now=dt("2025-01-01T00:00:00+00:00"),
    )
    run = first.claim_due_runs(dt("2026-01-01T00:00:00+00:00"), "original", 1)[0]
    first.recover_stale_runs(dt("2026-01-01T00:00:01+00:00"))

    def retry(store: SchedulerStore, owner: str) -> bool:
        try:
            store.create_retry(
                run.id,
                owner,
                30,
                approved=True,
                now=dt("2026-01-01T00:00:02+00:00"),
            )
        except SchedulerConflictError:
            return False
        return True

    with ThreadPoolExecutor(max_workers=2) as executor:
        retried = list(executor.map(retry, (first, second), ("one", "two")))

    assert sum(retried) == 1
    assert first.get_run(run.id).attempt == 2
    first.close()
    second.close()


@pytest.mark.parametrize(
    ("policy", "maximum", "grace"),
    [
        (CatchupPolicy.BOUNDED, 11, 0),
        (CatchupPolicy.FIRE_ONCE, 2, 0),
        (CatchupPolicy.FIRE_ONCE, 1, -1),
        (CatchupPolicy.FIRE_ONCE, 1, float("nan")),
    ],
)
def test_invalid_catchup_and_grace_rejected(
    tmp_path: Path,
    policy: CatchupPolicy,
    maximum: int,
    grace: float,
) -> None:
    with (
        SchedulerStore(tmp_path / "journal.sqlite3") as store,
        pytest.raises(SchedulerValidationError),
    ):
        store.create_job(
            ScheduleSpec.interval(1),
            payload(),
            catchup_policy=policy,
            max_catchup=maximum,
            grace_seconds=grace,
        )


def test_cron_bounded_catchup_keeps_latest_occurrences(tmp_path: Path) -> None:
    with SchedulerStore(tmp_path / "journal.sqlite3") as store:
        job = store.create_job(
            ScheduleSpec.cron_schedule("* * * * *"),
            payload(),
            catchup_policy=CatchupPolicy.BOUNDED,
            max_catchup=2,
            now=dt("2026-01-01T00:00:00+00:00"),
        )
        runs = store.claim_due_runs(dt("2026-01-01T00:05:30+00:00"), "owner", 30)
        assert [run.scheduled_for for run in runs] == [
            dt("2026-01-01T00:04:00+00:00"),
            dt("2026-01-01T00:05:00+00:00"),
        ]
        assert store.get_job(job.id).next_run_at == dt("2026-01-01T00:06:00+00:00")


def test_run_lifecycle_delivery_and_transition_guards(tmp_path: Path) -> None:
    now = dt("2026-01-01T00:00:00+00:00")
    with SchedulerStore(tmp_path / "journal.sqlite3") as store:
        store.create_job(
            ScheduleSpec.once(now),
            payload(delivery=True),
            now=dt("2025-01-01T00:00:00+00:00"),
        )
        run = store.claim_due_runs(now, "owner", 30)[0]
        with pytest.raises(SchedulerOwnershipError):
            store.mark_running(run.id, "other", now=now)
        running = store.mark_running(run.id, "owner", now=now, polaris_run_id="early")
        assert running.status is JobRunStatus.RUNNING
        assert store.set_polaris_run_id(run.id, "owner", "final").polaris_run_id == "final"
        completed = store.complete(run.id, "owner", polaris_run_id="final", now=now)
        assert completed.status is JobRunStatus.SUCCEEDED
        delivered = store.record_delivery(run.id, DeliveryStatus.SUCCEEDED, now=now)
        assert delivered.delivery_status is DeliveryStatus.SUCCEEDED
        assert store.request_cancel(run.id, now=now).cancel_requested
        with pytest.raises(SchedulerConflictError):
            store.complete(run.id, "owner", now=now)
        with pytest.raises(SchedulerConflictError):
            store.heartbeat(run.id, "owner", 10, now=now)
        with pytest.raises(SchedulerOwnershipError):
            store.set_polaris_run_id(run.id, "owner", "late")
        with pytest.raises(SchedulerValidationError):
            store.set_polaris_run_id(run.id, "owner", "")
        with pytest.raises(SchedulerValidationError):
            store.record_delivery(run.id, DeliveryStatus.PENDING)


def test_cancel_and_store_validation_paths(tmp_path: Path) -> None:
    now = dt("2026-01-01T00:00:00+00:00")
    store = SchedulerStore(tmp_path / "journal.sqlite3")
    job = store.create_job(
        ScheduleSpec.once(now),
        payload(),
        now=dt("2025-01-01T00:00:00+00:00"),
    )
    run = store.claim_due_runs(now, "owner", 30)[0]
    with pytest.raises(SchedulerOwnershipError):
        store.cancel(run.id, owner="other", now=now)
    assert store.cancel(run.id, owner="owner", now=now).status is JobRunStatus.CANCELLED
    assert store.cancel(run.id, now=now).status is JobRunStatus.CANCELLED
    assert len(store.list_runs(status=JobRunStatus.CANCELLED)) == 1
    with pytest.raises(SchedulerConflictError):
        store.resume_job(job.id)
    with pytest.raises(SchedulerNotFoundError):
        store.get_job("missing")
    with pytest.raises(SchedulerNotFoundError):
        store.get_run("missing")
    with pytest.raises(SchedulerNotFoundError):
        store.pause_job("missing")
    invalid_claims = (
        ("", 1, 1, None),
        ("x", 0, 1, None),
        ("x", 1, 0, None),
        ("x", 1, 1, 0),
    )
    for owner, lease, batch, cap in invalid_claims:
        with pytest.raises(SchedulerValidationError):
            store.claim_due_runs(now, owner, lease, batch, startup_cap=cap)
    with pytest.raises(SchedulerValidationError):
        store.heartbeat(run.id, "owner", 0)
    store.create_retry(run.id, "owner", 1, approved=True, now=now)
    with pytest.raises(SchedulerConflictError):
        store.create_retry(run.id, "owner", 1, approved=True)
    assert store.recover_stale_runs(now) == ()
    store.close()
    store.close()
    with pytest.raises(SchedulerClosedError):
        _ = store.schema_version
    with pytest.raises(SchedulerValidationError):
        SchedulerStore(tmp_path / "invalid.sqlite3", busy_timeout_ms=-1)


def test_update_all_mutable_job_fields_and_filters(tmp_path: Path) -> None:
    now = dt("2026-01-01T00:00:00+00:00")
    with SchedulerStore(tmp_path / "journal.sqlite3") as store:
        job = store.create_job(ScheduleSpec.interval(10), payload(), now=now)
        changed = store.update_job(
            job.id,
            schedule=ScheduleSpec.interval(20),
            payload=payload(delivery=True),
            catchup_policy=CatchupPolicy.BOUNDED,
            max_catchup=2,
            grace_seconds=5,
            now=now,
        )
        assert changed.version == 2
        assert changed.next_run_at == now + timedelta(seconds=20)
        assert changed.payload.delivery is not None
        assert changed.grace_seconds == 5
        store.pause_job(job.id, now=now)
        assert store.list_jobs(state=JobState.PAUSED)[0].id == job.id
        with pytest.raises(SchedulerValidationError):
            store.update_job(job.id, name=" ")
        with pytest.raises(SchedulerValidationError):
            store.update_job(job.id, payload=object())


def test_terminal_job_states_cannot_be_paused_or_resumed(tmp_path: Path) -> None:
    now = dt("2026-01-01T00:00:00+00:00")
    with SchedulerStore(tmp_path / "journal.sqlite3") as store:
        cancelled = store.create_job(ScheduleSpec.interval(10), payload(), now=now)
        store.cancel_job(cancelled.id, now=now)
        with pytest.raises(SchedulerConflictError, match="only scheduled jobs"):
            store.pause_job(cancelled.id, now=now)
        with pytest.raises(SchedulerConflictError, match="only paused jobs"):
            store.resume_job(cancelled.id, now=now)
        assert store.get_job(cancelled.id).state is JobState.CANCELLED

        completed = store.create_job(ScheduleSpec.once(now), payload(), now=now)
        store.claim_due_runs(now, "owner", 30)
        assert store.get_job(completed.id).state is JobState.COMPLETED
        with pytest.raises(SchedulerConflictError, match="only scheduled jobs"):
            store.pause_job(completed.id, now=now)
        assert store.cancel_job(completed.id, now=now).state is JobState.COMPLETED
        assert store.get_job(completed.id).state is JobState.COMPLETED


def test_occurrence_keeps_payload_and_delivery_snapshot(tmp_path: Path) -> None:
    now = dt("2026-01-01T00:00:00+00:00")
    original = JobPayload(
        "single",
        {"prompt": "original"},
        {"channel": "telegram", "chat_id": "before"},
    )
    replacement = JobPayload("single", {"prompt": "changed"})
    with SchedulerStore(tmp_path / "journal.sqlite3") as store:
        job = store.create_job(ScheduleSpec.once(now), original, now=now)
        run = store.claim_due_runs(now, "owner", 30)[0]
        store.update_job(job.id, payload=replacement, now=now)

        snapshot = store.get_run(run.id)
        assert snapshot.payload == original
        assert snapshot.delivery_status is DeliveryStatus.PENDING
        store.cancel(run.id, owner="owner", now=now)
        retried = store.create_retry(
            run.id,
            "retry-owner",
            30,
            approved=True,
            now=now,
        )
        assert retried.payload == original
        assert retried.delivery_status is DeliveryStatus.PENDING
        store.mark_running(run.id, "retry-owner", now=now)
        store.complete(run.id, "retry-owner", polaris_run_id="agent-run", now=now)
        assert store.list_pending_deliveries()[0].payload == original


def test_cancel_job_suppresses_committed_pending_delivery(tmp_path: Path) -> None:
    now = dt("2026-01-01T00:00:00+00:00")
    with SchedulerStore(tmp_path / "journal.sqlite3") as store:
        job = store.create_job(
            ScheduleSpec.once(now),
            payload(delivery=True),
            now=dt("2025-01-01T00:00:00+00:00"),
        )
        run = store.claim_due_runs(now, "owner", 30)[0]
        store.mark_running(run.id, "owner", now=now)
        store.set_polaris_run_id(run.id, "owner", "agent-run", now=now)
        succeeded = store.complete(run.id, "owner", now=now)
        assert store.list_pending_deliveries() == (succeeded,)

        store.cancel_job(job.id, now=now)
        suppressed = store.get_run(run.id)
        assert suppressed.cancel_requested
        assert suppressed.delivery_status is DeliveryStatus.SUPPRESSED
        assert store.list_pending_deliveries() == ()

        unchanged = store.record_delivery(
            run.id,
            DeliveryStatus.SUCCEEDED,
            now=now,
        )
        assert unchanged.delivery_status is DeliveryStatus.SUPPRESSED
