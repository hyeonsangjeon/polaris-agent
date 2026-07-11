"""SQLite inbox, outbox, offset, and authorization journal for channels."""

from __future__ import annotations

import dataclasses
import hashlib
import json
import sqlite3
import threading
import time
from collections.abc import Callable, Iterable, Iterator, Mapping
from contextlib import contextmanager
from datetime import UTC, datetime, timedelta
from enum import Enum
from pathlib import Path
from typing import Any, cast

from .auth import AuthorizationPolicy
from .models import (
    AuthDecision,
    ChannelEnvelope,
    InboxRecord,
    InboxStatus,
    IngestResult,
    MessageOperation,
    OutboundMessage,
    OutboxRecord,
    OutboxStatus,
    ParseMode,
    Platform,
    RemoteReceipt,
)

_CHANNEL_SCHEMA_VERSION = 4
_MIGRATION_SCOPE = "channels"


class ChannelStoreError(RuntimeError):
    pass


class ChannelStoreClosedError(ChannelStoreError):
    pass


class ChannelTransitionError(ChannelStoreError):
    pass


def _json_default(value: object) -> object:
    if isinstance(value, Enum):
        return value.value
    if dataclasses.is_dataclass(value) and not isinstance(value, type):
        return dataclasses.asdict(value)
    raise TypeError(f"{type(value).__name__} is not JSON serializable")


def _canonical_json(value: object) -> str:
    return json.dumps(
        value,
        ensure_ascii=False,
        allow_nan=False,
        separators=(",", ":"),
        sort_keys=True,
        default=_json_default,
    )


def _sha256(value: object) -> str:
    encoded = value if isinstance(value, bytes) else _canonical_json(value).encode()
    return hashlib.sha256(encoded).hexdigest()


def _system_clock() -> datetime:
    return datetime.now(UTC)


def _timestamp(value: datetime) -> str:
    if value.tzinfo is None:
        value = value.replace(tzinfo=UTC)
    return value.astimezone(UTC).isoformat(timespec="microseconds").replace("+00:00", "Z")


