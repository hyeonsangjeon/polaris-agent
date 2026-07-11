from __future__ import annotations

import sqlite3
import threading
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from polaris.channels import (
    AuthDecision,
    AuthorizationPolicy,
    ChannelEnvelope,
    InboxStatus,
    MessageOperation,
    OutboundMessage,
    OutboxStatus,
    Platform,
    RemoteReceipt,
)
from polaris.channels.store import ChannelStore, ChannelStoreError
from polaris.channels.telegram import normalize_telegram_update


def policy() -> AuthorizationPolicy:
    return AuthorizationPolicy(
        allowed_user_ids=[10],
        allowed_chat_ids=[20],
        allowed_actions=["message", "command", "callback"],
    )


def message_update(update_id: int, text: str = "hello") -> dict[str, object]:
    return {
        "update_id": update_id,
        "message": {
            "message_id": 30,
            "from": {"id": 10},
            "chat": {"id": 20},
            "text": text,
        },
    }


def outbound(key: str) -> OutboundMessage:
    return OutboundMessage(
        platform=Platform.TELEGRAM,
        idempotency_key=key,
        channel_id="20",
        thread_key="telegram:20",
        text="response",
    )


def test_migration_reopen_duplicate_and_atomic_offset(tmp_path: Path) -> None:
    path = tmp_path / "journal.sqlite3"
    first = ChannelStore(path, authorization_policy=policy())
    result = first.ingest_telegram_update(message_update(12))
    assert result.accepted
    assert first.get_telegram_offset() == 13
    first.close()

    reopened = ChannelStore(path, authorization_policy=policy())
    duplicate = reopened.ingest_telegram_update(message_update(12))
    assert duplicate.duplicate
    assert reopened.claim_inbox("worker") is not None
    assert reopened.claim_inbox("other") is None
    assert reopened.schema_version == 4


def test_concurrent_first_openers_share_one_migration(tmp_path: Path) -> None:
    path = tmp_path / "journal.sqlite3"

    def open_store(_index: int) -> int:
        with ChannelStore(path) as store:
            return store.schema_version

    with ThreadPoolExecutor(max_workers=2) as executor:
        versions = list(executor.map(open_store, range(2)))

    assert versions == [4, 4]


def test_v3_offset_state_migrates_bot_identity_without_losing_offset(
    tmp_path: Path,
) -> None:
    path = tmp_path / "journal.sqlite3"
    connection = sqlite3.connect(path)
    connection.executescript(
        """
        CREATE TABLE channel_schema_version (
            scope TEXT PRIMARY KEY,
            version INTEGER NOT NULL,
            applied_at TEXT NOT NULL
        );
        INSERT INTO channel_schema_version
            VALUES ('channels', 3, '2026-01-01T00:00:00Z');
        CREATE TABLE telegram_offsets (
            stream_key TEXT PRIMARY KEY,
            next_offset INTEGER NOT NULL,
            updated_at TEXT NOT NULL,
            epoch INTEGER NOT NULL DEFAULT 0,
            last_activity_at TEXT,
            CHECK (next_offset >= 0)
        );
        INSERT INTO telegram_offsets VALUES (
            'default', 42, '2026-01-01T00:00:00Z', 2, '2026-01-01T00:00:00Z'
        );
        """
    )
    connection.close()

    with ChannelStore(path) as store:
        assert store.schema_version == 4
        assert store.get_telegram_offset_state("default").bot_id is None
        assert store.bind_telegram_identity("7") == 2
        state = store.get_telegram_offset_state("default")
        assert state.offset == 42
        assert state.epoch == 2
        assert state.bot_id == "7"


def test_normalizes_messages_commands_callbacks_and_thread_keys() -> None:
    normal = normalize_telegram_update(message_update(1), policy())
    assert normal.envelope is not None
    assert normal.envelope.thread_key == "telegram:20"
    assert normal.envelope.action == "message"

    command = normalize_telegram_update(message_update(2, "/start"), policy())
    assert command.envelope is not None
    assert command.envelope.action == "command"

    callback = normalize_telegram_update(
        {
            "update_id": 3,
            "callback_query": {
                "id": "callback-id",
                "from": {"id": 10},
                "data": "approve",
                "message": {"message_id": 31, "chat": {"id": 20}},
            },
        },
        policy(),
    )
    assert callback.decision is AuthDecision.ALLOW
    assert callback.envelope is not None
    assert callback.envelope.callback_data == "approve"
    assert callback.envelope.callback_query_id == "callback-id"
    assert callback.envelope.thread_key == "telegram:20"


def test_outbox_concurrency_and_transitions(tmp_path: Path) -> None:
    path = tmp_path / "journal.sqlite3"
    first = ChannelStore(path)
    second = ChannelStore(path)
    first.enqueue_outbox(outbound("key"))
    barrier = threading.Barrier(2)
    claims: list[object] = []

    def claim(store: ChannelStore, owner: str) -> None:
        barrier.wait()
        claims.append(store.claim_outbox(owner))

    one = threading.Thread(target=claim, args=(first, "one"))
    two = threading.Thread(target=claim, args=(second, "two"))
    one.start()
    two.start()
    one.join()
    two.join()
    claimed = [item for item in claims if item is not None]
    assert len(claimed) == 1
    record = claimed[0]
    assert hasattr(record, "lease_owner")
    owner = str(record.lease_owner)
    receipt = RemoteReceipt(
        platform=Platform.TELEGRAM,
        idempotency_key="key",
        remote_message_id="99",
        channel_id="20",
        operation=MessageOperation.SEND,
    )
    sent = first.mark_outbox_sent("key", owner, receipt)
    assert sent.status is OutboxStatus.SENT


