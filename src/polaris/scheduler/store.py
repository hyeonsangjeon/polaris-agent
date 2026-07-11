"""SQLite WAL-backed durable scheduler store."""

from __future__ import annotations

import json
import math
import random
import sqlite3
import threading
import uuid
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Final

from .cron import CronExpression
from .errors import (
    SchedulerClosedError,
    SchedulerConflictError,
    SchedulerNotFoundError,
    SchedulerOwnershipError,
    SchedulerValidationError,
)
from .models import (
    CatchupPolicy,
    DeliveryStatus,
    Job,
    JobPayload,
    JobRun,
    JobRunStatus,
    JobState,
    ScheduleKind,
    ScheduleSpec,
    ensure_aware,
    get_timezone,
    parse_timestamp,
    utc_now,
)

_SCHEMA_SCOPE: Final = "scheduler"
_SCHEMA_VERSION: Final = 2
_UNSET: Final = object()


def _timestamp(value: datetime) -> str:
    return ensure_aware(value).isoformat(timespec="microseconds")


def _from_timestamp(value: str | None) -> datetime | None:
    if value is None:
        return None
    return datetime.fromisoformat(value).astimezone(UTC)


def _payload_json(payload: JobPayload) -> str:
    return json.dumps(
        {
            "mode": payload.mode,
            "request": dict(payload.request),
            "delivery": None if payload.delivery is None else dict(payload.delivery),
        },
        sort_keys=True,
        separators=(",", ":"),
    )


def _payload_from_json(value: str) -> JobPayload:
    raw = json.loads(value)
    return JobPayload(raw["mode"], raw["request"], raw.get("delivery"))


def _schedule_json(schedule: ScheduleSpec) -> str:
    def encoded(value: str | datetime | None) -> str | None:
        if isinstance(value, datetime):
            return value.isoformat()
        return value

    return json.dumps(
        {
            "kind": ScheduleKind(schedule.kind).value,
            "once_at": encoded(schedule.once_at),
            "interval_seconds": schedule.interval_seconds,
            "cron": schedule.cron,
            "timezone": schedule.timezone,
            "start_at": encoded(schedule.start_at),
        },
        sort_keys=True,
        separators=(",", ":"),
    )


def _schedule_from_json(value: str) -> ScheduleSpec:
    raw = json.loads(value)
    return ScheduleSpec(**raw)


