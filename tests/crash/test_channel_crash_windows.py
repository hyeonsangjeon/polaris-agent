from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from polaris.channels import (
    AuthorizationPolicy,
    ChannelEnvelope,
    ChannelProcessor,
    MessageOperation,
    OutboundMessage,
    OutboxStatus,
    Platform,
    RemoteReceipt,
)
from polaris.channels.store import ChannelStore
from polaris.journal import Journal


class MutableClock:
    def __init__(self) -> None:
        self.value = datetime(2026, 1, 1, tzinfo=UTC)

    def __call__(self) -> datetime:
        return self.value

    def advance(self, seconds: int) -> None:
        self.value += timedelta(seconds=seconds)


class RecordingAdapter:
    def __init__(self) -> None:
        self.sent = 0

    async def connect(self) -> None:
        return None

    async def close(self) -> None:
        return None

    async def send(self, message: OutboundMessage) -> RemoteReceipt:
        self.sent += 1
        return RemoteReceipt(
            platform=message.platform,
            idempotency_key=message.idempotency_key,
            remote_message_id="remote-1",
            channel_id=message.channel_id,
            operation=MessageOperation.SEND,
        )


def update() -> dict[str, object]:
    return {
        "update_id": 1,
        "message": {
            "message_id": 2,
            "from": {"id": 10},
            "chat": {"id": 20},
            "text": "hello",
        },
    }


def store_for(path: Path, clock: MutableClock) -> ChannelStore:
    return ChannelStore(
        path,
        clock=clock,
        authorization_policy=AuthorizationPolicy(
            allowed_user_ids=[10],
            allowed_chat_ids=[20],
            allowed_actions=["message"],
        ),
    )


def test_restart_duplicate_is_processed_once_and_backlog_remains(tmp_path: Path) -> None:
    path = tmp_path / "journal.sqlite3"
    clock = MutableClock()
    first = store_for(path, clock)
    first.ingest_telegram_update(update())
    first.close()
    second = store_for(path, clock)
    assert second.ingest_telegram_update(update()).duplicate
    claimed = second.claim_inbox("worker")
    assert claimed is not None
    second.complete_inbox(
        Platform.TELEGRAM,
        claimed.envelope.external_event_id,
        "worker",
    )
    assert second.claim_inbox("worker") is None


@pytest.mark.asyncio
async def test_crash_after_remote_send_before_commit_becomes_unknown(tmp_path: Path) -> None:
    path = tmp_path / "journal.sqlite3"
    clock = MutableClock()
    store = store_for(path, clock)
    store.enqueue_outbox(
        OutboundMessage(
            platform=Platform.TELEGRAM,
            idempotency_key="send-once",
            channel_id="20",
            thread_key="telegram:20",
            text="response",
        )
    )
    adapter = RecordingAdapter()

    async def unused_handler(_envelope: ChannelEnvelope) -> list[OutboundMessage]:
        return []

    processor = ChannelProcessor(
        store,
        adapter,
        unused_handler,
        owner="worker",
        platform=Platform.TELEGRAM,
        lease_seconds=5,
    )
    original = store.mark_outbox_sent

    def crash_before_commit(
        idempotency_key: str, owner: str, receipt: RemoteReceipt
    ) -> object:
        del idempotency_key, owner, receipt
        raise SystemExit("simulated process death")

    store.mark_outbox_sent = crash_before_commit  # type: ignore[method-assign,assignment]
    with pytest.raises(SystemExit):
        await processor.send_outbox_once()
    store.mark_outbox_sent = original  # type: ignore[method-assign]
    assert adapter.sent == 1
    clock.advance(6)
    assert store.recover_outbox_leases() == 1
    record = store.get_outbox("send-once")
    assert record is not None and record.status is OutboxStatus.UNKNOWN
    assert await processor.send_outbox_once() is False
    assert adapter.sent == 1


@pytest.mark.asyncio
async def test_crash_after_run_creation_before_inbox_complete_reuses_run(
    tmp_path: Path,
) -> None:
    path = tmp_path / "journal.sqlite3"
    clock = MutableClock()
    journal = Journal(path)
    store = store_for(path, clock)
    store.ingest_telegram_update(update())

    async def create_run(envelope: ChannelEnvelope) -> list[OutboundMessage]:
        run = journal.create_run(
            "single",
            {"prompt": envelope.text},
            {"provider": "fake"},
            external_key=envelope.downstream_key,
        )
        store.link_inbox_run(
            envelope.platform,
            envelope.external_event_id,
            envelope.downstream_key,
            run.id,
        )
        return [
            OutboundMessage(
                platform=envelope.platform,
                idempotency_key=f"{envelope.downstream_key}:created",
                channel_id=envelope.channel_id,
                thread_key=envelope.thread_key,
                text=f"Run created: {run.id}",
                metadata={"run_id": run.id},
            )
        ]

    first = ChannelProcessor(
        store,
        RecordingAdapter(),
        create_run,
        owner="first",
        platform=Platform.TELEGRAM,
        lease_seconds=5,
    )
    original_complete = store.complete_inbox_with_outbox

    def crash_before_complete(*_args: object, **_kwargs: object) -> None:
        raise SystemExit("simulated process death")

    store.complete_inbox_with_outbox = crash_before_complete  # type: ignore[method-assign,assignment]
    with pytest.raises(SystemExit, match="simulated process death"):
        await first.process_inbox_once()
    store.complete_inbox_with_outbox = original_complete  # type: ignore[method-assign]
    linked = store.get_inbox(Platform.TELEGRAM, "0:1")
    assert linked is not None and linked.run_id is not None
    assert store.get_outbox("telegram:update:0:1:created") is None
    original_run_id = linked.run_id

    clock.advance(6)
    assert store.recover_inbox_leases() == 1
    replacement = ChannelProcessor(
        store,
        RecordingAdapter(),
        create_run,
        owner="replacement",
        platform=Platform.TELEGRAM,
        lease_seconds=5,
    )
    assert await replacement.process_inbox_once()
    completed = store.get_inbox(Platform.TELEGRAM, "0:1")
    assert completed is not None and completed.run_id == original_run_id
    assert store.get_outbox("telegram:update:0:1:created") is not None
    assert [run.id for run in journal.list_runs()] == [original_run_id]
    journal.close()
