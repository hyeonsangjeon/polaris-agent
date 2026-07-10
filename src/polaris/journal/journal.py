"""SQLite WAL-backed durable execution journal."""

from __future__ import annotations

import sqlite3
import threading
import uuid
from collections.abc import Iterable, Iterator, Mapping
from contextlib import contextmanager
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

from .codec import canonical_json, decode_json, normalize_timestamp, sha256_hex, utc_now
from .errors import (
    BudgetExceededError,
    InvalidTransitionError,
    JournalClosedError,
    JournalConflictError,
    JournalNotFoundError,
    JournalValidationError,
    LeaseExpiredError,
    OwnershipError,
)
from .models import (
    ApprovalRecord,
    ArtifactRecord,
    Budget,
    EventRecord,
    ProviderCallRecord,
    ReceiptRecord,
    RunRecord,
    RunStatus,
    SafetyClass,
    StepRecord,
    StepStatus,
)

_SCHEMA_VERSION = 1

_RUN_TRANSITIONS: dict[RunStatus, frozenset[RunStatus]] = {
    RunStatus.CREATED: frozenset(
        {RunStatus.RUNNING, RunStatus.PAUSED, RunStatus.FAILED, RunStatus.CANCELLED}
    ),
    RunStatus.RUNNING: frozenset(
        {RunStatus.PAUSED, RunStatus.COMPLETED, RunStatus.FAILED, RunStatus.CANCELLED}
    ),
    RunStatus.PAUSED: frozenset(
        {RunStatus.RUNNING, RunStatus.COMPLETED, RunStatus.FAILED, RunStatus.CANCELLED}
    ),
    RunStatus.COMPLETED: frozenset(),
    RunStatus.FAILED: frozenset(),
    RunStatus.CANCELLED: frozenset(),
}