def compute_next_run(schedule: ScheduleSpec, after: datetime) -> datetime | None:
    """Return the first schedule occurrence strictly after ``after``."""
    after = ensure_aware(after, "after")
    if schedule.kind is ScheduleKind.ONCE:
        assert schedule.once_at is not None
        occurrence = parse_timestamp(schedule.once_at, schedule.timezone)
        return occurrence if occurrence > after else None
    if schedule.kind is ScheduleKind.INTERVAL:
        assert schedule.interval_seconds is not None
        if schedule.start_at is None:
            anchor = after
        else:
            anchor = parse_timestamp(schedule.start_at, schedule.timezone)
        if anchor > after:
            return anchor
        elapsed = (after - anchor).total_seconds()
        steps = int(elapsed // schedule.interval_seconds) + 1
        return anchor + timedelta(seconds=steps * schedule.interval_seconds)
    assert schedule.cron is not None
    return CronExpression(schedule.cron).next_after(after, get_timezone(schedule.timezone))


def preview_next_times(
    schedule: ScheduleSpec,
    after: datetime,
    count: int = 5,
) -> tuple[datetime, ...]:
    if count < 0 or count > 1000:
        raise SchedulerValidationError("count must be between 0 and 1000")
    cursor = ensure_aware(after, "after")
    result: list[datetime] = []
    for _ in range(count):
        next_run = compute_next_run(schedule, cursor)
        if next_run is None:
            break
        result.append(next_run)
        cursor = next_run
    return tuple(result)


class SchedulerStore:
    """Thread-safe scheduler state using an independent SQLite connection."""

    def __init__(self, path: str | Path, *, busy_timeout_ms: int = 5000) -> None:
        if busy_timeout_ms < 0:
            raise SchedulerValidationError("busy_timeout_ms must be non-negative")
        self.path = Path(path)
        if str(path) != ":memory:":
            self.path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.RLock()
        self._closed = False
        self._connection = sqlite3.connect(
            str(path),
            timeout=busy_timeout_ms / 1000,
            isolation_level=None,
            check_same_thread=False,
        )
        self._connection.row_factory = sqlite3.Row
        with self._lock:
            self._connection.execute("PRAGMA foreign_keys = ON")
            self._connection.execute(f"PRAGMA busy_timeout = {busy_timeout_ms:d}")
            self._connection.execute("PRAGMA synchronous = FULL")
            self._connection.execute("PRAGMA journal_mode = WAL")
            self._migrate()

    def __enter__(self) -> SchedulerStore:
        self._ensure_open()
        return self

    def __exit__(self, _type: object, _value: object, _traceback: object) -> None:
        self.close()

    @property
    def schema_version(self) -> int:
        with self._lock:
            self._ensure_open()
            row = self._connection.execute(
                """
                SELECT MAX(version) AS version
                FROM scheduler_schema_migrations WHERE scope = ?
                """,
                (_SCHEMA_SCOPE,),
            ).fetchone()
            return int(row["version"] or 0)

    def close(self) -> None:
        with self._lock:
            if not self._closed:
                self._connection.close()
                self._closed = True

    def _ensure_open(self) -> None:
        if self._closed:
            raise SchedulerClosedError("scheduler store is closed")

    @contextmanager
    def _transaction(self) -> Iterator[sqlite3.Connection]:
        with self._lock:
            self._ensure_open()
            self._connection.execute("BEGIN IMMEDIATE")
            try:
                yield self._connection
            except BaseException:
                self._connection.rollback()
                raise
            else:
                self._connection.commit()

    def _migrate(self) -> None:
        self._connection.execute(
            """
            CREATE TABLE IF NOT EXISTS scheduler_schema_migrations (
                scope TEXT NOT NULL,
                version INTEGER NOT NULL,
                applied_at TEXT NOT NULL,
                PRIMARY KEY (scope, version)
            )
            """
        )
        with self._transaction() as connection:
            row = connection.execute(
                """
                SELECT MAX(version) AS version
                FROM scheduler_schema_migrations WHERE scope = ?
                """,
                (_SCHEMA_SCOPE,),
            ).fetchone()
            version = int(row["version"] or 0)
            if version > _SCHEMA_VERSION:
                raise SchedulerValidationError(
                    f"scheduler schema {version} is newer than supported {_SCHEMA_VERSION}"
                )
            now = _timestamp(utc_now())
            if version == 0:
                connection.execute(
                    """
                CREATE TABLE IF NOT EXISTS jobs (
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
                    version INTEGER NOT NULL,
                    CHECK (max_catchup >= 1 AND max_catchup <= 10),
                    CHECK (grace_seconds >= 0)
                )
                """
                )
                connection.execute(
                    """
                CREATE INDEX IF NOT EXISTS scheduler_jobs_due_idx
                    ON jobs(state, next_run_at)
                """
                )
                connection.execute(
                    """
                CREATE TABLE IF NOT EXISTS job_runs (
                    id TEXT PRIMARY KEY,
                    job_id TEXT NOT NULL REFERENCES jobs(id),
                    scheduled_for TEXT NOT NULL,
                    payload_json TEXT NOT NULL,
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
                    UNIQUE (job_id, scheduled_for),
                    CHECK (attempt >= 1)
                )
                """
                )
                connection.execute(
                    """
                CREATE INDEX IF NOT EXISTS scheduler_runs_lease_idx
                    ON job_runs(status, lease_expires_at)
                """
                )
                connection.execute(
                    """
                CREATE INDEX IF NOT EXISTS scheduler_runs_job_idx
                    ON job_runs(job_id, scheduled_for)
                """
                )
                connection.execute(
                    """
                    CREATE TRIGGER IF NOT EXISTS scheduler_run_payload_immutable
                    BEFORE UPDATE OF payload_json ON job_runs
                    WHEN NEW.payload_json IS NOT OLD.payload_json
                    BEGIN
                        SELECT RAISE(ABORT, 'scheduled run payload is immutable');
                    END
                    """
                )
                connection.executemany(
                    """
                    INSERT INTO scheduler_schema_migrations(scope, version, applied_at)
                    VALUES (?, ?, ?)
                    """,
                    (
                        (_SCHEMA_SCOPE, 1, now),
                        (_SCHEMA_SCOPE, 2, now),
                    ),
                )
                version = _SCHEMA_VERSION
            if version == 1:
                connection.execute("ALTER TABLE job_runs ADD COLUMN payload_json TEXT")
                connection.execute(
                    """
                    UPDATE job_runs
                    SET payload_json = (
                        SELECT jobs.payload_json FROM jobs WHERE jobs.id = job_runs.job_id
                    )
                    WHERE payload_json IS NULL
                    """
                )
                connection.execute(
                    """
                    CREATE TRIGGER IF NOT EXISTS scheduler_run_payload_immutable
                    BEFORE UPDATE OF payload_json ON job_runs
                    WHEN NEW.payload_json IS NOT OLD.payload_json
                    BEGIN
                        SELECT RAISE(ABORT, 'scheduled run payload is immutable');
                    END
                    """
                )
                connection.execute(
                    """
                    INSERT INTO scheduler_schema_migrations(scope, version, applied_at)
                    VALUES (?, ?, ?)
                    """,
                    (_SCHEMA_SCOPE, 2, now),
                )

    @staticmethod
    def _validate_job_options(
        catchup_policy: CatchupPolicy | str,
        max_catchup: int,
        grace_seconds: float,
    ) -> CatchupPolicy:
        try:
            policy = CatchupPolicy(catchup_policy)
        except ValueError as exc:
            raise SchedulerValidationError("invalid catch-up policy") from exc
        if max_catchup < 1 or max_catchup > 10:
            raise SchedulerValidationError("max_catchup must be between 1 and 10")
        if not math.isfinite(grace_seconds) or grace_seconds < 0:
            raise SchedulerValidationError("grace_seconds must be finite and non-negative")
        if policy is not CatchupPolicy.BOUNDED and max_catchup != 1:
            raise SchedulerValidationError("max_catchup applies only to bounded catch-up")
        return policy

    def create_job(
        self,
        schedule: ScheduleSpec,
        payload: JobPayload,
        *,
        name: str = "",
        job_id: str | None = None,
        catchup_policy: CatchupPolicy | str = CatchupPolicy.FIRE_ONCE,
        max_catchup: int = 1,
        grace_seconds: float = 0,
        now: datetime | None = None,
    ) -> Job:
        now = ensure_aware(now or utc_now(), "now")
        policy = self._validate_job_options(catchup_policy, max_catchup, grace_seconds)
        if not isinstance(schedule, ScheduleSpec) or not isinstance(payload, JobPayload):
            raise SchedulerValidationError("schedule and payload must use scheduler models")
        if schedule.kind is ScheduleKind.INTERVAL and schedule.start_at is None:
            assert schedule.interval_seconds is not None
            schedule = ScheduleSpec.interval(
                schedule.interval_seconds,
                start_at=now + timedelta(seconds=schedule.interval_seconds),
                timezone=schedule.timezone,
            )
        next_run = self._initial_run(schedule, now)
        identifier = job_id or str(uuid.uuid4())
        if not identifier:
            raise SchedulerValidationError("job_id must not be empty")
        display_name = name.strip() or identifier
        stamp = _timestamp(now)
        try:
            with self._transaction() as connection:
                connection.execute(
                    """
                    INSERT INTO jobs(
                        id, name, schedule_kind, schedule_json, payload_json, timezone,
                        catchup_policy, max_catchup, grace_seconds, state, next_run_at,
                        created_at, updated_at, version
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 1)
                    """,
                    (
                        identifier,
                        display_name,
                        ScheduleKind(schedule.kind).value,
                        _schedule_json(schedule),
                        _payload_json(payload),
                        schedule.timezone,
                        policy.value,
                        max_catchup,
                        grace_seconds,
                        JobState.SCHEDULED.value,
                        None if next_run is None else _timestamp(next_run),
                        stamp,
                        stamp,
                    ),
                )
        except sqlite3.IntegrityError as exc:
            raise SchedulerConflictError(f"job {identifier!r} already exists") from exc
        return self.get_job(identifier)

    @staticmethod
    def _initial_run(schedule: ScheduleSpec, now: datetime) -> datetime | None:
        if schedule.kind is ScheduleKind.ONCE:
            assert schedule.once_at is not None
            occurrence = parse_timestamp(schedule.once_at, schedule.timezone)
            return occurrence if occurrence >= now else occurrence
        if schedule.kind is ScheduleKind.INTERVAL:
            assert schedule.start_at is not None
            anchor = parse_timestamp(schedule.start_at, schedule.timezone)
            if anchor >= now:
                return anchor
            return compute_next_run(schedule, now)
        # Cron creation never backfills time before the creation boundary.
        return compute_next_run(schedule, now)

    def get_job(self, job_id: str) -> Job:
        with self._lock:
            self._ensure_open()
            row = self._connection.execute("SELECT * FROM jobs WHERE id = ?", (job_id,)).fetchone()
        if row is None:
            raise SchedulerNotFoundError(f"job {job_id!r} was not found")
        return self._job_from_row(row)

    def list_jobs(self, *, state: JobState | str | None = None) -> tuple[Job, ...]:
        parameters: tuple[str, ...] = ()
        query = "SELECT * FROM jobs"
        if state is not None:
            query += " WHERE state = ?"
            parameters = (JobState(state).value,)
        query += " ORDER BY created_at, id"
        with self._lock:
            self._ensure_open()
            rows = self._connection.execute(query, parameters).fetchall()
        return tuple(self._job_from_row(row) for row in rows)

    def update_job(
        self,
        job_id: str,
        *,
        name: str | object = _UNSET,
        schedule: ScheduleSpec | object = _UNSET,
        payload: JobPayload | object = _UNSET,
        catchup_policy: CatchupPolicy | str | object = _UNSET,
        max_catchup: int | object = _UNSET,
        grace_seconds: float | object = _UNSET,
        now: datetime | None = None,
    ) -> Job:
        current = self.get_job(job_id)
        new_name = current.name if name is _UNSET else str(name).strip()
        if not new_name:
            raise SchedulerValidationError("name must not be empty")
        new_schedule = current.schedule if schedule is _UNSET else schedule
        new_payload = current.payload if payload is _UNSET else payload
        if not isinstance(new_schedule, ScheduleSpec) or not isinstance(new_payload, JobPayload):
            raise SchedulerValidationError("invalid schedule or payload")
        if catchup_policy is _UNSET:
            new_policy: CatchupPolicy | str = current.catchup_policy
        elif isinstance(catchup_policy, (CatchupPolicy, str)):
            new_policy = catchup_policy
        else:
            raise SchedulerValidationError("invalid catch-up policy")
        if max_catchup is _UNSET:
            new_max = current.max_catchup
        elif isinstance(max_catchup, int):
            new_max = max_catchup
        else:
            raise SchedulerValidationError("max_catchup must be an integer")
        if grace_seconds is _UNSET:
            new_grace = current.grace_seconds
        elif isinstance(grace_seconds, (int, float)):
            new_grace = float(grace_seconds)
        else:
            raise SchedulerValidationError("grace_seconds must be numeric")
        policy = self._validate_job_options(new_policy, new_max, new_grace)
        stamp_time = ensure_aware(now or utc_now(), "now")
        if new_schedule.kind is ScheduleKind.INTERVAL and new_schedule.start_at is None:
            assert new_schedule.interval_seconds is not None
            new_schedule = ScheduleSpec.interval(
                new_schedule.interval_seconds,
                start_at=stamp_time + timedelta(seconds=new_schedule.interval_seconds),
                timezone=new_schedule.timezone,
            )
        next_run = current.next_run_at
        if schedule is not _UNSET:
            next_run = self._initial_run(new_schedule, stamp_time)
        with self._transaction() as connection:
            result = connection.execute(
                """
                UPDATE jobs SET name = ?, schedule_kind = ?, schedule_json = ?,
                    payload_json = ?, timezone = ?, catchup_policy = ?, max_catchup = ?,
                    grace_seconds = ?, next_run_at = ?, updated_at = ?, version = version + 1
                WHERE id = ?
                """,
                (
                    new_name,
                    ScheduleKind(new_schedule.kind).value,
                    _schedule_json(new_schedule),
                    _payload_json(new_payload),
                    new_schedule.timezone,
                    policy.value,
                    new_max,
                    new_grace,
                    None if next_run is None else _timestamp(next_run),
                    _timestamp(stamp_time),
                    job_id,
                ),
            )
            if result.rowcount != 1:
                raise SchedulerNotFoundError(f"job {job_id!r} was not found")
        return self.get_job(job_id)

    def pause_job(self, job_id: str, *, now: datetime | None = None) -> Job:
        return self._set_job_state(
            job_id,
            JobState.PAUSED,
            from_state=JobState.SCHEDULED,
            now=now,
        )

    def resume_job(self, job_id: str, *, now: datetime | None = None) -> Job:
        stamp = ensure_aware(now or utc_now(), "now")
        with self._transaction() as connection:
            row = connection.execute(
                "SELECT * FROM jobs WHERE id = ?",
                (job_id,),
            ).fetchone()
            if row is None:
                raise SchedulerNotFoundError(f"job {job_id!r} was not found")
            current = self._job_from_row(row)
            if current.state is not JobState.PAUSED:
                raise SchedulerConflictError("only paused jobs can be resumed")
            next_run = current.next_run_at
            if next_run is None:
                next_run = self._initial_run(current.schedule, stamp)
            result = connection.execute(
                """
                UPDATE jobs SET state = ?, next_run_at = ?, updated_at = ?, version = version + 1
                WHERE id = ? AND state = ?
                """,
                (
                    JobState.SCHEDULED.value,
                    None if next_run is None else _timestamp(next_run),
                    _timestamp(stamp),
                    job_id,
                    JobState.PAUSED.value,
                ),
            )
            if result.rowcount != 1:
                raise SchedulerConflictError("job state changed while it was being resumed")
        return self.get_job(job_id)

    def cancel_job(self, job_id: str, *, now: datetime | None = None) -> Job:
        stamp = _timestamp(ensure_aware(now or utc_now(), "now"))
        with self._transaction() as connection:
            row = connection.execute(
                "SELECT state FROM jobs WHERE id = ?",
                (job_id,),
            ).fetchone()
            if row is None:
                raise SchedulerNotFoundError(f"job {job_id!r} was not found")
            current_state = JobState(row["state"])
            if current_state is JobState.CANCELLED:
                return self.get_job(job_id)
            if current_state is not JobState.COMPLETED:
                result = connection.execute(
                    """
                    UPDATE jobs SET state = ?, updated_at = ?, version = version + 1
                    WHERE id = ? AND state IN (?, ?)
                    """,
                    (
                        JobState.CANCELLED.value,
                        stamp,
                        job_id,
                        JobState.SCHEDULED.value,
                        JobState.PAUSED.value,
                    ),
                )
                if result.rowcount != 1:
                    raise SchedulerConflictError(
                        "job state changed while cancellation was requested"
                    )
            connection.execute(
                """
                UPDATE job_runs SET cancel_requested = 1, updated_at = ?
                WHERE job_id = ? AND status IN (?, ?)
                """,
                (
                    stamp,
                    job_id,
                    JobRunStatus.CLAIMED.value,
                    JobRunStatus.RUNNING.value,
                ),
            )
            connection.execute(
                """
                UPDATE job_runs SET cancel_requested = 1, delivery_status = ?,
                    delivery_error = NULL, updated_at = ?
                WHERE job_id = ? AND status = ? AND delivery_status = ?
                """,
                (
                    DeliveryStatus.SUPPRESSED.value,
                    stamp,
                    job_id,
                    JobRunStatus.SUCCEEDED.value,
                    DeliveryStatus.PENDING.value,
                ),
            )
        return self.get_job(job_id)

    def delete_job(self, job_id: str, *, now: datetime | None = None) -> Job:
        """Durably soft-delete a job so its history remains queryable."""
        return self.cancel_job(job_id, now=now)

    def _set_job_state(
        self,
        job_id: str,
        state: JobState,
        *,
        from_state: JobState,
        now: datetime | None,
    ) -> Job:
        stamp = _timestamp(ensure_aware(now or utc_now(), "now"))
        with self._transaction() as connection:
            result = connection.execute(
                """
                UPDATE jobs SET state = ?, updated_at = ?, version = version + 1
                WHERE id = ? AND state = ?
                """,
                (state.value, stamp, job_id, from_state.value),
            )
            if result.rowcount != 1:
                row = connection.execute(
                    "SELECT state FROM jobs WHERE id = ?",
                    (job_id,),
                ).fetchone()
                if row is None:
                    raise SchedulerNotFoundError(f"job {job_id!r} was not found")
                raise SchedulerConflictError(
                    f"only {from_state.value} jobs can transition to {state.value}"
                )
        return self.get_job(job_id)

    def compute_next_run(self, schedule: ScheduleSpec, after: datetime) -> datetime | None:
        return compute_next_run(schedule, after)

    def preview(
        self,
        schedule: ScheduleSpec,
        *,
        after: datetime,
        count: int = 5,
    ) -> tuple[datetime, ...]:
        return preview_next_times(schedule, after, count)

    @staticmethod
    def _due_occurrences(job: Job, now: datetime) -> tuple[datetime, ...]:
        first = job.next_run_at
        if first is None or first > now:
            return ()
        if job.catchup_policy is CatchupPolicy.SKIP:
            if job.schedule.kind is ScheduleKind.ONCE:
                latest = first
            elif job.schedule.kind is ScheduleKind.INTERVAL:
                assert job.schedule.interval_seconds is not None
                elapsed = max(0.0, (now - first).total_seconds())
                latest = first + timedelta(
                    seconds=int(elapsed // job.schedule.interval_seconds)
                    * job.schedule.interval_seconds
                )
            else:
                assert job.schedule.cron is not None
                latest = CronExpression(job.schedule.cron).previous_or_at(
                    now,
                    get_timezone(job.schedule.timezone),
                )
            if latest < first or (now - latest).total_seconds() > job.grace_seconds:
                return ()
            return (latest,)
        count = job.max_catchup if job.catchup_policy is CatchupPolicy.BOUNDED else 1
        schedule = job.schedule
        if schedule.kind is ScheduleKind.ONCE:
            return (first,)
        if schedule.kind is ScheduleKind.INTERVAL:
            assert schedule.interval_seconds is not None
            elapsed = max(0.0, (now - first).total_seconds())
            latest_index = int(elapsed // schedule.interval_seconds)
            start_index = max(0, latest_index - count + 1)
            if job.catchup_policy is CatchupPolicy.FIRE_ONCE:
                start_index = latest_index
            return tuple(
                first + timedelta(seconds=index * schedule.interval_seconds)
                for index in range(start_index, latest_index + 1)
            )
        assert schedule.cron is not None
        expression = CronExpression(schedule.cron)
        zone = get_timezone(schedule.timezone)
        cursor = expression.previous_or_at(now, zone)
        found: list[datetime] = []
        while cursor >= first and len(found) < count:
            found.append(cursor)
            cursor = expression.previous_or_at(cursor - timedelta(microseconds=1), zone)
        return tuple(reversed(found))

    @staticmethod
    def _advance_after_now(job: Job, now: datetime) -> datetime | None:
        if job.schedule.kind is ScheduleKind.ONCE:
            return None
        return compute_next_run(job.schedule, now)

    def claim_due_runs(
        self,
        now: datetime,
        owner: str,
        lease: timedelta | float,
        batch: int = 32,
        *,
        startup_cap: int | None = None,
        jitter_seed: int | None = None,
    ) -> tuple[JobRun, ...]:
        now = ensure_aware(now, "now")
        lease_delta = lease if isinstance(lease, timedelta) else timedelta(seconds=lease)
        if not owner:
            raise SchedulerValidationError("owner must not be empty")
        if lease_delta <= timedelta(0):
            raise SchedulerValidationError("lease must be positive")
        if batch < 1:
            raise SchedulerValidationError("batch must be positive")
        limit = min(batch, startup_cap) if startup_cap is not None else batch
        if limit < 1:
            raise SchedulerValidationError("startup_cap must be positive")
        claimed_ids: list[str] = []
        with self._transaction() as connection:
            rows = connection.execute(
                """
                SELECT * FROM jobs
                WHERE state = ? AND next_run_at IS NOT NULL AND next_run_at <= ?
                ORDER BY next_run_at, id
                """,
                (JobState.SCHEDULED.value, _timestamp(now)),
            ).fetchall()
            jobs = [self._job_from_row(row) for row in rows]
            if jitter_seed is not None:
                random.Random(jitter_seed).shuffle(jobs)
            for job in jobs:
                if len(claimed_ids) >= limit:
                    break
                occurrences = self._due_occurrences(job, now)
                available = limit - len(claimed_ids)
                if len(occurrences) > available:
                    occurrences = occurrences[-available:]
                for scheduled_for in occurrences:
                    run_id = str(uuid.uuid4())
                    delivery = (
                        DeliveryStatus.PENDING
                        if job.payload.delivery is not None
                        else DeliveryStatus.NOT_REQUESTED
                    )
                    result = connection.execute(
                        """
                        INSERT OR IGNORE INTO job_runs(
                            id, job_id, scheduled_for, payload_json, status, attempt, owner,
                            lease_expires_at, cancel_requested, delivery_status,
                            claimed_at, updated_at
                        ) VALUES (?, ?, ?, ?, ?, 1, ?, ?, 0, ?, ?, ?)
                        """,
                        (
                            run_id,
                            job.id,
                            _timestamp(scheduled_for),
                            _payload_json(job.payload),
                            JobRunStatus.CLAIMED.value,
                            owner,
                            _timestamp(now + lease_delta),
                            delivery.value,
                            _timestamp(now),
                            _timestamp(now),
                        ),
                    )
                    if result.rowcount == 1:
                        claimed_ids.append(run_id)
                next_run = self._advance_after_now(job, now)
                next_state = (
                    JobState.COMPLETED if next_run is None else JobState.SCHEDULED
                )
                connection.execute(
                    """
                    UPDATE jobs SET next_run_at = ?, state = ?, updated_at = ?,
                        version = version + 1 WHERE id = ?
                    """,
                    (
                        None if next_run is None else _timestamp(next_run),
                        next_state.value,
                        _timestamp(now),
                        job.id,
                    ),
                )
        return tuple(self.get_run(run_id) for run_id in claimed_ids)

    def get_run(self, run_id: str) -> JobRun:
        with self._lock:
            self._ensure_open()
            row = self._connection.execute(
                "SELECT * FROM job_runs WHERE id = ?",
                (run_id,),
            ).fetchone()
        if row is None:
            raise SchedulerNotFoundError(f"job run {run_id!r} was not found")
        return self._run_from_row(row)

    def list_runs(
        self,
        *,
        job_id: str | None = None,
        status: JobRunStatus | str | None = None,
    ) -> tuple[JobRun, ...]:
        clauses: list[str] = []
        parameters: list[str] = []
        if job_id is not None:
            clauses.append("job_id = ?")
            parameters.append(job_id)
        if status is not None:
            clauses.append("status = ?")
            parameters.append(JobRunStatus(status).value)
        query = "SELECT * FROM job_runs"
        if clauses:
            query += " WHERE " + " AND ".join(clauses)
        query += " ORDER BY scheduled_for, id"
        with self._lock:
            self._ensure_open()
            rows = self._connection.execute(query, parameters).fetchall()
        return tuple(self._run_from_row(row) for row in rows)

    @staticmethod
    def _require_active_lease(run: JobRun, owner: str, now: datetime) -> None:
        if run.owner != owner:
            raise SchedulerOwnershipError("job run is owned by another scheduler")
        if run.lease_expires_at is None or run.lease_expires_at <= now:
            raise SchedulerOwnershipError("job run lease has expired")

    def mark_running(
        self,
        run_id: str,
        owner: str,
        *,
        now: datetime | None = None,
        polaris_run_id: str | None = None,
    ) -> JobRun:
        stamp = ensure_aware(now or utc_now(), "now")
        with self._transaction() as connection:
            row = connection.execute(
                "SELECT * FROM job_runs WHERE id = ?",
                (run_id,),
            ).fetchone()
            if row is None:
                raise SchedulerNotFoundError(f"job run {run_id!r} was not found")
            if JobRunStatus(row["status"]) is not JobRunStatus.CLAIMED:
                raise SchedulerConflictError("only claimed runs can start")
            lease_expires_at = _from_timestamp(row["lease_expires_at"])
            if row["owner"] != owner:
                raise SchedulerOwnershipError("job run is owned by another scheduler")
            if lease_expires_at is None or lease_expires_at <= stamp:
                raise SchedulerOwnershipError("job run lease has expired")
            if bool(row["cancel_requested"]):
                connection.execute(
                    """
                    UPDATE job_runs SET status = ?, owner = NULL, lease_expires_at = NULL,
                        polaris_run_id = COALESCE(?, polaris_run_id),
                        delivery_status = CASE
                            WHEN delivery_status = ? THEN ?
                            ELSE delivery_status
                        END,
                        completed_at = ?, updated_at = ?
                    WHERE id = ? AND status = ? AND owner = ?
                    """,
                    (
                        JobRunStatus.CANCELLED.value,
                        polaris_run_id,
                        DeliveryStatus.PENDING.value,
                        DeliveryStatus.SUPPRESSED.value,
                        _timestamp(stamp),
                        _timestamp(stamp),
                        run_id,
                        JobRunStatus.CLAIMED.value,
                        owner,
                    ),
                )
                return self.get_run(run_id)
            result = connection.execute(
                """
                UPDATE job_runs SET status = ?, started_at = ?, updated_at = ?,
                    polaris_run_id = COALESCE(?, polaris_run_id)
                WHERE id = ? AND status = ? AND owner = ?
                """,
                (
                    JobRunStatus.RUNNING.value,
                    _timestamp(stamp),
                    _timestamp(stamp),
                    polaris_run_id,
                    run_id,
                    JobRunStatus.CLAIMED.value,
                    owner,
                ),
            )
            if result.rowcount != 1:
                raise SchedulerOwnershipError("job run is no longer actively owned")
        return self.get_run(run_id)

    def set_polaris_run_id(
        self,
        run_id: str,
        owner: str,
        polaris_run_id: str,
        *,
        now: datetime | None = None,
    ) -> JobRun:
        if not polaris_run_id:
            raise SchedulerValidationError("polaris_run_id must not be empty")
        stamp = _timestamp(ensure_aware(now or utc_now(), "now"))
        with self._transaction() as connection:
            result = connection.execute(
                """
                UPDATE job_runs SET polaris_run_id = ?, updated_at = ?
                WHERE id = ? AND owner = ? AND status IN (?, ?)
                """,
                (
                    polaris_run_id,
                    stamp,
                    run_id,
                    owner,
                    JobRunStatus.CLAIMED.value,
                    JobRunStatus.RUNNING.value,
                ),
            )
            if result.rowcount != 1:
                raise SchedulerOwnershipError(
                    "cannot attach run id without an active ownership"
                )
        return self.get_run(run_id)

    def heartbeat(
        self,
        run_id: str,
        owner: str,
        lease: timedelta | float,
        *,
        now: datetime | None = None,
    ) -> JobRun:
        stamp = ensure_aware(now or utc_now(), "now")
        lease_delta = lease if isinstance(lease, timedelta) else timedelta(seconds=lease)
        if lease_delta <= timedelta(0):
            raise SchedulerValidationError("lease must be positive")
        run = self.get_run(run_id)
        if run.status not in {JobRunStatus.CLAIMED, JobRunStatus.RUNNING}:
            raise SchedulerConflictError("only active runs can heartbeat")
        self._require_active_lease(run, owner, stamp)
        with self._transaction() as connection:
            connection.execute(
                """
                UPDATE job_runs SET lease_expires_at = ?, updated_at = ?
                WHERE id = ? AND owner = ?
                """,
                (_timestamp(stamp + lease_delta), _timestamp(stamp), run_id, owner),
            )
        return self.get_run(run_id)

    def request_cancel(self, run_id: str, *, now: datetime | None = None) -> JobRun:
        stamp = _timestamp(ensure_aware(now or utc_now(), "now"))
        run = self.get_run(run_id)
        if run.status in {
            JobRunStatus.FAILED,
            JobRunStatus.INTERRUPTED,
            JobRunStatus.CANCELLED,
        }:
            return run
        with self._transaction() as connection:
            connection.execute(
                """
                UPDATE job_runs SET cancel_requested = 1,
                    delivery_status = CASE
                        WHEN status = ? AND delivery_status = ? THEN ?
                        ELSE delivery_status
                    END,
                    delivery_error = CASE
                        WHEN status = ? AND delivery_status = ? THEN NULL
                        ELSE delivery_error
                    END,
                    updated_at = ? WHERE id = ?
                """,
                (
                    JobRunStatus.SUCCEEDED.value,
                    DeliveryStatus.PENDING.value,
                    DeliveryStatus.SUPPRESSED.value,
                    JobRunStatus.SUCCEEDED.value,
                    DeliveryStatus.PENDING.value,
                    stamp,
                    run_id,
                ),
            )
        return self.get_run(run_id)

    def cancel(
        self,
        run_id: str,
        *,
        owner: str | None = None,
        now: datetime | None = None,
    ) -> JobRun:
        stamp = ensure_aware(now or utc_now(), "now")
        with self._transaction() as connection:
            row = connection.execute(
                "SELECT status, owner FROM job_runs WHERE id = ?",
                (run_id,),
            ).fetchone()
            if row is None:
                raise SchedulerNotFoundError(f"job run {run_id!r} was not found")
            current = JobRunStatus(row["status"])
            if current in {
                JobRunStatus.SUCCEEDED,
                JobRunStatus.FAILED,
                JobRunStatus.INTERRUPTED,
                JobRunStatus.CANCELLED,
            }:
                return self.get_run(run_id)
            if owner is not None and row["owner"] != owner:
                raise SchedulerOwnershipError("job run is owned by another scheduler")
            result = connection.execute(
                """
                UPDATE job_runs SET status = ?, cancel_requested = 1, owner = NULL,
                    lease_expires_at = NULL,
                    delivery_status = CASE
                        WHEN delivery_status = ? THEN ?
                        ELSE delivery_status
                    END,
                    delivery_error = CASE
                        WHEN delivery_status = ? THEN NULL
                        ELSE delivery_error
                    END,
                    completed_at = ?, updated_at = ?
                WHERE id = ? AND status IN (?, ?)
                """,
                (
                    JobRunStatus.CANCELLED.value,
                    DeliveryStatus.PENDING.value,
                    DeliveryStatus.SUPPRESSED.value,
                    DeliveryStatus.PENDING.value,
                    _timestamp(stamp),
                    _timestamp(stamp),
                    run_id,
                    JobRunStatus.CLAIMED.value,
                    JobRunStatus.RUNNING.value,
                ),
            )
            if result.rowcount != 1:
                raise SchedulerConflictError("job run changed while cancellation was requested")
        return self.get_run(run_id)

    def complete(
        self,
        run_id: str,
        owner: str,
        status: JobRunStatus | str = JobRunStatus.SUCCEEDED,
        *,
        error: str | None = None,
        polaris_run_id: str | None = None,
        now: datetime | None = None,
    ) -> JobRun:
        stamp = ensure_aware(now or utc_now(), "now")
        terminal = JobRunStatus(status)
        if terminal not in {
            JobRunStatus.SUCCEEDED,
            JobRunStatus.FAILED,
            JobRunStatus.CANCELLED,
        }:
            raise SchedulerValidationError("completion status must be terminal")
        with self._transaction() as connection:
            row = connection.execute(
                "SELECT * FROM job_runs WHERE id = ?",
                (run_id,),
            ).fetchone()
            if row is None:
                raise SchedulerNotFoundError(f"job run {run_id!r} was not found")
            current_status = JobRunStatus(row["status"])
            if current_status not in {JobRunStatus.CLAIMED, JobRunStatus.RUNNING}:
                raise SchedulerConflictError("only active runs can complete")
            lease_expires_at = _from_timestamp(row["lease_expires_at"])
            if row["owner"] != owner:
                raise SchedulerOwnershipError("job run is owned by another scheduler")
            if lease_expires_at is None or lease_expires_at <= stamp:
                raise SchedulerOwnershipError("job run lease has expired")
            effective = (
                JobRunStatus.CANCELLED
                if bool(row["cancel_requested"])
                else terminal
            )
            connection.execute(
                """
                UPDATE job_runs SET status = ?, execution_error = ?,
                    polaris_run_id = COALESCE(?, polaris_run_id), owner = NULL,
                    lease_expires_at = NULL,
                    delivery_status = CASE
                        WHEN ? = ? AND delivery_status = ? THEN ?
                        ELSE delivery_status
                    END,
                    delivery_error = CASE
                        WHEN ? = ? AND delivery_status = ? THEN NULL
                        ELSE delivery_error
                    END,
                    completed_at = ?, updated_at = ?
                WHERE id = ? AND owner = ?
                """,
                (
                    effective.value,
                    error,
                    polaris_run_id,
                    effective.value,
                    JobRunStatus.CANCELLED.value,
                    DeliveryStatus.PENDING.value,
                    DeliveryStatus.SUPPRESSED.value,
                    effective.value,
                    JobRunStatus.CANCELLED.value,
                    DeliveryStatus.PENDING.value,
                    _timestamp(stamp),
                    _timestamp(stamp),
                    run_id,
                    owner,
                ),
            )
        return self.get_run(run_id)

    def record_delivery(
        self,
        run_id: str,
        status: DeliveryStatus | str,
        *,
        error: str | None = None,
        now: datetime | None = None,
    ) -> JobRun:
        delivery_status = DeliveryStatus(status)
        if delivery_status not in {
            DeliveryStatus.SUCCEEDED,
            DeliveryStatus.FAILED,
            DeliveryStatus.SUPPRESSED,
        }:
            raise SchedulerValidationError("delivery status must be terminal")
        with self._transaction() as connection:
            row = connection.execute(
                "SELECT status, delivery_status FROM job_runs WHERE id = ?",
                (run_id,),
            ).fetchone()
            if row is None:
                raise SchedulerNotFoundError(f"job run {run_id!r} was not found")
            if JobRunStatus(row["status"]) is not JobRunStatus.SUCCEEDED:
                raise SchedulerConflictError(
                    "delivery is only recorded after execution success"
                )
            current = DeliveryStatus(row["delivery_status"])
            if current is not DeliveryStatus.PENDING:
                return self.get_run(run_id)
            connection.execute(
                """
                UPDATE job_runs SET delivery_status = ?, delivery_error = ?, updated_at = ?
                WHERE id = ? AND status = ? AND delivery_status = ?
                """,
                (
                    delivery_status.value,
                    error,
                    _timestamp(ensure_aware(now or utc_now(), "now")),
                    run_id,
                    JobRunStatus.SUCCEEDED.value,
                    DeliveryStatus.PENDING.value,
                ),
            )
        return self.get_run(run_id)

    def list_pending_deliveries(self, *, limit: int | None = None) -> tuple[JobRun, ...]:
        if limit is not None and limit < 1:
            raise SchedulerValidationError("delivery limit must be positive")
        query = (
            "SELECT * FROM job_runs "
            "WHERE status = ? AND delivery_status = ? "
            "ORDER BY completed_at, id"
        )
        parameters: list[object] = [
            JobRunStatus.SUCCEEDED.value,
            DeliveryStatus.PENDING.value,
        ]
        if limit is not None:
            query += " LIMIT ?"
            parameters.append(limit)
        with self._lock:
            self._ensure_open()
            rows = self._connection.execute(query, parameters).fetchall()
        return tuple(self._run_from_row(row) for row in rows)

    def recover_stale_runs(self, now: datetime | None = None) -> tuple[JobRun, ...]:
        stamp = ensure_aware(now or utc_now(), "now")
        with self._transaction() as connection:
            rows = connection.execute(
                """
                SELECT id FROM job_runs
                WHERE status IN (?, ?) AND lease_expires_at <= ?
                ORDER BY lease_expires_at, id
                """,
                (
                    JobRunStatus.CLAIMED.value,
                    JobRunStatus.RUNNING.value,
                    _timestamp(stamp),
                ),
            ).fetchall()
            identifiers = [str(row["id"]) for row in rows]
            if identifiers:
                placeholders = ",".join("?" for _ in identifiers)
                connection.execute(
                    f"""
                    UPDATE job_runs SET status = ?, execution_error = ?,
                        owner = NULL, lease_expires_at = NULL, completed_at = ?, updated_at = ?
                    WHERE id IN ({placeholders})
                    """,
                    (
                        JobRunStatus.INTERRUPTED.value,
                        "lease expired; execution outcome may be ambiguous",
                        _timestamp(stamp),
                        _timestamp(stamp),
                        *identifiers,
                    ),
                )
        return tuple(self.get_run(identifier) for identifier in identifiers)

    def create_retry(
        self,
        run_id: str,
        owner: str,
        lease: timedelta | float,
        *,
        approved: bool,
        now: datetime | None = None,
    ) -> JobRun:
        if not approved:
            raise SchedulerValidationError("retry requires explicit approval")
        stamp = ensure_aware(now or utc_now(), "now")
        lease_delta = lease if isinstance(lease, timedelta) else timedelta(seconds=lease)
        if lease_delta <= timedelta(0) or not owner:
            raise SchedulerValidationError("retry owner and lease must be valid")
        with self._transaction() as connection:
            row = connection.execute(
                "SELECT status, payload_json FROM job_runs WHERE id = ?",
                (run_id,),
            ).fetchone()
            if row is None:
                raise SchedulerNotFoundError(f"job run {run_id!r} was not found")
            retryable = {
                JobRunStatus.FAILED,
                JobRunStatus.INTERRUPTED,
                JobRunStatus.CANCELLED,
            }
            if JobRunStatus(row["status"]) not in retryable:
                raise SchedulerConflictError("only unsuccessful terminal runs can be retried")
            run_payload = _payload_from_json(str(row["payload_json"]))
            delivery = (
                DeliveryStatus.PENDING
                if run_payload.delivery is not None
                else DeliveryStatus.NOT_REQUESTED
            )
            result = connection.execute(
                """
                UPDATE job_runs SET status = ?, attempt = attempt + 1, owner = ?,
                    lease_expires_at = ?, polaris_run_id = NULL, cancel_requested = 0,
                    execution_error = NULL, delivery_status = ?, delivery_error = NULL,
                    claimed_at = ?, started_at = NULL, completed_at = NULL, updated_at = ?
                WHERE id = ? AND status IN (?, ?, ?)
                """,
                (
                    JobRunStatus.CLAIMED.value,
                    owner,
                    _timestamp(stamp + lease_delta),
                    delivery.value,
                    _timestamp(stamp),
                    _timestamp(stamp),
                    run_id,
                    JobRunStatus.FAILED.value,
                    JobRunStatus.INTERRUPTED.value,
                    JobRunStatus.CANCELLED.value,
                ),
            )
            if result.rowcount != 1:
                raise SchedulerConflictError("job run changed while retry was requested")
        return self.get_run(run_id)

    @staticmethod
    def _job_from_row(row: sqlite3.Row) -> Job:
        return Job(
            id=str(row["id"]),
            name=str(row["name"]),
            schedule=_schedule_from_json(str(row["schedule_json"])),
            payload=_payload_from_json(str(row["payload_json"])),
            catchup_policy=CatchupPolicy(row["catchup_policy"]),
            max_catchup=int(row["max_catchup"]),
            state=JobState(row["state"]),
            next_run_at=_from_timestamp(row["next_run_at"]),
            grace_seconds=float(row["grace_seconds"]),
            created_at=datetime.fromisoformat(row["created_at"]).astimezone(UTC),
            updated_at=datetime.fromisoformat(row["updated_at"]).astimezone(UTC),
            version=int(row["version"]),
        )

    @staticmethod
    def _run_from_row(row: sqlite3.Row) -> JobRun:
        return JobRun(
            id=str(row["id"]),
            job_id=str(row["job_id"]),
            scheduled_for=datetime.fromisoformat(row["scheduled_for"]).astimezone(UTC),
            status=JobRunStatus(row["status"]),
            attempt=int(row["attempt"]),
            owner=row["owner"],
            lease_expires_at=_from_timestamp(row["lease_expires_at"]),
            polaris_run_id=row["polaris_run_id"],
            cancel_requested=bool(row["cancel_requested"]),
            execution_error=row["execution_error"],
            delivery_status=DeliveryStatus(row["delivery_status"]),
            delivery_error=row["delivery_error"],
            claimed_at=datetime.fromisoformat(row["claimed_at"]).astimezone(UTC),
            started_at=_from_timestamp(row["started_at"]),
            completed_at=_from_timestamp(row["completed_at"]),
            updated_at=datetime.fromisoformat(row["updated_at"]).astimezone(UTC),
            payload=_payload_from_json(str(row["payload_json"])),
        )

    # Concise CRUD aliases are useful for API adapters without duplicating behavior.
    create = create_job
    get = get_job
    list = list_jobs
    update = update_job
    pause = pause_job
    resume = resume_job
    delete = delete_job