def test_outbox_batch_and_inbox_completion_are_atomic(tmp_path: Path) -> None:
    store = ChannelStore(
        tmp_path / "journal.sqlite3",
        authorization_policy=policy(),
    )
    store.ingest_telegram_update(message_update(30))
    claimed = store.claim_inbox("worker")
    assert claimed is not None
    store.enqueue_outbox(outbound("conflict"))
    conflicting = OutboundMessage(
        platform=Platform.TELEGRAM,
        idempotency_key="conflict",
        channel_id="20",
        thread_key="telegram:20",
        text="different",
    )

    with pytest.raises(ChannelStoreError, match="different content"):
        store.complete_inbox_with_outbox(
            Platform.TELEGRAM,
            claimed.envelope.external_event_id,
            "worker",
            (outbound("new"), conflicting),
            run_id="run-30",
        )

    assert store.get_outbox("new") is None
    still_processing = store.get_inbox(
        Platform.TELEGRAM,
        claimed.envelope.external_event_id,
    )
    assert still_processing is not None
    assert still_processing.status is InboxStatus.PROCESSING
    store.complete_inbox_with_outbox(
        Platform.TELEGRAM,
        claimed.envelope.external_event_id,
        "worker",
        (outbound("new"),),
        run_id="run-30",
    )
    completed = store.get_inbox(Platform.TELEGRAM, claimed.envelope.external_event_id)
    assert completed is not None
    assert completed.status is InboxStatus.COMPLETED
    assert completed.run_id == "run-30"


class MutableClock:
    def __init__(self) -> None:
        self.value = datetime(2026, 1, 1, tzinfo=UTC)

    def __call__(self) -> datetime:
        return self.value

    def advance(self, seconds: int) -> None:
        self.value += timedelta(seconds=seconds)


def test_inbox_lease_recovers_with_same_downstream_key(tmp_path: Path) -> None:
    clock = MutableClock()
    store = ChannelStore(
        tmp_path / "journal.sqlite3",
        authorization_policy=policy(),
        clock=clock,
    )
    store.ingest_telegram_update(message_update(8))
    first = store.claim_inbox("crashed", lease_seconds=5)
    assert first is not None
    clock.advance(6)
    assert store.recover_inbox_leases() == 1
    recovered = store.claim_inbox("replacement")
    assert recovered is not None
    assert recovered.status is InboxStatus.PROCESSING
    assert recovered.envelope.downstream_key == first.envelope.downstream_key


def test_unknown_outbox_requires_operator_retry(tmp_path: Path) -> None:
    clock = MutableClock()
    store = ChannelStore(tmp_path / "journal.sqlite3", clock=clock)
    store.enqueue_outbox(outbound("uncertain"))
    assert store.claim_outbox("crashed", lease_seconds=5) is not None
    clock.advance(6)
    assert store.recover_outbox_leases() == 1
    assert store.claim_outbox("blind-resend") is None
    unknown = store.get_outbox("uncertain")
    assert unknown is not None and unknown.status is OutboxStatus.UNKNOWN
    store.retry_outbox("uncertain", note="remote history checked")
    assert store.claim_outbox("operator") is not None


def test_channel_tables_coexist_with_existing_journal_schema(tmp_path: Path) -> None:
    path = tmp_path / "journal.sqlite3"
    connection = sqlite3.connect(path)
    connection.execute("CREATE TABLE schema_version(version INTEGER PRIMARY KEY, applied_at TEXT)")
    connection.execute("INSERT INTO schema_version VALUES (7, 'existing')")
    connection.commit()
    connection.close()
    store = ChannelStore(path)
    store.close()
    connection = sqlite3.connect(path)
    assert connection.execute("SELECT version FROM schema_version").fetchone() == (7,)
    assert connection.execute(
        "SELECT version FROM channel_schema_version WHERE scope = 'channels'"
    ).fetchone() == (4,)


def test_generic_ingest_is_a_slack_extension_point(tmp_path: Path) -> None:
    store = ChannelStore(
        tmp_path / "journal.sqlite3",
        authorization_policy=AuthorizationPolicy(
            platform_users={Platform.SLACK: ["U1"]},
            platform_channels={Platform.SLACK: ["C1"]},
            allowed_actions=["message"],
        ),
    )
    envelope = ChannelEnvelope(
        platform=Platform.SLACK,
        external_event_id="E1",
        event_type="message",
        user_id="U1",
        channel_id="C1",
        thread_key="slack:C1:T1",
        downstream_key="slack:event:E1",
        text="hello",
    )
    result = store.ingest_envelope(envelope, {"event_id": "E1", "text": "hello"})
    assert result.accepted
    claimed = store.claim_inbox("slack-worker", platform=Platform.SLACK)
    assert claimed is not None
    assert claimed.envelope == envelope