class Journal:
    """Durable, thread-safe execution journal using a single SQLite connection."""

    def __init__(self, path: str | Path, *, busy_timeout_ms: int = 5000) -> None:
        if busy_timeout_ms < 0:
            raise JournalValidationError("busy_timeout_ms must be non-negative")
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

    def __enter__(self) -> Journal:
        self._ensure_open()
        return self

    def __exit__(self, _exc_type: object, _exc: object, _traceback: object) -> None:
        self.close()

    @property
    def schema_version(self) -> int:
        with self._lock:
            self._ensure_open()
            row = self._connection.execute(
                "SELECT MAX(version) AS version FROM schema_version"
            ).fetchone()
            return int(row["version"] or 0)

    def close(self) -> None:
        with self._lock:
            if not self._closed:
                self._connection.close()
                self._closed = True

    def _ensure_open(self) -> None:
        if self._closed:
            raise JournalClosedError("journal is closed")

    @contextmanager
    def _transaction(self, *, immediate: bool = False) -> Iterator[sqlite3.Connection]:
        with self._lock:
            self._ensure_open()
            self._connection.execute("BEGIN IMMEDIATE" if immediate else "BEGIN")
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
            CREATE TABLE IF NOT EXISTS schema_version (
                version INTEGER PRIMARY KEY,
                applied_at TEXT NOT NULL
            )
            """
        )
        row = self._connection.execute(
            "SELECT MAX(version) AS version FROM schema_version"
        ).fetchone()
        version = int(row["version"] or 0)
        if version > _SCHEMA_VERSION:
            raise JournalValidationError(
                "journal schema version "
                f"{version} is newer than supported version {_SCHEMA_VERSION}"
            )
        if version == 0:
            self._connection.executescript(
                """
                BEGIN IMMEDIATE;

                CREATE TABLE IF NOT EXISTS runs (
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
                    updated_at TEXT NOT NULL,
                    CHECK (reserved_calls >= 0 AND reserved_tokens >= 0),
                    CHECK (reserved_micro_usd >= 0 AND reserved_wall_seconds >= 0)
                );

                CREATE TABLE IF NOT EXISTS steps (
                    id TEXT PRIMARY KEY,
                    run_id TEXT NOT NULL REFERENCES runs(id),
                    deterministic_key TEXT NOT NULL,
                    kind TEXT NOT NULL,
                    name TEXT NOT NULL,
                    input_json TEXT NOT NULL,
                    input_hash TEXT NOT NULL,
                    safety TEXT NOT NULL,
                    sequence INTEGER,
                    status TEXT NOT NULL,
                    output_json TEXT,
                    error_json TEXT,
                    uncertainty_reason TEXT,
                    lease_owner TEXT,
                    lease_expires_at TEXT,
                    attempt_count INTEGER NOT NULL DEFAULT 0,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    UNIQUE (run_id, deterministic_key)
                );
                CREATE INDEX IF NOT EXISTS steps_ready_idx
                    ON steps(status, run_id, sequence, created_at);
                CREATE INDEX IF NOT EXISTS steps_lease_idx
                    ON steps(status, lease_expires_at);

                CREATE TABLE IF NOT EXISTS events (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    run_id TEXT NOT NULL REFERENCES runs(id),
                    step_id TEXT REFERENCES steps(id),
                    type TEXT NOT NULL,
                    payload_json TEXT NOT NULL,
                    created_at TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS events_run_idx ON events(run_id, id);

                CREATE TRIGGER IF NOT EXISTS events_no_update
                    BEFORE UPDATE ON events BEGIN
                    SELECT RAISE(ABORT, 'events are append-only');
                END;
                CREATE TRIGGER IF NOT EXISTS events_no_delete
                    BEFORE DELETE ON events BEGIN
                    SELECT RAISE(ABORT, 'events are append-only');
                END;

                CREATE TABLE IF NOT EXISTS receipts (
                    id TEXT PRIMARY KEY,
                    run_id TEXT NOT NULL REFERENCES runs(id),
                    step_id TEXT REFERENCES steps(id),
                    idempotency_key TEXT NOT NULL UNIQUE,
                    payload_json TEXT NOT NULL,
                    created_at TEXT NOT NULL
                );

                CREATE TABLE IF NOT EXISTS approvals (
                    id TEXT PRIMARY KEY,
                    run_id TEXT NOT NULL REFERENCES runs(id),
                    step_id TEXT REFERENCES steps(id),
                    kind TEXT NOT NULL,
                    request_json TEXT NOT NULL,
                    status TEXT NOT NULL,
                    decision TEXT,
                    decided_by TEXT,
                    decision_reason TEXT,
                    created_at TEXT NOT NULL,
                    decided_at TEXT
                );
                CREATE INDEX IF NOT EXISTS approvals_pending_idx
                    ON approvals(status, run_id, created_at);

                CREATE TABLE IF NOT EXISTS artifacts (
                    id TEXT PRIMARY KEY,
                    run_id TEXT NOT NULL REFERENCES runs(id),
                    step_id TEXT REFERENCES steps(id),
                    name TEXT NOT NULL,
                    media_type TEXT,
                    uri TEXT NOT NULL,
                    sha256 TEXT,
                    size_bytes INTEGER,
                    metadata_json TEXT NOT NULL,
                    created_at TEXT NOT NULL
                );

                CREATE TABLE IF NOT EXISTS provider_calls (
                    id TEXT PRIMARY KEY,
                    run_id TEXT NOT NULL REFERENCES runs(id),
                    step_id TEXT REFERENCES steps(id),
                    provider TEXT NOT NULL,
                    model TEXT,
                    request_json TEXT NOT NULL,
                    response_json TEXT,
                    status TEXT NOT NULL,
                    input_tokens INTEGER NOT NULL DEFAULT 0,
                    output_tokens INTEGER NOT NULL DEFAULT 0,
                    micro_usd INTEGER NOT NULL DEFAULT 0,
                    started_at TEXT NOT NULL,
                    completed_at TEXT
                );

                CREATE TABLE IF NOT EXISTS budget_reservations (
                    id TEXT PRIMARY KEY,
                    run_id TEXT NOT NULL REFERENCES runs(id),
                    calls INTEGER NOT NULL,
                    tokens INTEGER NOT NULL,
                    micro_usd INTEGER NOT NULL,
                    wall_seconds REAL NOT NULL,
                    actual_calls INTEGER,
                    actual_tokens INTEGER,
                    actual_micro_usd INTEGER,
                    actual_wall_seconds REAL,
                    status TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    settled_at TEXT
                );
                CREATE INDEX IF NOT EXISTS budget_reservations_run_idx
                    ON budget_reservations(run_id, status);
                COMMIT;
                """
            )
            self._connection.execute(
                "INSERT OR IGNORE INTO schema_version(version, applied_at) VALUES (?, ?)",
                (_SCHEMA_VERSION, utc_now()),
            )

    @staticmethod
    def _new_id(prefix: str) -> str:
        return f"{prefix}_{uuid.uuid4().hex}"

    @staticmethod
    def _coerce_run_status(status: RunStatus | str) -> RunStatus:
        try:
            return RunStatus(status)
        except ValueError as exc:
            raise JournalValidationError(f"unknown run status: {status!r}") from exc

    @staticmethod
    def _coerce_step_status(status: StepStatus | str) -> StepStatus:
        try:
            return StepStatus(status)
        except ValueError as exc:
            raise JournalValidationError(f"unknown step status: {status!r}") from exc

    @staticmethod
    def _coerce_safety(safety: SafetyClass | str) -> SafetyClass:
        try:
            return SafetyClass(safety)
        except ValueError as exc:
            raise JournalValidationError(f"unknown safety class: {safety!r}") from exc

    @staticmethod
    def _validate_nonnegative(name: str, value: int | float) -> None:
        if isinstance(value, bool) or not isinstance(value, (int, float)) or value < 0:
            raise JournalValidationError(f"{name} must be non-negative")

    @staticmethod
    def _validate_nonnegative_integer(name: str, value: object) -> None:
        if isinstance(value, bool) or not isinstance(value, int) or value < 0:
            raise JournalValidationError(f"{name} must be a non-negative integer")

    @staticmethod
    def _budget_from_input(value: Budget | Mapping[str, Any] | None) -> Budget:
        if value is None:
            return Budget()
        if isinstance(value, Budget):
            budget = value
        else:
            aliases = {
                "max_calls": "call_limit",
                "calls": "call_limit",
                "max_tokens": "token_limit",
                "tokens": "token_limit",
                "max_micro_usd": "micro_usd_limit",
                "micro_usd": "micro_usd_limit",
                "max_wall_seconds": "wall_seconds_limit",
                "wall_seconds": "wall_seconds_limit",
                "wall_time_limit_seconds": "wall_seconds_limit",
            }
            data = {aliases.get(key, key): item for key, item in value.items()}
            allowed = {"call_limit", "token_limit", "micro_usd_limit", "wall_seconds_limit"}
            unknown = set(data) - allowed
            if unknown:
                raise JournalValidationError(
                    f"unknown budget limit(s): {', '.join(sorted(unknown))}"
                )
            budget = Budget(**data)
        for name in ("call_limit", "token_limit", "micro_usd_limit"):
            limit = getattr(budget, name)
            if limit is not None:
                Journal._validate_nonnegative_integer(name, limit)
        if budget.wall_seconds_limit is not None:
            Journal._validate_nonnegative(
                "wall_seconds_limit", budget.wall_seconds_limit
            )
        return budget

    @staticmethod
    def _budget_from_row(row: sqlite3.Row) -> Budget:
        return Budget(
            call_limit=row["call_limit"],
            token_limit=row["token_limit"],
            micro_usd_limit=row["micro_usd_limit"],
            wall_seconds_limit=row["wall_seconds_limit"],
            reserved_calls=row["reserved_calls"],
            reserved_tokens=row["reserved_tokens"],
            reserved_micro_usd=row["reserved_micro_usd"],
            reserved_wall_seconds=row["reserved_wall_seconds"],
            used_calls=row["used_calls"],
            used_tokens=row["used_tokens"],
            used_micro_usd=row["used_micro_usd"],
            used_wall_seconds=row["used_wall_seconds"],
        )

    @classmethod
    def _run_from_row(cls, row: sqlite3.Row) -> RunRecord:
        return RunRecord(
            id=row["id"],
            mode=row["mode"],
            request=decode_json(row["request_json"]),
            config=decode_json(row["config_json"]),
            status=RunStatus(row["status"]),
            budget=cls._budget_from_row(row),
            parent_run_id=row["parent_run_id"],
            created_at=row["created_at"],
            updated_at=row["updated_at"],
        )

    @staticmethod
    def _step_from_row(row: sqlite3.Row) -> StepRecord:
        return StepRecord(
            id=row["id"],
            run_id=row["run_id"],
            key=row["deterministic_key"],
            kind=row["kind"],
            name=row["name"],
            input=decode_json(row["input_json"]),
            input_hash=row["input_hash"],
            safety=SafetyClass(row["safety"]),
            sequence=row["sequence"],
            status=StepStatus(row["status"]),
            output=decode_json(row["output_json"]),
            error=decode_json(row["error_json"]),
            uncertainty_reason=row["uncertainty_reason"],
            lease_owner=row["lease_owner"],
            lease_expires_at=row["lease_expires_at"],
            attempt_count=row["attempt_count"],
            created_at=row["created_at"],
            updated_at=row["updated_at"],
        )

    @staticmethod
    def _event_from_row(row: sqlite3.Row) -> EventRecord:
        return EventRecord(
            id=row["id"],
            run_id=row["run_id"],
            step_id=row["step_id"],
            type=row["type"],
            payload=decode_json(row["payload_json"]),
            created_at=row["created_at"],
        )

    @staticmethod
    def _receipt_from_row(row: sqlite3.Row) -> ReceiptRecord:
        return ReceiptRecord(
            id=row["id"],
            run_id=row["run_id"],
            step_id=row["step_id"],
            idempotency_key=row["idempotency_key"],
            payload=decode_json(row["payload_json"]),
            created_at=row["created_at"],
        )

    @staticmethod
    def _approval_from_row(row: sqlite3.Row) -> ApprovalRecord:
        return ApprovalRecord(
            id=row["id"],
            run_id=row["run_id"],
            step_id=row["step_id"],
            kind=row["kind"],
            request=decode_json(row["request_json"]),
            status=row["status"],
            decision=row["decision"],
            decided_by=row["decided_by"],
            decision_reason=row["decision_reason"],
            created_at=row["created_at"],
            decided_at=row["decided_at"],
        )

    @staticmethod
    def _artifact_from_row(row: sqlite3.Row) -> ArtifactRecord:
        return ArtifactRecord(
            id=row["id"],
            run_id=row["run_id"],
            step_id=row["step_id"],
            name=row["name"],
            media_type=row["media_type"],
            uri=row["uri"],
            sha256=row["sha256"],
            size_bytes=row["size_bytes"],
            metadata=decode_json(row["metadata_json"]),
            created_at=row["created_at"],
        )

    @staticmethod
    def _provider_call_from_row(row: sqlite3.Row) -> ProviderCallRecord:
        return ProviderCallRecord(
            id=row["id"],
            run_id=row["run_id"],
            step_id=row["step_id"],
            provider=row["provider"],
            model=row["model"],
            request=decode_json(row["request_json"]),
            response=decode_json(row["response_json"]),
            status=row["status"],
            input_tokens=row["input_tokens"],
            output_tokens=row["output_tokens"],
            micro_usd=row["micro_usd"],
            started_at=row["started_at"],
            completed_at=row["completed_at"],
        )

    @staticmethod
    def _require_row(row: sqlite3.Row | None, record_type: str, record_id: str) -> sqlite3.Row:
        if row is None:
            raise JournalNotFoundError(f"{record_type} {record_id!r} does not exist")
        return row

    @staticmethod
    def _append_event(
        connection: sqlite3.Connection,
        run_id: str,
        event_type: str,
        payload: object,
        *,
        step_id: str | None = None,
        created_at: str | None = None,
    ) -> EventRecord:
        timestamp = created_at or utc_now()
        cursor = connection.execute(
            """
            INSERT INTO events(run_id, step_id, type, payload_json, created_at)
            VALUES (?, ?, ?, ?, ?)
            """,
            (run_id, step_id, event_type, canonical_json(payload), timestamp),
        )
        event_id = cursor.lastrowid
        if event_id is None:
            raise JournalConflictError("SQLite did not return an event identifier")
        return EventRecord(
            id=event_id,
            run_id=run_id,
            step_id=step_id,
            type=event_type,
            payload=decode_json(canonical_json(payload)),
            created_at=timestamp,
        )

    def create_run(
        self,
        mode: str,
        request: object,
        config: object,
        budget_limits: Budget | Mapping[str, Any] | None = None,
        parent_run_id: str | None = None,
        *,
        budget: Budget | Mapping[str, Any] | None = None,
    ) -> RunRecord:
        """Create a durable run in the created state."""
        if not mode:
            raise JournalValidationError("mode must not be empty")
        if budget_limits is not None and budget is not None:
            raise JournalValidationError("pass either budget_limits or budget, not both")
        limits = self._budget_from_input(budget if budget is not None else budget_limits)
        run_id = self._new_id("run")
        timestamp = utc_now()
        with self._transaction(immediate=True) as connection:
            if parent_run_id is not None:
                self._require_row(
                    connection.execute(
                        "SELECT id FROM runs WHERE id = ?", (parent_run_id,)
                    ).fetchone(),
                    "run",
                    parent_run_id,
                )
            connection.execute(
                """
                INSERT INTO runs(
                    id, mode, request_json, config_json, status, parent_run_id,
                    call_limit, token_limit, micro_usd_limit, wall_seconds_limit,
                    created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    run_id,
                    mode,
                    canonical_json(request),
                    canonical_json(config),
                    RunStatus.CREATED.value,
                    parent_run_id,
                    limits.call_limit,
                    limits.token_limit,
                    limits.micro_usd_limit,
                    limits.wall_seconds_limit,
                    timestamp,
                    timestamp,
                ),
            )
            self._append_event(
                connection,
                run_id,
                "run.created",
                {"mode": mode, "parent_run_id": parent_run_id},
                created_at=timestamp,
            )
            row = connection.execute("SELECT * FROM runs WHERE id = ?", (run_id,)).fetchone()
        return self._run_from_row(self._require_row(row, "run", run_id))

    def get_run(self, run_id: str) -> RunRecord:
        with self._lock:
            self._ensure_open()
            row = self._connection.execute("SELECT * FROM runs WHERE id = ?", (run_id,)).fetchone()
            return self._run_from_row(self._require_row(row, "run", run_id))

    def list_runs(
        self, status: RunStatus | str | Iterable[RunStatus | str] | None = None
    ) -> list[RunRecord]:
        query = "SELECT * FROM runs"
        parameters: list[object] = []
        if status is not None:
            statuses = (
                [status]
                if isinstance(status, (str, RunStatus))
                else list(status)
            )
            values = [self._coerce_run_status(item).value for item in statuses]
            if not values:
                return []
            query += f" WHERE status IN ({','.join('?' for _ in values)})"
            parameters.extend(values)
        query += " ORDER BY created_at, id"
        with self._lock:
            self._ensure_open()
            rows = self._connection.execute(query, parameters).fetchall()
            return [self._run_from_row(row) for row in rows]

    def recoverable_runs(self) -> list[RunRecord]:
        return self.list_runs((RunStatus.CREATED, RunStatus.RUNNING, RunStatus.PAUSED))

    list_recoverable_runs = recoverable_runs

    def mark_run_status(self, run_id: str, status: RunStatus | str) -> RunRecord:
        target = self._coerce_run_status(status)
        with self._transaction(immediate=True) as connection:
            row = self._require_row(
                connection.execute("SELECT * FROM runs WHERE id = ?", (run_id,)).fetchone(),
                "run",
                run_id,
            )
            current = RunStatus(row["status"])
            if current == target:
                return self._run_from_row(row)
            if target not in _RUN_TRANSITIONS[current]:
                raise InvalidTransitionError(
                    f"run {run_id!r} cannot transition from {current.value} to {target.value}"
                )
            timestamp = utc_now()
            connection.execute(
                "UPDATE runs SET status = ?, updated_at = ? WHERE id = ? AND status = ?",
                (target.value, timestamp, run_id, current.value),
            )
            self._append_event(
                connection,
                run_id,
                "run.status_changed",
                {"from": current.value, "to": target.value},
                created_at=timestamp,
            )
            updated = connection.execute("SELECT * FROM runs WHERE id = ?", (run_id,)).fetchone()
        return self._run_from_row(self._require_row(updated, "run", run_id))

    def append_event(
        self,
        run_id: str,
        event_type: str,
        payload: object,
        *,
        step_id: str | None = None,
    ) -> EventRecord:
        if not event_type:
            raise JournalValidationError("event_type must not be empty")
        with self._transaction(immediate=True) as connection:
            self._require_row(
                connection.execute("SELECT id FROM runs WHERE id = ?", (run_id,)).fetchone(),
                "run",
                run_id,
            )
            if step_id is not None:
                step = self._require_row(
                    connection.execute(
                        "SELECT run_id FROM steps WHERE id = ?", (step_id,)
                    ).fetchone(),
                    "step",
                    step_id,
                )
                if step["run_id"] != run_id:
                    raise JournalValidationError("step does not belong to run")
            return self._append_event(
                connection, run_id, event_type, payload, step_id=step_id
            )

    def create_step(
        self,
        run_id: str,
        kind: str,
        name: str,
        input: object,
        safety: SafetyClass | str,
        sequence: int | None = None,
    ) -> StepRecord:
        """Idempotently create a ready step using a deterministic content key."""
        if not kind or not name:
            raise JournalValidationError("step kind and name must not be empty")
        if sequence is not None and sequence < 0:
            raise JournalValidationError("sequence must be non-negative")
        safety_value = self._coerce_safety(safety)
        input_json = canonical_json(input)
        input_hash = sha256_hex(input)
        key = sha256_hex(
            {
                "kind": kind,
                "name": name,
                "input_hash": input_hash,
                "safety": safety_value.value,
                "sequence": sequence,
            }
        )
        step_id = f"step_{sha256_hex({'run_id': run_id, 'key': key})}"
        timestamp = utc_now()
        with self._transaction(immediate=True) as connection:
            self._require_row(
                connection.execute("SELECT id FROM runs WHERE id = ?", (run_id,)).fetchone(),
                "run",
                run_id,
            )
            cursor = connection.execute(
                """
                INSERT OR IGNORE INTO steps(
                    id, run_id, deterministic_key, kind, name, input_json, input_hash,
                    safety, sequence, status, created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    step_id,
                    run_id,
                    key,
                    kind,
                    name,
                    input_json,
                    input_hash,
                    safety_value.value,
                    sequence,
                    StepStatus.READY.value,
                    timestamp,
                    timestamp,
                ),
            )
            if cursor.rowcount == 1:
                self._append_event(
                    connection,
                    run_id,
                    "step.created",
                    {
                        "kind": kind,
                        "name": name,
                        "input_hash": input_hash,
                        "safety": safety_value.value,
                        "sequence": sequence,
                        "status": StepStatus.READY.value,
                    },
                    step_id=step_id,
                    created_at=timestamp,
                )
            row = connection.execute("SELECT * FROM steps WHERE id = ?", (step_id,)).fetchone()
        return self._step_from_row(self._require_row(row, "step", step_id))

    def get_step(self, step_id: str) -> StepRecord:
        with self._lock:
            self._ensure_open()
            row = self._connection.execute(
                "SELECT * FROM steps WHERE id = ?", (step_id,)
            ).fetchone()
            return self._step_from_row(self._require_row(row, "step", step_id))

    def list_steps(
        self,
        run_id: str,
        status: StepStatus | str | Iterable[StepStatus | str] | None = None,
    ) -> list[StepRecord]:
        query = "SELECT * FROM steps WHERE run_id = ?"
        parameters: list[object] = [run_id]
        if status is not None:
            statuses = (
                [status]
                if isinstance(status, (str, StepStatus))
                else list(status)
            )
            values = [self._coerce_step_status(item).value for item in statuses]
            if not values:
                return []
            query += f" AND status IN ({','.join('?' for _ in values)})"
            parameters.extend(values)
        query += " ORDER BY CASE WHEN sequence IS NULL THEN 1 ELSE 0 END, sequence, created_at, id"
        with self._lock:
            self._ensure_open()
            self._require_row(
                self._connection.execute("SELECT id FROM runs WHERE id = ?", (run_id,)).fetchone(),
                "run",
                run_id,
            )
            return [
                self._step_from_row(row)
                for row in self._connection.execute(query, parameters).fetchall()
            ]

    def claim_ready_step(
        self,
        worker_id: str,
        lease_seconds: float,
        run_id: str | None = None,
    ) -> StepRecord | None:
        """Atomically claim one ready step, returning ``None`` when none is available."""
        if not worker_id:
            raise JournalValidationError("worker_id must not be empty")
        if lease_seconds <= 0:
            raise JournalValidationError("lease_seconds must be positive")
        timestamp = utc_now()
        expiry = normalize_timestamp(
            datetime.now(UTC) + timedelta(seconds=lease_seconds)
        )
        with self._transaction(immediate=True) as connection:
            if run_id is not None:
                self._require_row(
                    connection.execute("SELECT id FROM runs WHERE id = ?", (run_id,)).fetchone(),
                    "run",
                    run_id,
                )
            query = """
                SELECT * FROM steps
                WHERE status = ?
            """
            parameters: list[object] = [StepStatus.READY.value]
            if run_id is not None:
                query += " AND run_id = ?"
                parameters.append(run_id)
            query += """
                ORDER BY CASE WHEN sequence IS NULL THEN 1 ELSE 0 END,
                         sequence, created_at, id
                LIMIT 1
            """
            row = connection.execute(query, parameters).fetchone()
            if row is None:
                return None
            cursor = connection.execute(
                """
                UPDATE steps
                SET status = ?, lease_owner = ?, lease_expires_at = ?,
                    attempt_count = attempt_count + 1, updated_at = ?
                WHERE id = ? AND status = ?
                """,
                (
                    StepStatus.LEASED.value,
                    worker_id,
                    expiry,
                    timestamp,
                    row["id"],
                    StepStatus.READY.value,
                ),
            )
            if cursor.rowcount != 1:
                return None
            self._append_event(
                connection,
                row["run_id"],
                "step.leased",
                {
                    "worker_id": worker_id,
                    "lease_expires_at": expiry,
                    "attempt": row["attempt_count"] + 1,
                },
                step_id=row["id"],
                created_at=timestamp,
            )
            claimed = connection.execute(
                "SELECT * FROM steps WHERE id = ?", (row["id"],)
            ).fetchone()
        return self._step_from_row(self._require_row(claimed, "step", row["id"]))

    @staticmethod
    def _check_owner_and_lease(
        row: sqlite3.Row,
        worker_id: str,
        *,
        now: str | None = None,
    ) -> None:
        if not worker_id:
            raise JournalValidationError("worker_id must not be empty")
        if row["lease_owner"] != worker_id:
            raise OwnershipError(
                f"step {row['id']!r} is owned by {row['lease_owner']!r}, not {worker_id!r}"
            )
        current_time = normalize_timestamp(now)
        if row["lease_expires_at"] is None or row["lease_expires_at"] <= current_time:
            raise LeaseExpiredError(f"lease for step {row['id']!r} has expired")

    def mark_executing(self, step_id: str, worker_id: str) -> StepRecord:
        with self._transaction(immediate=True) as connection:
            row = self._require_row(
                connection.execute("SELECT * FROM steps WHERE id = ?", (step_id,)).fetchone(),
                "step",
                step_id,
            )
            current = StepStatus(row["status"])
            if current != StepStatus.LEASED:
                raise InvalidTransitionError(
                    f"step {step_id!r} cannot transition from {current.value} to executing"
                )
            self._check_owner_and_lease(row, worker_id)
            timestamp = utc_now()
            connection.execute(
                "UPDATE steps SET status = ?, updated_at = ? WHERE id = ? AND status = ?",
                (
                    StepStatus.EXECUTING.value,
                    timestamp,
                    step_id,
                    StepStatus.LEASED.value,
                ),
            )
            self._append_event(
                connection,
                row["run_id"],
                "step.executing",
                {"worker_id": worker_id},
                step_id=step_id,
                created_at=timestamp,
            )
            updated = connection.execute("SELECT * FROM steps WHERE id = ?", (step_id,)).fetchone()
        return self._step_from_row(self._require_row(updated, "step", step_id))

    def heartbeat(
        self,
        step_id: str,
        worker_id: str,
        lease_seconds: float,
    ) -> StepRecord:
        if lease_seconds <= 0:
            raise JournalValidationError("lease_seconds must be positive")
        with self._transaction(immediate=True) as connection:
            row = self._require_row(
                connection.execute("SELECT * FROM steps WHERE id = ?", (step_id,)).fetchone(),
                "step",
                step_id,
            )
            current = StepStatus(row["status"])
            if current not in (StepStatus.LEASED, StepStatus.EXECUTING):
                raise InvalidTransitionError(
                    f"cannot heartbeat step {step_id!r} in {current.value} state"
                )
            self._check_owner_and_lease(row, worker_id)
            timestamp = utc_now()
            expiry = normalize_timestamp(
                datetime.now(UTC) + timedelta(seconds=lease_seconds)
            )
            connection.execute(
                """
                UPDATE steps SET lease_expires_at = ?, updated_at = ?
                WHERE id = ? AND status = ? AND lease_owner = ?
                """,
                (expiry, timestamp, step_id, current.value, worker_id),
            )
            self._append_event(
                connection,
                row["run_id"],
                "step.heartbeat",
                {"worker_id": worker_id, "lease_expires_at": expiry},
                step_id=step_id,
                created_at=timestamp,
            )
            updated = connection.execute("SELECT * FROM steps WHERE id = ?", (step_id,)).fetchone()
        return self._step_from_row(self._require_row(updated, "step", step_id))

    @staticmethod
    def _record_receipt_in_transaction(
        connection: sqlite3.Connection,
        *,
        run_id: str,
        step_id: str | None,
        idempotency_key: str,
        payload: object,
        timestamp: str,
    ) -> ReceiptRecord:
        if not idempotency_key:
            raise JournalValidationError("idempotency_key must not be empty")
        payload_json = canonical_json(payload)
        existing = connection.execute(
            "SELECT * FROM receipts WHERE idempotency_key = ?", (idempotency_key,)
        ).fetchone()
        if existing is not None:
            if (
                existing["run_id"] != run_id
                or existing["step_id"] != step_id
                or existing["payload_json"] != payload_json
            ):
                raise JournalConflictError(
                    f"receipt {idempotency_key!r} already exists with different data"
                )
            return Journal._receipt_from_row(existing)
        receipt_id = f"receipt_{sha256_hex(idempotency_key)}"
        connection.execute(
            """
            INSERT INTO receipts(
                id, run_id, step_id, idempotency_key, payload_json, created_at
            ) VALUES (?, ?, ?, ?, ?, ?)
            """,
            (receipt_id, run_id, step_id, idempotency_key, payload_json, timestamp),
        )
        row = connection.execute("SELECT * FROM receipts WHERE id = ?", (receipt_id,)).fetchone()
        return Journal._receipt_from_row(
            Journal._require_row(row, "receipt", idempotency_key)
        )

    def commit_step(
        self,
        step_id: str,
        worker_id: str,
        output: object,
        receipt: ReceiptRecord | Mapping[str, Any] | None = None,
    ) -> StepRecord:
        """Atomically persist an output, optional receipt, and committed state."""
        with self._transaction(immediate=True) as connection:
            row = self._require_row(
                connection.execute("SELECT * FROM steps WHERE id = ?", (step_id,)).fetchone(),
                "step",
                step_id,
            )
            current = StepStatus(row["status"])
            if current != StepStatus.EXECUTING:
                raise InvalidTransitionError(
                    f"step {step_id!r} cannot transition from {current.value} to committed"
                )
            self._check_owner_and_lease(row, worker_id)
            timestamp = utc_now()
            receipt_id: str | None = None
            if receipt is not None:
                if isinstance(receipt, ReceiptRecord):
                    idempotency_key = receipt.idempotency_key
                    receipt_payload = receipt.payload
                else:
                    try:
                        idempotency_key = str(receipt["idempotency_key"])
                    except KeyError as exc:
                        raise JournalValidationError(
                            "receipt requires an idempotency_key"
                        ) from exc
                    receipt_payload = receipt.get("payload", receipt.get("receipt"))
                receipt_record = self._record_receipt_in_transaction(
                    connection,
                    run_id=row["run_id"],
                    step_id=step_id,
                    idempotency_key=idempotency_key,
                    payload=receipt_payload,
                    timestamp=timestamp,
                )
                receipt_id = receipt_record.id
            connection.execute(
                """
                UPDATE steps
                SET status = ?, output_json = ?, error_json = NULL,
                    lease_owner = NULL, lease_expires_at = NULL, updated_at = ?
                WHERE id = ? AND status = ? AND lease_owner = ?
                """,
                (
                    StepStatus.COMMITTED.value,
                    canonical_json(output),
                    timestamp,
                    step_id,
                    StepStatus.EXECUTING.value,
                    worker_id,
                ),
            )
            self._append_event(
                connection,
                row["run_id"],
                "step.committed",
                {
                    "worker_id": worker_id,
                    "output_hash": sha256_hex(output),
                    "receipt_id": receipt_id,
                },
                step_id=step_id,
                created_at=timestamp,
            )
            updated = connection.execute("SELECT * FROM steps WHERE id = ?", (step_id,)).fetchone()
        return self._step_from_row(self._require_row(updated, "step", step_id))

    def _finish_owned_step(
        self,
        step_id: str,
        worker_id: str,
        target: StepStatus,
        event_type: str,
        *,
        error: object | None = None,
        reason: str | None = None,
    ) -> StepRecord:
        with self._transaction(immediate=True) as connection:
            row = self._require_row(
                connection.execute("SELECT * FROM steps WHERE id = ?", (step_id,)).fetchone(),
                "step",
                step_id,
            )
            current = StepStatus(row["status"])
            if current not in (StepStatus.LEASED, StepStatus.EXECUTING):
                raise InvalidTransitionError(
                    f"step {step_id!r} cannot transition from {current.value} to {target.value}"
                )
            self._check_owner_and_lease(row, worker_id)
            timestamp = utc_now()
            connection.execute(
                """
                UPDATE steps
                SET status = ?, error_json = ?, uncertainty_reason = ?,
                    lease_owner = NULL, lease_expires_at = NULL, updated_at = ?
                WHERE id = ? AND status = ? AND lease_owner = ?
                """,
                (
                    target.value,
                    canonical_json(error) if error is not None else None,
                    reason,
                    timestamp,
                    step_id,
                    current.value,
                    worker_id,
                ),
            )
            payload: dict[str, object] = {"worker_id": worker_id}
            if error is not None:
                payload["error"] = error
            if reason is not None:
                payload["reason"] = reason
            self._append_event(
                connection,
                row["run_id"],
                event_type,
                payload,
                step_id=step_id,
                created_at=timestamp,
            )
            updated = connection.execute("SELECT * FROM steps WHERE id = ?", (step_id,)).fetchone()
        return self._step_from_row(self._require_row(updated, "step", step_id))

    def fail_step(self, step_id: str, worker_id: str, error: object) -> StepRecord:
        return self._finish_owned_step(
            step_id, worker_id, StepStatus.FAILED, "step.failed", error=error
        )

    def mark_uncertain(self, step_id: str, worker_id: str, reason: str) -> StepRecord:
        if not reason:
            raise JournalValidationError("uncertainty reason must not be empty")
        return self._finish_owned_step(
            step_id,
            worker_id,
            StepStatus.UNCERTAIN,
            "step.uncertain",
            reason=reason,
        )

    def cancel_step(self, step_id: str, worker_id: str | None = None) -> StepRecord:
        with self._transaction(immediate=True) as connection:
            row = self._require_row(
                connection.execute("SELECT * FROM steps WHERE id = ?", (step_id,)).fetchone(),
                "step",
                step_id,
            )
            current = StepStatus(row["status"])
            if current not in (
                StepStatus.CREATED,
                StepStatus.READY,
                StepStatus.LEASED,
                StepStatus.EXECUTING,
                StepStatus.UNCERTAIN,
            ):
                raise InvalidTransitionError(
                    f"step {step_id!r} cannot transition from {current.value} to cancelled"
                )
            if current in (StepStatus.LEASED, StepStatus.EXECUTING):
                if worker_id is None:
                    raise OwnershipError(f"worker_id is required to cancel leased step {step_id!r}")
                self._check_owner_and_lease(row, worker_id)
            timestamp = utc_now()
            connection.execute(
                """
                UPDATE steps
                SET status = ?, lease_owner = NULL, lease_expires_at = NULL, updated_at = ?
                WHERE id = ? AND status = ?
                """,
                (StepStatus.CANCELLED.value, timestamp, step_id, current.value),
            )
            self._append_event(
                connection,
                row["run_id"],
                "step.cancelled",
                {"worker_id": worker_id, "from": current.value},
                step_id=step_id,
                created_at=timestamp,
            )
            updated = connection.execute("SELECT * FROM steps WHERE id = ?", (step_id,)).fetchone()
        return self._step_from_row(self._require_row(updated, "step", step_id))

    def reclaim_expired_leases(
        self, now: str | datetime | None = None
    ) -> list[StepRecord]:
        """Recover expired work according to its safety class."""
        timestamp = normalize_timestamp(now)
        reclaimed: list[StepRecord] = []
        with self._transaction(immediate=True) as connection:
            rows = connection.execute(
                """
                SELECT * FROM steps
                WHERE status IN (?, ?)
                  AND lease_expires_at IS NOT NULL
                  AND lease_expires_at <= ?
                ORDER BY lease_expires_at, id
                """,
                (StepStatus.LEASED.value, StepStatus.EXECUTING.value, timestamp),
            ).fetchall()
            for row in rows:
                safety = SafetyClass(row["safety"])
                target = (
                    StepStatus.READY
                    if safety in (SafetyClass.READ_ONLY, SafetyClass.IDEMPOTENT)
                    else StepStatus.UNCERTAIN
                )
                reason = (
                    None
                    if target == StepStatus.READY
                    else "lease expired; outcome requires reconciliation or approval"
                )
                cursor = connection.execute(
                    """
                    UPDATE steps
                    SET status = ?, uncertainty_reason = ?,
                        lease_owner = NULL, lease_expires_at = NULL, updated_at = ?
                    WHERE id = ? AND status = ? AND lease_expires_at <= ?
                    """,
                    (
                        target.value,
                        reason,
                        timestamp,
                        row["id"],
                        row["status"],
                        timestamp,
                    ),
                )
                if cursor.rowcount != 1:
                    continue
                self._append_event(
                    connection,
                    row["run_id"],
                    "step.lease_expired",
                    {
                        "from": row["status"],
                        "to": target.value,
                        "previous_owner": row["lease_owner"],
                        "safety": safety.value,
                        "reason": reason,
                    },
                    step_id=row["id"],
                    created_at=timestamp,
                )
                updated = connection.execute(
                    "SELECT * FROM steps WHERE id = ?", (row["id"],)
                ).fetchone()
                reclaimed.append(
                    self._step_from_row(self._require_row(updated, "step", row["id"]))
                )
        return reclaimed

    def record_receipt(
        self,
        step_id: str,
        idempotency_key: str,
        payload: object,
    ) -> ReceiptRecord:
        """Record a receipt exactly once by idempotency key."""
        with self._transaction(immediate=True) as connection:
            step = self._require_row(
                connection.execute("SELECT run_id FROM steps WHERE id = ?", (step_id,)).fetchone(),
                "step",
                step_id,
            )
            timestamp = utc_now()
            existed = (
                connection.execute(
                    "SELECT 1 FROM receipts WHERE idempotency_key = ?",
                    (idempotency_key,),
                ).fetchone()
                is not None
            )
            receipt = self._record_receipt_in_transaction(
                connection,
                run_id=step["run_id"],
                step_id=step_id,
                idempotency_key=idempotency_key,
                payload=payload,
                timestamp=timestamp,
            )
            if not existed:
                self._append_event(
                    connection,
                    step["run_id"],
                    "receipt.recorded",
                    {"receipt_id": receipt.id, "idempotency_key": idempotency_key},
                    step_id=step_id,
                    created_at=timestamp,
                )
            return receipt

    def find_receipt(self, idempotency_key: str) -> ReceiptRecord | None:
        with self._lock:
            self._ensure_open()
            row = self._connection.execute(
                "SELECT * FROM receipts WHERE idempotency_key = ?", (idempotency_key,)
            ).fetchone()
            return None if row is None else self._receipt_from_row(row)

    def request_approval(
        self,
        run_id: str,
        step_id: str | None = None,
        request: object | None = None,
        *,
        kind: str = "execution",
    ) -> ApprovalRecord:
        if not kind:
            raise JournalValidationError("approval kind must not be empty")
        approval_id = self._new_id("approval")
        timestamp = utc_now()
        with self._transaction(immediate=True) as connection:
            self._require_row(
                connection.execute("SELECT id FROM runs WHERE id = ?", (run_id,)).fetchone(),
                "run",
                run_id,
            )
            if step_id is not None:
                step = self._require_row(
                    connection.execute(
                        "SELECT run_id FROM steps WHERE id = ?", (step_id,)
                    ).fetchone(),
                    "step",
                    step_id,
                )
                if step["run_id"] != run_id:
                    raise JournalValidationError("step does not belong to run")
            connection.execute(
                """
                INSERT INTO approvals(
                    id, run_id, step_id, kind, request_json, status, created_at
                ) VALUES (?, ?, ?, ?, ?, 'pending', ?)
                """,
                (
                    approval_id,
                    run_id,
                    step_id,
                    kind,
                    canonical_json(request),
                    timestamp,
                ),
            )
            self._append_event(
                connection,
                run_id,
                "approval.requested",
                {"approval_id": approval_id, "kind": kind},
                step_id=step_id,
                created_at=timestamp,
            )
            row = connection.execute(
                "SELECT * FROM approvals WHERE id = ?", (approval_id,)
            ).fetchone()
        return self._approval_from_row(
            self._require_row(row, "approval", approval_id)
        )

    def get_approval(self, approval_id: str) -> ApprovalRecord:
        with self._lock:
            self._ensure_open()
            row = self._connection.execute(
                "SELECT * FROM approvals WHERE id = ?", (approval_id,)
            ).fetchone()
            return self._approval_from_row(
                self._require_row(row, "approval", approval_id)
            )

    def decide_approval(
        self,
        approval_id: str,
        decision: str | bool,
        decided_by: str,
        reason: str | None = None,
    ) -> ApprovalRecord:
        decision_value = (
            "approved" if decision is True else "rejected" if decision is False else decision
        )
        if decision_value not in ("approved", "rejected"):
            raise JournalValidationError("approval decision must be approved or rejected")
        if not decided_by:
            raise JournalValidationError("decided_by must not be empty")
        with self._transaction(immediate=True) as connection:
            row = self._require_row(
                connection.execute(
                    "SELECT * FROM approvals WHERE id = ?", (approval_id,)
                ).fetchone(),
                "approval",
                approval_id,
            )
            if row["status"] != "pending":
                if row["decision"] == decision_value and row["decided_by"] == decided_by:
                    return self._approval_from_row(row)
                raise InvalidTransitionError(
                    f"approval {approval_id!r} has already been {row['status']}"
                )
            timestamp = utc_now()
            connection.execute(
                """
                UPDATE approvals
                SET status = ?, decision = ?, decided_by = ?,
                    decision_reason = ?, decided_at = ?
                WHERE id = ? AND status = 'pending'
                """,
                (
                    decision_value,
                    decision_value,
                    decided_by,
                    reason,
                    timestamp,
                    approval_id,
                ),
            )
            self._append_event(
                connection,
                row["run_id"],
                "approval.decided",
                {
                    "approval_id": approval_id,
                    "decision": decision_value,
                    "decided_by": decided_by,
                    "reason": reason,
                },
                step_id=row["step_id"],
                created_at=timestamp,
            )
            updated = connection.execute(
                "SELECT * FROM approvals WHERE id = ?", (approval_id,)
            ).fetchone()
        return self._approval_from_row(
            self._require_row(updated, "approval", approval_id)
        )

    def list_pending_approvals(self, run_id: str | None = None) -> list[ApprovalRecord]:
        query = "SELECT * FROM approvals WHERE status = 'pending'"
        parameters: tuple[object, ...] = ()
        if run_id is not None:
            query += " AND run_id = ?"
            parameters = (run_id,)
        query += " ORDER BY created_at, id"
        with self._lock:
            self._ensure_open()
            rows = self._connection.execute(query, parameters).fetchall()
            return [self._approval_from_row(row) for row in rows]

    def list_approvals(
        self,
        *,
        run_id: str | None = None,
        step_id: str | None = None,
        status: str | None = None,
    ) -> list[ApprovalRecord]:
        query = "SELECT * FROM approvals WHERE 1 = 1"
        parameters: list[object] = []
        if run_id is not None:
            query += " AND run_id = ?"
            parameters.append(run_id)
        if step_id is not None:
            query += " AND step_id = ?"
            parameters.append(step_id)
        if status is not None:
            query += " AND status = ?"
            parameters.append(status)
        query += " ORDER BY created_at, id"
        with self._lock:
            self._ensure_open()
            rows = self._connection.execute(query, parameters).fetchall()
            return [self._approval_from_row(row) for row in rows]

    def record_artifact(
        self,
        run_id: str,
        name: str,
        uri: str,
        *,
        step_id: str | None = None,
        media_type: str | None = None,
        sha256: str | None = None,
        size_bytes: int | None = None,
        metadata: object | None = None,
    ) -> ArtifactRecord:
        if not name or not uri:
            raise JournalValidationError("artifact name and uri must not be empty")
        if size_bytes is not None:
            self._validate_nonnegative_integer("size_bytes", size_bytes)
        artifact_id = self._new_id("artifact")
        timestamp = utc_now()
        with self._transaction(immediate=True) as connection:
            self._require_row(
                connection.execute("SELECT id FROM runs WHERE id = ?", (run_id,)).fetchone(),
                "run",
                run_id,
            )
            if step_id is not None:
                step = self._require_row(
                    connection.execute(
                        "SELECT run_id FROM steps WHERE id = ?", (step_id,)
                    ).fetchone(),
                    "step",
                    step_id,
                )
                if step["run_id"] != run_id:
                    raise JournalValidationError("step does not belong to run")
            connection.execute(
                """
                INSERT INTO artifacts(
                    id, run_id, step_id, name, media_type, uri, sha256,
                    size_bytes, metadata_json, created_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    artifact_id,
                    run_id,
                    step_id,
                    name,
                    media_type,
                    uri,
                    sha256,
                    size_bytes,
                    canonical_json(metadata),
                    timestamp,
                ),
            )
            self._append_event(
                connection,
                run_id,
                "artifact.recorded",
                {"artifact_id": artifact_id, "name": name, "sha256": sha256},
                step_id=step_id,
                created_at=timestamp,
            )
            row = connection.execute(
                "SELECT * FROM artifacts WHERE id = ?", (artifact_id,)
            ).fetchone()
        return self._artifact_from_row(
            self._require_row(row, "artifact", artifact_id)
        )

    def list_artifacts(self, run_id: str) -> list[ArtifactRecord]:
        with self._lock:
            self._ensure_open()
            rows = self._connection.execute(
                "SELECT * FROM artifacts WHERE run_id = ? ORDER BY created_at, id",
                (run_id,),
            ).fetchall()
            return [self._artifact_from_row(row) for row in rows]

    def start_provider_call(
        self,
        run_id: str,
        provider: str,
        request: object,
        *,
        step_id: str | None = None,
        model: str | None = None,
    ) -> ProviderCallRecord:
        if not provider:
            raise JournalValidationError("provider must not be empty")
        call_id = self._new_id("provider")
        timestamp = utc_now()
        with self._transaction(immediate=True) as connection:
            self._require_row(
                connection.execute("SELECT id FROM runs WHERE id = ?", (run_id,)).fetchone(),
                "run",
                run_id,
            )
            connection.execute(
                """
                INSERT INTO provider_calls(
                    id, run_id, step_id, provider, model, request_json,
                    status, started_at
                ) VALUES (?, ?, ?, ?, ?, ?, 'started', ?)
                """,
                (
                    call_id,
                    run_id,
                    step_id,
                    provider,
                    model,
                    canonical_json(request),
                    timestamp,
                ),
            )
            self._append_event(
                connection,
                run_id,
                "provider.started",
                {"provider_call_id": call_id, "provider": provider, "model": model},
                step_id=step_id,
                created_at=timestamp,
            )
            row = connection.execute(
                "SELECT * FROM provider_calls WHERE id = ?", (call_id,)
            ).fetchone()
        return self._provider_call_from_row(
            self._require_row(row, "provider call", call_id)
        )

    def complete_provider_call(
        self,
        call_id: str,
        *,
        response: object,
        status: str = "completed",
        model: str | None = None,
        input_tokens: int = 0,
        output_tokens: int = 0,
        micro_usd: int = 0,
    ) -> ProviderCallRecord:
        if status not in {"completed", "failed", "uncertain"}:
            raise JournalValidationError("invalid provider call status")
        for name, value in (
            ("input_tokens", input_tokens),
            ("output_tokens", output_tokens),
            ("micro_usd", micro_usd),
        ):
            self._validate_nonnegative_integer(name, value)
        timestamp = utc_now()
        with self._transaction(immediate=True) as connection:
            row = self._require_row(
                connection.execute(
                    "SELECT * FROM provider_calls WHERE id = ?", (call_id,)
                ).fetchone(),
                "provider call",
                call_id,
            )
            if row["status"] != "started":
                raise InvalidTransitionError(
                    f"provider call {call_id!r} is already {row['status']}"
                )
            connection.execute(
                """
                UPDATE provider_calls
                SET response_json = ?, status = ?, model = COALESCE(?, model),
                    input_tokens = ?, output_tokens = ?, micro_usd = ?,
                    completed_at = ?
                WHERE id = ? AND status = 'started'
                """,
                (
                    canonical_json(response),
                    status,
                    model,
                    input_tokens,
                    output_tokens,
                    micro_usd,
                    timestamp,
                    call_id,
                ),
            )
            self._append_event(
                connection,
                row["run_id"],
                f"provider.{status}",
                {
                    "provider_call_id": call_id,
                    "model": model or row["model"],
                    "input_tokens": input_tokens,
                    "output_tokens": output_tokens,
                    "micro_usd": micro_usd,
                },
                step_id=row["step_id"],
                created_at=timestamp,
            )
            updated = connection.execute(
                "SELECT * FROM provider_calls WHERE id = ?", (call_id,)
            ).fetchone()
        return self._provider_call_from_row(
            self._require_row(updated, "provider call", call_id)
        )

    def list_provider_calls(self, run_id: str) -> list[ProviderCallRecord]:
        with self._lock:
            self._ensure_open()
            rows = self._connection.execute(
                "SELECT * FROM provider_calls WHERE run_id = ? ORDER BY started_at, id",
                (run_id,),
            ).fetchall()
            return [self._provider_call_from_row(row) for row in rows]

    def reserve_budget(
        self,
        run_id: str,
        *,
        calls: int = 0,
        tokens: int = 0,
        micro_usd: int = 0,
        wall_seconds: float = 0.0,
        reservation_id: str | None = None,
    ) -> str:
        """Atomically reserve budget capacity and return a durable reservation ID."""
        for name, value in (
            ("calls", calls),
            ("tokens", tokens),
            ("micro_usd", micro_usd),
        ):
            self._validate_nonnegative_integer(name, value)
        self._validate_nonnegative("wall_seconds", wall_seconds)
        reservation_id = reservation_id or self._new_id("budget")
        timestamp = utc_now()
        with self._transaction(immediate=True) as connection:
            existing = connection.execute(
                "SELECT * FROM budget_reservations WHERE id = ?", (reservation_id,)
            ).fetchone()
            if existing is not None:
                if (
                    existing["run_id"] == run_id
                    and existing["calls"] == calls
                    and existing["tokens"] == tokens
                    and existing["micro_usd"] == micro_usd
                    and existing["wall_seconds"] == wall_seconds
                ):
                    return reservation_id
                raise JournalConflictError(
                    f"budget reservation {reservation_id!r} already exists with different data"
                )
            row = self._require_row(
                connection.execute("SELECT * FROM runs WHERE id = ?", (run_id,)).fetchone(),
                "run",
                run_id,
            )
            requested = {
                "calls": calls,
                "tokens": tokens,
                "micro_usd": micro_usd,
                "wall_seconds": wall_seconds,
            }
            columns = {
                "calls": ("call_limit", "reserved_calls", "used_calls"),
                "tokens": ("token_limit", "reserved_tokens", "used_tokens"),
                "micro_usd": (
                    "micro_usd_limit",
                    "reserved_micro_usd",
                    "used_micro_usd",
                ),
                "wall_seconds": (
                    "wall_seconds_limit",
                    "reserved_wall_seconds",
                    "used_wall_seconds",
                ),
            }
            for resource, amount in requested.items():
                limit_column, reserved_column, used_column = columns[resource]
                limit = row[limit_column]
                projected = row[reserved_column] + row[used_column] + amount
                if limit is not None and projected > limit:
                    raise BudgetExceededError(
                        f"{resource} budget exceeded: {projected} requested against limit {limit}"
                    )
            connection.execute(
                """
                UPDATE runs
                SET reserved_calls = reserved_calls + ?,
                    reserved_tokens = reserved_tokens + ?,
                    reserved_micro_usd = reserved_micro_usd + ?,
                    reserved_wall_seconds = reserved_wall_seconds + ?,
                    updated_at = ?
                WHERE id = ?
                """,
                (calls, tokens, micro_usd, wall_seconds, timestamp, run_id),
            )
            connection.execute(
                """
                INSERT INTO budget_reservations(
                    id, run_id, calls, tokens, micro_usd, wall_seconds,
                    status, created_at
                ) VALUES (?, ?, ?, ?, ?, ?, 'reserved', ?)
                """,
                (
                    reservation_id,
                    run_id,
                    calls,
                    tokens,
                    micro_usd,
                    wall_seconds,
                    timestamp,
                ),
            )
            self._append_event(
                connection,
                run_id,
                "budget.reserved",
                {"reservation_id": reservation_id, **requested},
                created_at=timestamp,
            )
        return reservation_id

    def settle_budget(
        self,
        run_id: str,
        reservation_id: str,
        *,
        actual_calls: int | None = None,
        actual_tokens: int | None = None,
        actual_micro_usd: int | None = None,
        actual_wall_seconds: float | None = None,
        calls: int | None = None,
        tokens: int | None = None,
        micro_usd: int | None = None,
        wall_seconds: float | None = None,
    ) -> Budget:
        """Release a reservation and durably record actual consumption."""
        pairs = (
            ("calls", actual_calls, calls),
            ("tokens", actual_tokens, tokens),
            ("micro_usd", actual_micro_usd, micro_usd),
            ("wall_seconds", actual_wall_seconds, wall_seconds),
        )
        for name, explicit, alias in pairs:
            if explicit is not None and alias is not None:
                raise JournalValidationError(
                    f"pass either actual_{name} or {name}, not both"
                )
        actual_calls = actual_calls if actual_calls is not None else (calls or 0)
        actual_tokens = actual_tokens if actual_tokens is not None else (tokens or 0)
        actual_micro_usd = (
            actual_micro_usd if actual_micro_usd is not None else (micro_usd or 0)
        )
        actual_wall_seconds = (
            actual_wall_seconds
            if actual_wall_seconds is not None
            else (wall_seconds or 0.0)
        )
        for name, value in (
            ("actual_calls", actual_calls),
            ("actual_tokens", actual_tokens),
            ("actual_micro_usd", actual_micro_usd),
        ):
            self._validate_nonnegative_integer(name, value)
        self._validate_nonnegative("actual_wall_seconds", actual_wall_seconds)
        actual = {
            "calls": actual_calls,
            "tokens": actual_tokens,
            "micro_usd": actual_micro_usd,
            "wall_seconds": actual_wall_seconds,
        }
        with self._transaction(immediate=True) as connection:
            reservation = self._require_row(
                connection.execute(
                    "SELECT * FROM budget_reservations WHERE id = ?", (reservation_id,)
                ).fetchone(),
                "budget reservation",
                reservation_id,
            )
            if reservation["run_id"] != run_id:
                raise JournalValidationError("budget reservation does not belong to run")
            if reservation["status"] == "settled":
                stored = {
                    "calls": reservation["actual_calls"],
                    "tokens": reservation["actual_tokens"],
                    "micro_usd": reservation["actual_micro_usd"],
                    "wall_seconds": reservation["actual_wall_seconds"],
                }
                if stored != actual:
                    raise JournalConflictError(
                        f"budget reservation {reservation_id!r} was settled differently"
                    )
                run = self._require_row(
                    connection.execute("SELECT * FROM runs WHERE id = ?", (run_id,)).fetchone(),
                    "run",
                    run_id,
                )
                return self._budget_from_row(run)
            timestamp = utc_now()
            connection.execute(
                """
                UPDATE runs
                SET reserved_calls = reserved_calls - ?,
                    reserved_tokens = reserved_tokens - ?,
                    reserved_micro_usd = reserved_micro_usd - ?,
                    reserved_wall_seconds = reserved_wall_seconds - ?,
                    used_calls = used_calls + ?,
                    used_tokens = used_tokens + ?,
                    used_micro_usd = used_micro_usd + ?,
                    used_wall_seconds = used_wall_seconds + ?,
                    updated_at = ?
                WHERE id = ?
                """,
                (
                    reservation["calls"],
                    reservation["tokens"],
                    reservation["micro_usd"],
                    reservation["wall_seconds"],
                    actual_calls,
                    actual_tokens,
                    actual_micro_usd,
                    actual_wall_seconds,
                    timestamp,
                    run_id,
                ),
            )
            connection.execute(
                """
                UPDATE budget_reservations
                SET actual_calls = ?, actual_tokens = ?, actual_micro_usd = ?,
                    actual_wall_seconds = ?, status = 'settled', settled_at = ?
                WHERE id = ? AND status = 'reserved'
                """,
                (
                    actual_calls,
                    actual_tokens,
                    actual_micro_usd,
                    actual_wall_seconds,
                    timestamp,
                    reservation_id,
                ),
            )
            self._append_event(
                connection,
                run_id,
                "budget.settled",
                {"reservation_id": reservation_id, **actual},
                created_at=timestamp,
            )
            run = connection.execute("SELECT * FROM runs WHERE id = ?", (run_id,)).fetchone()
        return self._budget_from_row(self._require_row(run, "run", run_id))

    def list_events(
        self,
        run_id: str,
        *,
        step_id: str | None = None,
        after_id: int | None = None,
        limit: int | None = None,
    ) -> list[EventRecord]:
        if limit is not None and limit <= 0:
            raise JournalValidationError("limit must be positive")
        query = "SELECT * FROM events WHERE run_id = ?"
        parameters: list[object] = [run_id]
        if step_id is not None:
            query += " AND step_id = ?"
            parameters.append(step_id)
        if after_id is not None:
            query += " AND id > ?"
            parameters.append(after_id)
        query += " ORDER BY id"
        if limit is not None:
            query += " LIMIT ?"
            parameters.append(limit)
        with self._lock:
            self._ensure_open()
            self._require_row(
                self._connection.execute("SELECT id FROM runs WHERE id = ?", (run_id,)).fetchone(),
                "run",
                run_id,
            )
            return [
                self._event_from_row(row)
                for row in self._connection.execute(query, parameters).fetchall()
            ]

    def get_event(self, event_id: int) -> EventRecord:
        with self._lock:
            self._ensure_open()
            row = self._connection.execute(
                "SELECT * FROM events WHERE id = ?", (event_id,)
            ).fetchone()
            return self._event_from_row(
                self._require_row(row, "event", str(event_id))
            )

    def materialized_timeline(self, run_id: str) -> list[EventRecord]:
        """Return the run's durable event timeline in commit order."""
        return self.list_events(run_id)

    get_timeline = materialized_timeline