class ChannelStore:
    """Thread-safe channel persistence using an independent WAL connection."""

    def __init__(
        self,
        path: str | Path,
        *,
        authorization_policy: AuthorizationPolicy | None = None,
        telegram_stream_key: str = "default",
        busy_timeout_ms: int = 5000,
        clock: Callable[[], datetime] = _system_clock,
    ) -> None:
        if busy_timeout_ms < 0:
            raise ValueError("busy_timeout_ms must be non-negative")
        self.path = Path(path)
        if str(path) != ":memory:":
            self.path.parent.mkdir(parents=True, exist_ok=True)
        self.authorization_policy = authorization_policy or AuthorizationPolicy()
        self.telegram_stream_key = telegram_stream_key
        self._clock = clock
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
            self._enable_wal(busy_timeout_ms)
            self._migrate()

    def __enter__(self) -> ChannelStore:
        self._ensure_open()
        return self

    def __exit__(self, _type: object, _value: object, _traceback: object) -> None:
        self.close()

    @property
    def schema_version(self) -> int:
        with self._lock:
            self._ensure_open()
            row = self._connection.execute(
                "SELECT version FROM channel_schema_version WHERE scope = ?",
                (_MIGRATION_SCOPE,),
            ).fetchone()
            return 0 if row is None else int(row["version"])

    def close(self) -> None:
        with self._lock:
            if not self._closed:
                self._connection.close()
                self._closed = True

    def _ensure_open(self) -> None:
        if self._closed:
            raise ChannelStoreClosedError("channel store is closed")

    def _enable_wal(self, busy_timeout_ms: int) -> None:
        deadline = time.monotonic() + (busy_timeout_ms / 1000)
        while True:
            try:
                self._connection.execute("PRAGMA journal_mode = WAL")
                return
            except sqlite3.OperationalError as exc:
                if "locked" not in str(exc).lower() or time.monotonic() >= deadline:
                    raise
                time.sleep(0.01)

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
            CREATE TABLE IF NOT EXISTS channel_schema_version (
                scope TEXT PRIMARY KEY,
                version INTEGER NOT NULL,
                applied_at TEXT NOT NULL
            )
            """
        )
        with self._transaction() as connection:
            row = connection.execute(
                "SELECT version FROM channel_schema_version WHERE scope = ?",
                (_MIGRATION_SCOPE,),
            ).fetchone()
            version = 0 if row is None else int(row["version"])
            if version > _CHANNEL_SCHEMA_VERSION:
                raise ChannelStoreError(
                    f"channel schema {version} is newer than supported {_CHANNEL_SCHEMA_VERSION}"
                )
            if version == 0:
                connection.execute(
                    """
                    CREATE TABLE IF NOT EXISTS channel_inbox (
                        platform TEXT NOT NULL,
                        external_event_id TEXT NOT NULL,
                        event_type TEXT NOT NULL,
                        user_id TEXT,
                        channel_id TEXT,
                        thread_key TEXT,
                        downstream_key TEXT,
                        payload_json TEXT,
                        payload_hash TEXT NOT NULL,
                        envelope_json TEXT,
                        auth_decision TEXT NOT NULL,
                        auth_reason TEXT NOT NULL,
                        status TEXT NOT NULL,
                        lease_owner TEXT,
                        lease_expires_at TEXT,
                        heartbeat_at TEXT,
                        attempt_count INTEGER NOT NULL DEFAULT 0,
                        run_id TEXT,
                        outbox_key TEXT,
                        error TEXT,
                        created_at TEXT NOT NULL,
                        updated_at TEXT NOT NULL,
                        PRIMARY KEY (platform, external_event_id),
                        UNIQUE (downstream_key)
                    )
                    """
                )
                connection.execute(
                    """
                    CREATE INDEX IF NOT EXISTS channel_inbox_claim_idx
                        ON channel_inbox(status, lease_expires_at, created_at)
                    """
                )
                connection.execute(
                    """
                    CREATE TABLE IF NOT EXISTS channel_outbox (
                        idempotency_key TEXT PRIMARY KEY,
                        platform TEXT NOT NULL,
                        channel_id TEXT NOT NULL,
                        thread_key TEXT NOT NULL,
                        operation TEXT NOT NULL,
                        message_json TEXT NOT NULL,
                        content_hash TEXT NOT NULL,
                        chunk_index INTEGER NOT NULL,
                        chunk_count INTEGER NOT NULL,
                        status TEXT NOT NULL,
                        lease_owner TEXT,
                        lease_expires_at TEXT,
                        heartbeat_at TEXT,
                        attempt_count INTEGER NOT NULL DEFAULT 0,
                        receipt_json TEXT,
                        error TEXT,
                        created_at TEXT NOT NULL,
                        updated_at TEXT NOT NULL
                    )
                    """
                )
                connection.execute(
                    """
                    CREATE INDEX IF NOT EXISTS channel_outbox_claim_idx
                        ON channel_outbox(status, lease_expires_at, created_at)
                    """
                )
                connection.execute(
                    """
                    CREATE TABLE IF NOT EXISTS telegram_offsets (
                        stream_key TEXT PRIMARY KEY,
                        next_offset INTEGER NOT NULL,
                        updated_at TEXT NOT NULL,
                        epoch INTEGER NOT NULL DEFAULT 0,
                        last_activity_at TEXT,
                        bot_id TEXT,
                        CHECK (next_offset >= 0)
                    )
                    """
                )
                connection.execute(
                    """
                    CREATE TABLE IF NOT EXISTS channel_auth_audit (
                        id INTEGER PRIMARY KEY AUTOINCREMENT,
                        platform TEXT NOT NULL,
                        external_event_id TEXT NOT NULL,
                        event_type TEXT NOT NULL,
                        user_id TEXT,
                        channel_id TEXT,
                        action TEXT,
                        payload_hash TEXT NOT NULL,
                        decision TEXT NOT NULL,
                        reason TEXT NOT NULL,
                        created_at TEXT NOT NULL
                    )
                    """
                )
                connection.execute(
                    """
                    CREATE INDEX IF NOT EXISTS channel_auth_audit_event_idx
                        ON channel_auth_audit(platform, external_event_id)
                    """
                )
                connection.execute(
                    """
                    CREATE TABLE IF NOT EXISTS channel_notifications (
                        notification_key TEXT PRIMARY KEY,
                        platform TEXT NOT NULL,
                        external_event_id TEXT NOT NULL,
                        outbox_keys_json TEXT NOT NULL,
                        created_at TEXT NOT NULL,
                        FOREIGN KEY (platform, external_event_id)
                            REFERENCES channel_inbox(platform, external_event_id)
                    )
                    """
                )
                connection.execute(
                    """
                    INSERT INTO channel_schema_version(scope, version, applied_at)
                    VALUES (?, ?, ?)
                    """,
                    (_MIGRATION_SCOPE, _CHANNEL_SCHEMA_VERSION, self._now()),
                )
                version = _CHANNEL_SCHEMA_VERSION
            if version == 1:
                connection.execute(
                    """
                    CREATE TABLE IF NOT EXISTS channel_notifications (
                        notification_key TEXT PRIMARY KEY,
                        platform TEXT NOT NULL,
                        external_event_id TEXT NOT NULL,
                        outbox_keys_json TEXT NOT NULL,
                        created_at TEXT NOT NULL,
                        FOREIGN KEY (platform, external_event_id)
                            REFERENCES channel_inbox(platform, external_event_id)
                    )
                    """
                )
                version = 2
            if version == 2:
                connection.execute(
                    "ALTER TABLE telegram_offsets ADD COLUMN epoch INTEGER NOT NULL DEFAULT 0"
                )
                connection.execute(
                    "ALTER TABLE telegram_offsets ADD COLUMN last_activity_at TEXT"
                )
                connection.execute(
                    """
                    UPDATE telegram_offsets
                    SET last_activity_at = updated_at
                    WHERE last_activity_at IS NULL
                    """
                )
                version = 3
            if version == 3:
                connection.execute("ALTER TABLE telegram_offsets ADD COLUMN bot_id TEXT")
                version = 4
            if row is not None and version != int(row["version"]):
                connection.execute(
                    """
                    UPDATE channel_schema_version
                    SET version = ?, applied_at = ?
                    WHERE scope = ?
                    """,
                    (version, self._now(), _MIGRATION_SCOPE),
                )

    def _now(self) -> str:
        return _timestamp(self._clock())

    def _deadline(self, seconds: float) -> str:
        if seconds <= 0:
            raise ValueError("lease_seconds must be positive")
        return _timestamp(self._clock() + timedelta(seconds=seconds))

    def get_telegram_offset(self, stream_key: str | None = None) -> int | None:
        key = stream_key or self.telegram_stream_key
        with self._lock:
            self._ensure_open()
            row = self._connection.execute(
                "SELECT next_offset FROM telegram_offsets WHERE stream_key = ?", (key,)
            ).fetchone()
            return None if row is None else int(row["next_offset"])

    def get_telegram_offset_state(self, stream_key: str) -> Any:
        from .telegram import TelegramOffsetState

        with self._lock:
            self._ensure_open()
            row = self._connection.execute(
                """
                SELECT next_offset, last_activity_at, epoch, bot_id
                FROM telegram_offsets
                WHERE stream_key = ?
                """,
                (stream_key,),
            ).fetchone()
        if row is None:
            return TelegramOffsetState(None, None)
        last_activity = (
            None
            if row["last_activity_at"] is None
            else datetime.fromisoformat(str(row["last_activity_at"]).replace("Z", "+00:00"))
        )
        return TelegramOffsetState(
            int(row["next_offset"]),
            last_activity,
            epoch=int(row["epoch"]),
            bot_id=None if row["bot_id"] is None else str(row["bot_id"]),
        )

    def bind_telegram_identity(self, bot_id: str, stream_key: str | None = None) -> int:
        if not bot_id:
            raise ValueError("bot_id must not be empty")
        key = stream_key or self.telegram_stream_key
        now = self._now()
        with self._transaction() as connection:
            row = connection.execute(
                "SELECT epoch, bot_id FROM telegram_offsets WHERE stream_key = ?",
                (key,),
            ).fetchone()
            if row is None:
                connection.execute(
                    """
                    INSERT INTO telegram_offsets(
                        stream_key, next_offset, updated_at, epoch, last_activity_at, bot_id
                    ) VALUES (?, 0, ?, 0, NULL, ?)
                    """,
                    (key, now, bot_id),
                )
                return 0
            epoch = int(row["epoch"])
            previous = row["bot_id"]
            if previous is None:
                connection.execute(
                    """
                    UPDATE telegram_offsets SET bot_id = ?, updated_at = ?
                    WHERE stream_key = ?
                    """,
                    (bot_id, now, key),
                )
            elif str(previous) != bot_id:
                epoch += 1
                connection.execute(
                    """
                    UPDATE telegram_offsets
                    SET bot_id = ?, next_offset = 0, epoch = ?,
                        updated_at = ?, last_activity_at = NULL
                    WHERE stream_key = ?
                    """,
                    (bot_id, epoch, now, key),
                )
            return epoch

    def begin_telegram_epoch(self, next_offset: int, stream_key: str | None = None) -> int:
        if next_offset < 0:
            raise ValueError("next_offset must be non-negative")
        key = stream_key or self.telegram_stream_key
        now = self._now()
        with self._transaction() as connection:
            connection.execute(
                """
                INSERT INTO telegram_offsets(
                    stream_key, next_offset, updated_at, epoch, last_activity_at
                ) VALUES (?, ?, ?, 1, ?)
                ON CONFLICT(stream_key) DO UPDATE SET
                    next_offset = excluded.next_offset,
                    updated_at = excluded.updated_at,
                    epoch = telegram_offsets.epoch + 1,
                    last_activity_at = excluded.last_activity_at
                """,
                (key, next_offset, now, now),
            )
            row = connection.execute(
                "SELECT epoch FROM telegram_offsets WHERE stream_key = ?",
                (key,),
            ).fetchone()
        return int(row["epoch"])

    def set_telegram_offset(self, next_offset: int, stream_key: str | None = None) -> None:
        if next_offset < 0:
            raise ValueError("next_offset must be non-negative")
        key = stream_key or self.telegram_stream_key
        now = self._now()
        with self._transaction() as connection:
            connection.execute(
                """
                INSERT INTO telegram_offsets(
                    stream_key, next_offset, updated_at, epoch, last_activity_at, bot_id
                )
                VALUES (?, ?, ?, 0, ?, NULL)
                ON CONFLICT(stream_key) DO UPDATE SET
                    next_offset = MAX(telegram_offsets.next_offset, excluded.next_offset),
                    updated_at = excluded.updated_at,
                    last_activity_at = excluded.last_activity_at
                """,
                (key, next_offset, now, now),
            )

    def ingest_telegram_update(
        self,
        update: Mapping[str, Any],
        policy: AuthorizationPolicy | None = None,
        *,
        stream_key: str | None = None,
    ) -> IngestResult:
        from .telegram import normalize_telegram_update

        if isinstance(update.get("update_id"), bool) or not isinstance(
            update.get("update_id"), int
        ):
            raise ValueError("Telegram update_id must be an integer")
        update_id = int(update["update_id"])
        if update_id < 0:
            raise ValueError("Telegram update_id must be non-negative")
        normalized = normalize_telegram_update(update, policy or self.authorization_policy)
        next_offset = update_id + 1
        key = stream_key or self.telegram_stream_key
        payload_hash = _sha256(update)
        now = self._now()
        allowed = normalized.decision is AuthDecision.ALLOW and normalized.envelope is not None
        payload_json = _canonical_json(update) if allowed else None
        envelope_json = _canonical_json(normalized.envelope) if allowed else None
        status = InboxStatus.RECEIVED if allowed else InboxStatus.COMPLETED
        envelope = normalized.envelope
        with self._transaction() as connection:
            offset_row = connection.execute(
                "SELECT epoch FROM telegram_offsets WHERE stream_key = ?",
                (key,),
            ).fetchone()
            epoch = 0 if offset_row is None else int(offset_row["epoch"])
            event_id = f"{epoch}:{update_id}"
            if envelope is not None:
                envelope = dataclasses.replace(
                    envelope,
                    external_event_id=event_id,
                    downstream_key=f"telegram:update:{epoch}:{update_id}",
                )
                envelope_json = _canonical_json(envelope) if allowed else None
            cursor = connection.execute(
                """
                INSERT INTO channel_inbox(
                    platform, external_event_id, event_type, user_id, channel_id,
                    thread_key, downstream_key, payload_json, payload_hash, envelope_json,
                    auth_decision, auth_reason, status, created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(platform, external_event_id) DO NOTHING
                """,
                (
                    Platform.TELEGRAM,
                    event_id,
                    normalized.event_type,
                    normalized.user_id,
                    normalized.channel_id,
                    envelope.thread_key if allowed and envelope else None,
                    envelope.downstream_key if allowed and envelope else None,
                    payload_json,
                    payload_hash,
                    envelope_json,
                    normalized.decision,
                    normalized.reason,
                    status,
                    now,
                    now,
                ),
            )
            duplicate = cursor.rowcount == 0
            connection.execute(
                """
                INSERT INTO telegram_offsets(
                    stream_key, next_offset, updated_at, epoch, last_activity_at, bot_id
                )
                VALUES (?, ?, ?, 0, ?, NULL)
                ON CONFLICT(stream_key) DO UPDATE SET
                    next_offset = MAX(telegram_offsets.next_offset, excluded.next_offset),
                    updated_at = excluded.updated_at,
                    last_activity_at = excluded.last_activity_at
                """,
                (key, next_offset, now, now),
            )
            if not duplicate:
                connection.execute(
                    """
                    INSERT INTO channel_auth_audit(
                        platform, external_event_id, event_type, user_id, channel_id,
                        action, payload_hash, decision, reason, created_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        Platform.TELEGRAM,
                        event_id,
                        normalized.event_type,
                        normalized.user_id,
                        normalized.channel_id,
                        normalized.action,
                        payload_hash,
                        normalized.decision,
                        normalized.reason,
                        now,
                    ),
                )
            offset_row = connection.execute(
                "SELECT next_offset FROM telegram_offsets WHERE stream_key = ?", (key,)
            ).fetchone()
        durable_offset = next_offset if offset_row is None else int(offset_row["next_offset"])
        return IngestResult(
            envelope=envelope if allowed else None,
            decision=normalized.decision,
            reason=normalized.reason,
            duplicate=duplicate,
            next_offset=durable_offset,
        )

    def ingest_envelope(
        self,
        envelope: ChannelEnvelope,
        payload: Mapping[str, Any],
        *,
        decision: AuthDecision | None = None,
        reason: str | None = None,
    ) -> IngestResult:
        """Persist a normalized event for any adapter without exposing unauthorized content."""
        auth = self.authorization_policy.evaluate(
            envelope.platform,
            envelope.user_id,
            envelope.channel_id,
            envelope.action,
        )
        selected_decision = auth.decision if decision is None else decision
        selected_reason = auth.reason if reason is None else reason
        allowed = selected_decision is AuthDecision.ALLOW
        now = self._now()
        payload_hash = _sha256(payload)
        with self._transaction() as connection:
            cursor = connection.execute(
                """
                INSERT INTO channel_inbox(
                    platform, external_event_id, event_type, user_id, channel_id,
                    thread_key, downstream_key, payload_json, payload_hash, envelope_json,
                    auth_decision, auth_reason, status, created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(platform, external_event_id) DO NOTHING
                """,
                (
                    envelope.platform,
                    envelope.external_event_id,
                    envelope.event_type,
                    envelope.user_id,
                    envelope.channel_id,
                    envelope.thread_key if allowed else None,
                    envelope.downstream_key if allowed else None,
                    _canonical_json(payload) if allowed else None,
                    payload_hash,
                    _canonical_json(envelope) if allowed else None,
                    selected_decision,
                    selected_reason,
                    InboxStatus.RECEIVED if allowed else InboxStatus.COMPLETED,
                    now,
                    now,
                ),
            )
            duplicate = cursor.rowcount == 0
            if not duplicate:
                connection.execute(
                    """
                    INSERT INTO channel_auth_audit(
                        platform, external_event_id, event_type, user_id, channel_id,
                        action, payload_hash, decision, reason, created_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        envelope.platform,
                        envelope.external_event_id,
                        envelope.event_type,
                        envelope.user_id,
                        envelope.channel_id,
                        envelope.action,
                        payload_hash,
                        selected_decision,
                        selected_reason,
                        now,
                    ),
                )
        return IngestResult(
            envelope=envelope if allowed else None,
            decision=selected_decision,
            reason=selected_reason,
            duplicate=duplicate,
            next_offset=None,
        )

    def claim_inbox(
        self,
        owner: str,
        *,
        lease_seconds: float = 60,
        platform: Platform | str | None = None,
    ) -> InboxRecord | None:
        if not owner:
            raise ValueError("owner must not be empty")
        now = self._now()
        deadline = self._deadline(lease_seconds)
        with self._transaction() as connection:
            self._recover_inbox_locked(connection, now)
            parameters: list[object] = [InboxStatus.RECEIVED]
            platform_filter = ""
            if platform is not None:
                platform_filter = " AND platform = ?"
                parameters.append(Platform(platform))
            row = connection.execute(
                f"""
                SELECT * FROM channel_inbox
                WHERE status = ?{platform_filter}
                ORDER BY created_at, rowid
                LIMIT 1
                """,
                parameters,
            ).fetchone()
            if row is None:
                return None
            connection.execute(
                """
                UPDATE channel_inbox SET
                    status = ?, lease_owner = ?, lease_expires_at = ?,
                    heartbeat_at = ?, attempt_count = attempt_count + 1, updated_at = ?
                WHERE platform = ? AND external_event_id = ? AND status = ?
                """,
                (
                    InboxStatus.PROCESSING,
                    owner,
                    deadline,
                    now,
                    now,
                    row["platform"],
                    row["external_event_id"],
                    InboxStatus.RECEIVED,
                ),
            )
            claimed = connection.execute(
                "SELECT * FROM channel_inbox WHERE platform = ? AND external_event_id = ?",
                (row["platform"], row["external_event_id"]),
            ).fetchone()
        return self._inbox_from_row(claimed)

    @staticmethod
    def _recover_inbox_locked(connection: sqlite3.Connection, now: str) -> int:
        cursor = connection.execute(
            """
            UPDATE channel_inbox SET
                status = CASE WHEN downstream_key IS NOT NULL THEN ? ELSE ? END,
                error = CASE
                    WHEN downstream_key IS NULL THEN 'expired without deterministic downstream key'
                    ELSE error
                END,
                lease_owner = NULL, lease_expires_at = NULL, heartbeat_at = NULL,
                updated_at = ?
            WHERE status = ? AND lease_expires_at <= ?
            """,
            (
                InboxStatus.RECEIVED,
                InboxStatus.FAILED,
                now,
                InboxStatus.PROCESSING,
                now,
            ),
        )
        return cursor.rowcount

    def recover_inbox_leases(self) -> int:
        now = self._now()
        with self._transaction() as connection:
            return self._recover_inbox_locked(connection, now)

    def heartbeat_inbox(
        self,
        platform: Platform | str,
        external_event_id: str,
        owner: str,
        *,
        lease_seconds: float = 60,
    ) -> None:
        now = self._now()
        deadline = self._deadline(lease_seconds)
        with self._transaction() as connection:
            cursor = connection.execute(
                """
                UPDATE channel_inbox
                SET lease_expires_at = ?, heartbeat_at = ?, updated_at = ?
                WHERE platform = ? AND external_event_id = ?
                    AND status = ? AND lease_owner = ? AND lease_expires_at > ?
                """,
                (
                    deadline,
                    now,
                    now,
                    Platform(platform),
                    external_event_id,
                    InboxStatus.PROCESSING,
                    owner,
                    now,
                ),
            )
            if cursor.rowcount != 1:
                raise ChannelTransitionError("inbox lease is missing, expired, or owned elsewhere")

    def link_inbox_run(
        self,
        platform: Platform | str,
        external_event_id: str,
        downstream_key: str,
        run_id: str,
    ) -> None:
        """Persist a run link while its inbox handler still owns the processing lease."""
        if not run_id:
            raise ValueError("run_id must not be empty")
        now = self._now()
        with self._transaction() as connection:
            row = connection.execute(
                """
                SELECT run_id FROM channel_inbox
                WHERE platform = ? AND external_event_id = ? AND downstream_key = ?
                    AND status = ? AND lease_expires_at > ?
                """,
                (
                    Platform(platform),
                    external_event_id,
                    downstream_key,
                    InboxStatus.PROCESSING,
                    now,
                ),
            ).fetchone()
            if row is None:
                raise ChannelTransitionError("inbox is not processing for this downstream key")
            if row["run_id"] is not None and row["run_id"] != run_id:
                raise ChannelTransitionError("inbox is already linked to a different run")
            connection.execute(
                """
                UPDATE channel_inbox SET run_id = ?, updated_at = ?
                WHERE platform = ? AND external_event_id = ? AND downstream_key = ?
                    AND status = ? AND lease_expires_at > ?
                """,
                (
                    run_id,
                    now,
                    Platform(platform),
                    external_event_id,
                    downstream_key,
                    InboxStatus.PROCESSING,
                    now,
                ),
            )

    def complete_inbox(
        self,
        platform: Platform | str,
        external_event_id: str,
        owner: str,
        *,
        run_id: str | None = None,
        outbox_key: str | None = None,
    ) -> None:
        self._finish_inbox(
            platform,
            external_event_id,
            owner,
            InboxStatus.COMPLETED,
            run_id=run_id,
            outbox_key=outbox_key,
            error=None,
        )

    def complete_inbox_with_outbox(
        self,
        platform: Platform | str,
        external_event_id: str,
        owner: str,
        messages: Iterable[OutboundMessage],
        *,
        run_id: str | None = None,
    ) -> tuple[OutboxRecord, ...]:
        batch = tuple(messages)
        now = self._now()
        with self._transaction() as connection:
            rows = tuple(
                self._enqueue_outbox_locked(connection, message, now) for message in batch
            )
            cursor = connection.execute(
                """
                UPDATE channel_inbox SET
                    status = ?, run_id = COALESCE(?, run_id),
                    outbox_key = COALESCE(?, outbox_key), error = NULL,
                    lease_owner = NULL, lease_expires_at = NULL, heartbeat_at = NULL,
                    updated_at = ?
                WHERE platform = ? AND external_event_id = ?
                    AND status = ? AND lease_owner = ? AND lease_expires_at > ?
                """,
                (
                    InboxStatus.COMPLETED,
                    run_id,
                    batch[0].idempotency_key if batch else None,
                    now,
                    Platform(platform),
                    external_event_id,
                    InboxStatus.PROCESSING,
                    owner,
                    now,
                ),
            )
            if cursor.rowcount != 1:
                raise ChannelTransitionError("inbox is not processing for this owner")
        return tuple(self._outbox_from_row(row) for row in rows)

    def fail_inbox(
        self,
        platform: Platform | str,
        external_event_id: str,
        owner: str,
        error: str,
        *,
        run_id: str | None = None,
    ) -> None:
        self._finish_inbox(
            platform,
            external_event_id,
            owner,
            InboxStatus.FAILED,
            run_id=run_id,
            outbox_key=None,
            error=error,
        )

    def _finish_inbox(
        self,
        platform: Platform | str,
        external_event_id: str,
        owner: str,
        status: InboxStatus,
        *,
        run_id: str | None,
        outbox_key: str | None,
        error: str | None,
    ) -> None:
        now = self._now()
        with self._transaction() as connection:
            cursor = connection.execute(
                """
                UPDATE channel_inbox SET
                    status = ?, run_id = COALESCE(?, run_id),
                    outbox_key = COALESCE(?, outbox_key), error = ?,
                    lease_owner = NULL, lease_expires_at = NULL, heartbeat_at = NULL,
                    updated_at = ?
                WHERE platform = ? AND external_event_id = ?
                    AND status = ? AND lease_owner = ? AND lease_expires_at > ?
                """,
                (
                    status,
                    run_id,
                    outbox_key,
                    error,
                    now,
                    Platform(platform),
                    external_event_id,
                    InboxStatus.PROCESSING,
                    owner,
                    now,
                ),
            )
            if cursor.rowcount != 1:
                raise ChannelTransitionError("inbox is not processing for this owner")

    def get_inbox(
        self, platform: Platform | str, external_event_id: str
    ) -> InboxRecord | None:
        with self._lock:
            self._ensure_open()
            row = self._connection.execute(
                "SELECT * FROM channel_inbox WHERE platform = ? AND external_event_id = ?",
                (Platform(platform), external_event_id),
            ).fetchone()
        return None if row is None else self._inbox_from_row(row)

    def list_linked_inbox(
        self,
        *,
        platform: Platform | str | None = None,
        limit: int = 500,
        after: tuple[str, Platform | str, str] | None = None,
    ) -> tuple[InboxRecord, ...]:
        if limit < 1 or limit > 5000:
            raise ValueError("limit must be between 1 and 5000")
        parameters: list[object] = []
        platform_filter = ""
        if platform is not None:
            platform_filter = " AND platform = ?"
            parameters.append(Platform(platform))
        cursor_filter = ""
        if after is not None:
            updated_at, after_platform, external_event_id = after
            cursor_filter = """
                AND (
                    updated_at > ?
                    OR (updated_at = ? AND platform > ?)
                    OR (updated_at = ? AND platform = ? AND external_event_id > ?)
                )
            """
            parameters.extend(
                (
                    updated_at,
                    updated_at,
                    Platform(after_platform),
                    updated_at,
                    Platform(after_platform),
                    external_event_id,
                )
            )
        parameters.append(limit)
        with self._lock:
            self._ensure_open()
            rows = self._connection.execute(
                f"""
                SELECT * FROM channel_inbox
                WHERE run_id IS NOT NULL{platform_filter}{cursor_filter}
                ORDER BY updated_at, platform, external_event_id
                LIMIT ?
                """,
                parameters,
            ).fetchall()
        return tuple(self._inbox_from_row(row) for row in rows)

    def notification_exists(self, notification_key: str) -> bool:
        with self._lock:
            self._ensure_open()
            row = self._connection.execute(
                "SELECT 1 FROM channel_notifications WHERE notification_key = ?",
                (notification_key,),
            ).fetchone()
        return row is not None

    def record_notification(
        self,
        notification_key: str,
        platform: Platform | str,
        external_event_id: str,
        outbox_keys: tuple[str, ...],
    ) -> None:
        if not notification_key:
            raise ValueError("notification_key must not be empty")
        if not outbox_keys:
            raise ValueError("notification requires at least one outbox key")
        keys_json = _canonical_json(outbox_keys)
        now = self._now()
        with self._transaction() as connection:
            missing = [
                key
                for key in outbox_keys
                if connection.execute(
                    "SELECT 1 FROM channel_outbox WHERE idempotency_key = ?", (key,)
                ).fetchone()
                is None
            ]
            if missing:
                raise ChannelStoreError("notification references an outbox row that is not durable")
            connection.execute(
                """
                INSERT INTO channel_notifications(
                    notification_key, platform, external_event_id, outbox_keys_json, created_at
                ) VALUES (?, ?, ?, ?, ?)
                ON CONFLICT(notification_key) DO NOTHING
                """,
                (
                    notification_key,
                    Platform(platform),
                    external_event_id,
                    keys_json,
                    now,
                ),
            )
            row = connection.execute(
                """
                SELECT platform, external_event_id, outbox_keys_json
                FROM channel_notifications WHERE notification_key = ?
                """,
                (notification_key,),
            ).fetchone()
            if row is None:
                raise ChannelStoreError("failed to read recorded notification")
            if (
                row["platform"] != Platform(platform)
                or row["external_event_id"] != external_event_id
                or row["outbox_keys_json"] != keys_json
            ):
                raise ChannelStoreError(
                    "notification key already exists with different outbox rows"
                )

    def enqueue_outbox(self, message: OutboundMessage) -> OutboxRecord:
        now = self._now()
        with self._transaction() as connection:
            row = self._enqueue_outbox_locked(connection, message, now)
        return self._outbox_from_row(row)

    @staticmethod
    def _enqueue_outbox_locked(
        connection: sqlite3.Connection,
        message: OutboundMessage,
        now: str,
    ) -> sqlite3.Row:
        message_json = _canonical_json(message)
        content_hash = _sha256(
            {
                "platform": message.platform,
                "channel_id": message.channel_id,
                "operation": message.operation,
                "text": message.text,
                "parse_mode": message.parse_mode,
                "chunk_index": message.chunk_index,
            }
        )
        connection.execute(
            """
            INSERT INTO channel_outbox(
                idempotency_key, platform, channel_id, thread_key, operation,
                message_json, content_hash, chunk_index, chunk_count, status,
                created_at, updated_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(idempotency_key) DO NOTHING
            """,
            (
                message.idempotency_key,
                message.platform,
                message.channel_id,
                message.thread_key,
                message.operation,
                message_json,
                content_hash,
                message.chunk_index,
                message.chunk_count,
                OutboxStatus.PENDING,
                now,
                now,
            ),
        )
        row = connection.execute(
            "SELECT * FROM channel_outbox WHERE idempotency_key = ?",
            (message.idempotency_key,),
        ).fetchone()
        if row is None:
            raise ChannelStoreError("failed to read enqueued outbox row")
        if row["content_hash"] != content_hash or row["message_json"] != message_json:
            raise ChannelStoreError("idempotency key already exists with different content")
        return cast(sqlite3.Row, row)

    def claim_outbox(
        self,
        owner: str,
        *,
        lease_seconds: float = 60,
        platform: Platform | str | None = None,
    ) -> OutboxRecord | None:
        if not owner:
            raise ValueError("owner must not be empty")
        now = self._now()
        deadline = self._deadline(lease_seconds)
        with self._transaction() as connection:
            self._recover_outbox_locked(connection, now)
            parameters: list[object] = [OutboxStatus.PENDING]
            platform_filter = ""
            if platform is not None:
                platform_filter = " AND platform = ?"
                parameters.append(Platform(platform))
            row = connection.execute(
                f"""
                SELECT * FROM channel_outbox
                WHERE status = ?{platform_filter}
                ORDER BY created_at, rowid LIMIT 1
                """,
                parameters,
            ).fetchone()
            if row is None:
                return None
            connection.execute(
                """
                UPDATE channel_outbox SET
                    status = ?, lease_owner = ?, lease_expires_at = ?,
                    heartbeat_at = ?, attempt_count = attempt_count + 1, updated_at = ?
                WHERE idempotency_key = ? AND status = ?
                """,
                (
                    OutboxStatus.SENDING,
                    owner,
                    deadline,
                    now,
                    now,
                    row["idempotency_key"],
                    OutboxStatus.PENDING,
                ),
            )
            claimed = connection.execute(
                "SELECT * FROM channel_outbox WHERE idempotency_key = ?",
                (row["idempotency_key"],),
            ).fetchone()
        return self._outbox_from_row(claimed)

    @staticmethod
    def _recover_outbox_locked(connection: sqlite3.Connection, now: str) -> int:
        cursor = connection.execute(
            """
            UPDATE channel_outbox SET
                status = ?, error = 'sending lease expired; remote outcome is unknown',
                lease_owner = NULL, lease_expires_at = NULL, heartbeat_at = NULL,
                updated_at = ?
            WHERE status = ? AND lease_expires_at <= ?
            """,
            (OutboxStatus.UNKNOWN, now, OutboxStatus.SENDING, now),
        )
        return cursor.rowcount

    def recover_outbox_leases(self) -> int:
        now = self._now()
        with self._transaction() as connection:
            return self._recover_outbox_locked(connection, now)

    def heartbeat_outbox(
        self,
        idempotency_key: str,
        owner: str,
        *,
        lease_seconds: float = 60,
    ) -> None:
        now = self._now()
        deadline = self._deadline(lease_seconds)
        with self._transaction() as connection:
            cursor = connection.execute(
                """
                UPDATE channel_outbox
                SET lease_expires_at = ?, heartbeat_at = ?, updated_at = ?
                WHERE idempotency_key = ? AND status = ?
                    AND lease_owner = ? AND lease_expires_at > ?
                """,
                (
                    deadline,
                    now,
                    now,
                    idempotency_key,
                    OutboxStatus.SENDING,
                    owner,
                    now,
                ),
            )
            if cursor.rowcount != 1:
                raise ChannelTransitionError("outbox lease is missing, expired, or owned elsewhere")

    def mark_outbox_sent(
        self, idempotency_key: str, owner: str, receipt: RemoteReceipt
    ) -> OutboxRecord:
        return self._finish_outbox(
            idempotency_key,
            owner,
            OutboxStatus.SENT,
            receipt=receipt,
            error=None,
        )

    def mark_outbox_unknown(
        self, idempotency_key: str, owner: str, error: str
    ) -> OutboxRecord:
        return self._finish_outbox(
            idempotency_key,
            owner,
            OutboxStatus.UNKNOWN,
            receipt=None,
            error=error,
        )

    def mark_outbox_failed(
        self, idempotency_key: str, owner: str, error: str
    ) -> OutboxRecord:
        return self._finish_outbox(
            idempotency_key,
            owner,
            OutboxStatus.FAILED,
            receipt=None,
            error=error,
        )

    def _finish_outbox(
        self,
        idempotency_key: str,
        owner: str,
        status: OutboxStatus,
        *,
        receipt: RemoteReceipt | None,
        error: str | None,
    ) -> OutboxRecord:
        now = self._now()
        receipt_json = None if receipt is None else _canonical_json(receipt)
        with self._transaction() as connection:
            cursor = connection.execute(
                """
                UPDATE channel_outbox SET
                    status = ?, receipt_json = ?, error = ?,
                    lease_owner = NULL, lease_expires_at = NULL, heartbeat_at = NULL,
                    updated_at = ?
                WHERE idempotency_key = ? AND status = ? AND lease_owner = ?
                    AND lease_expires_at > ?
                """,
                (
                    status,
                    receipt_json,
                    error,
                    now,
                    idempotency_key,
                    OutboxStatus.SENDING,
                    owner,
                    now,
                ),
            )
            if cursor.rowcount != 1:
                raise ChannelTransitionError("outbox is not sending for this owner")
            row = connection.execute(
                "SELECT * FROM channel_outbox WHERE idempotency_key = ?", (idempotency_key,)
            ).fetchone()
        return self._outbox_from_row(row)

    def resolve_outbox(
        self, idempotency_key: str, *, sent: bool, note: str
    ) -> OutboxRecord:
        if not note:
            raise ValueError("operator resolution note must not be empty")
        status = OutboxStatus.SENT if sent else OutboxStatus.FAILED
        now = self._now()
        with self._transaction() as connection:
            cursor = connection.execute(
                """
                UPDATE channel_outbox SET status = ?, error = ?, updated_at = ?
                WHERE idempotency_key = ? AND status = ?
                """,
                (status, note, now, idempotency_key, OutboxStatus.UNKNOWN),
            )
            if cursor.rowcount != 1:
                raise ChannelTransitionError("only unknown outbox rows can be resolved")
            row = connection.execute(
                "SELECT * FROM channel_outbox WHERE idempotency_key = ?", (idempotency_key,)
            ).fetchone()
        return self._outbox_from_row(row)

    def retry_outbox(self, idempotency_key: str, *, note: str) -> OutboxRecord:
        if not note:
            raise ValueError("operator retry note must not be empty")
        now = self._now()
        with self._transaction() as connection:
            cursor = connection.execute(
                """
                UPDATE channel_outbox SET status = ?, error = ?, updated_at = ?
                WHERE idempotency_key = ? AND status IN (?, ?)
                """,
                (
                    OutboxStatus.PENDING,
                    f"operator retry: {note}",
                    now,
                    idempotency_key,
                    OutboxStatus.UNKNOWN,
                    OutboxStatus.FAILED,
                ),
            )
            if cursor.rowcount != 1:
                raise ChannelTransitionError("only unknown or failed outbox rows can be retried")
            row = connection.execute(
                "SELECT * FROM channel_outbox WHERE idempotency_key = ?", (idempotency_key,)
            ).fetchone()
        return self._outbox_from_row(row)

    def get_outbox(self, idempotency_key: str) -> OutboxRecord | None:
        with self._lock:
            self._ensure_open()
            row = self._connection.execute(
                "SELECT * FROM channel_outbox WHERE idempotency_key = ?", (idempotency_key,)
            ).fetchone()
        return None if row is None else self._outbox_from_row(row)

    def list_outbox(
        self,
        *,
        status: OutboxStatus | str | None = None,
        platform: Platform | str | None = None,
        limit: int = 500,
    ) -> tuple[OutboxRecord, ...]:
        if limit < 1 or limit > 5000:
            raise ValueError("limit must be between 1 and 5000")
        clauses: list[str] = []
        parameters: list[object] = []
        if status is not None:
            clauses.append("status = ?")
            parameters.append(OutboxStatus(status))
        if platform is not None:
            clauses.append("platform = ?")
            parameters.append(Platform(platform))
        query = "SELECT * FROM channel_outbox"
        if clauses:
            query += " WHERE " + " AND ".join(clauses)
        query += " ORDER BY created_at, idempotency_key LIMIT ?"
        parameters.append(limit)
        with self._lock:
            self._ensure_open()
            rows = self._connection.execute(query, parameters).fetchall()
        return tuple(self._outbox_from_row(row) for row in rows)

    def list_unknown_outbox(
        self,
        *,
        platform: Platform | str | None = None,
        limit: int = 500,
    ) -> tuple[OutboxRecord, ...]:
        return self.list_outbox(
            status=OutboxStatus.UNKNOWN,
            platform=platform,
            limit=limit,
        )

    def export_auth_audit(self) -> list[dict[str, Any]]:
        with self._lock:
            self._ensure_open()
            rows = self._connection.execute(
                """
                SELECT platform, external_event_id, event_type, user_id, channel_id,
                    action, payload_hash, decision, reason, created_at
                FROM channel_auth_audit ORDER BY id
                """
            ).fetchall()
        return [dict(row) for row in rows]

    @staticmethod
    def _envelope_from_json(value: str) -> ChannelEnvelope:
        data = json.loads(value)
        return ChannelEnvelope(
            platform=Platform(data["platform"]),
            external_event_id=str(data["external_event_id"]),
            event_type=str(data["event_type"]),
            user_id=str(data["user_id"]),
            channel_id=str(data["channel_id"]),
            thread_key=str(data["thread_key"]),
            downstream_key=str(data["downstream_key"]),
            text=data.get("text"),
            message_id=data.get("message_id"),
            callback_query_id=data.get("callback_query_id"),
            callback_data=data.get("callback_data"),
            action=str(data.get("action", "message")),
            received_at=data.get("received_at"),
            metadata=dict(data.get("metadata", {})),
        )

    @classmethod
    def _inbox_from_row(cls, row: sqlite3.Row) -> InboxRecord:
        envelope_json = row["envelope_json"]
        if envelope_json is None:
            envelope = ChannelEnvelope(
                platform=Platform(row["platform"]),
                external_event_id=str(row["external_event_id"]),
                event_type=str(row["event_type"]),
                user_id=str(row["user_id"] or ""),
                channel_id=str(row["channel_id"] or ""),
                thread_key=str(row["thread_key"] or ""),
                downstream_key=str(row["downstream_key"] or ""),
            )
        else:
            envelope = cls._envelope_from_json(str(envelope_json))
        return InboxRecord(
            envelope=envelope,
            status=InboxStatus(row["status"]),
            auth_decision=AuthDecision(row["auth_decision"]),
            auth_reason=str(row["auth_reason"]),
            lease_owner=row["lease_owner"],
            lease_expires_at=row["lease_expires_at"],
            attempt_count=int(row["attempt_count"]),
            run_id=row["run_id"],
            outbox_key=row["outbox_key"],
            error=row["error"],
            created_at=str(row["created_at"]),
            updated_at=str(row["updated_at"]),
        )

    @staticmethod
    def _message_from_json(value: str) -> OutboundMessage:
        data = json.loads(value)
        return OutboundMessage(
            platform=Platform(data["platform"]),
            idempotency_key=str(data["idempotency_key"]),
            channel_id=str(data["channel_id"]),
            thread_key=str(data["thread_key"]),
            text=str(data["text"]),
            operation=MessageOperation(data["operation"]),
            parse_mode=ParseMode(data["parse_mode"]),
            message_id=data.get("message_id"),
            callback_query_id=data.get("callback_query_id"),
            disable_notification=bool(data.get("disable_notification", False)),
            chunk_index=int(data.get("chunk_index", 0)),
            chunk_count=int(data.get("chunk_count", 1)),
            metadata=dict(data.get("metadata", {})),
        )

    @staticmethod
    def _receipt_from_json(value: str | None) -> RemoteReceipt | None:
        if value is None:
            return None
        data = json.loads(value)
        return RemoteReceipt(
            platform=Platform(data["platform"]),
            idempotency_key=str(data["idempotency_key"]),
            remote_message_id=data.get("remote_message_id"),
            channel_id=str(data["channel_id"]),
            operation=MessageOperation(data["operation"]),
            remote_message_ids=tuple(str(item) for item in data.get("remote_message_ids", ())),
        )

    @classmethod
    def _outbox_from_row(cls, row: sqlite3.Row) -> OutboxRecord:
        return OutboxRecord(
            message=cls._message_from_json(str(row["message_json"])),
            status=OutboxStatus(row["status"]),
            content_hash=str(row["content_hash"]),
            lease_owner=row["lease_owner"],
            lease_expires_at=row["lease_expires_at"],
            attempt_count=int(row["attempt_count"]),
            remote_receipt=cls._receipt_from_json(row["receipt_json"]),
            error=row["error"],
            created_at=str(row["created_at"]),
            updated_at=str(row["updated_at"]),
        )
