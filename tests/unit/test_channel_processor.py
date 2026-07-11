from __future__ import annotations

import asyncio
from pathlib import Path

import pytest

from polaris.channels import (
    AuthorizationPolicy,
    ChannelEnvelope,
    ChannelHandler,
    ChannelProcessor,
    InboxStatus,
    MessageOperation,
    OutboundMessage,
    OutboxStatus,
    Platform,
    RemoteReceipt,
)
from polaris.channels.store import ChannelStore
from polaris.journal import Journal

HEARTBEAT_LEASE_SECONDS = 1.0
HEARTBEAT_OBSERVATION_SECONDS = 2.5


class DurableOrderAdapter:
    def __init__(self, store: ChannelStore) -> None:
        self.store = store
        self.sent: list[str] = []

    async def connect(self) -> None:
        return None

    async def close(self) -> None:
        return None

    async def send(self, message: OutboundMessage) -> RemoteReceipt:
        durable = self.store.get_outbox(message.idempotency_key)
        assert durable is not None and durable.status is OutboxStatus.SENDING
        self.sent.append(message.idempotency_key)
        return RemoteReceipt(
            platform=message.platform,
            idempotency_key=message.idempotency_key,
            remote_message_id="remote",
            channel_id=message.channel_id,
            operation=MessageOperation.SEND,
        )


@pytest.mark.asyncio
async def test_processor_persists_before_handler_and_enqueues_before_send(tmp_path: Path) -> None:
    store = ChannelStore(
        tmp_path / "journal.sqlite3",
        authorization_policy=AuthorizationPolicy(
            allowed_user_ids=[10],
            allowed_chat_ids=[20],
            allowed_actions=["message"],
        ),
    )
    store.ingest_telegram_update(
        {
            "update_id": 1,
            "message": {
                "message_id": 2,
                "from": {"id": 10},
                "chat": {"id": 20},
                "text": "hello",
            },
        }
    )

    async def handler(envelope: ChannelEnvelope) -> list[OutboundMessage]:
        durable = store.get_inbox(envelope.platform, envelope.external_event_id)
        assert durable is not None and durable.status is InboxStatus.PROCESSING
        return [
            OutboundMessage(
                platform=Platform.TELEGRAM,
                idempotency_key=f"{envelope.downstream_key}:reply",
                channel_id=envelope.channel_id,
                thread_key=envelope.thread_key,
                text="response",
                metadata={"run_id": "run-1"},
            )
        ]

    adapter = DurableOrderAdapter(store)
    processor = ChannelProcessor(
        store,
        adapter,
        handler,
        owner="worker",
        platform=Platform.TELEGRAM,
    )
    assert await processor.process_once()
    inbox = store.get_inbox(Platform.TELEGRAM, "0:1")
    assert inbox is not None
    assert inbox.status is InboxStatus.COMPLETED
    assert inbox.run_id == "run-1"
    assert inbox.outbox_key == "telegram:update:0:1:reply"
    outbox = store.get_outbox("telegram:update:0:1:reply")
    assert outbox is not None and outbox.status is OutboxStatus.SENT
    assert adapter.sent == ["telegram:update:0:1:reply"]


@pytest.mark.asyncio
async def test_handler_failure_marks_inbox_failed(tmp_path: Path) -> None:
    store = ChannelStore(
        tmp_path / "journal.sqlite3",
        authorization_policy=AuthorizationPolicy(
            allowed_user_ids=[10],
            allowed_chat_ids=[20],
            allowed_actions=["message"],
        ),
    )
    store.ingest_telegram_update(
        {
            "update_id": 2,
            "message": {
                "message_id": 2,
                "from": {"id": 10},
                "chat": {"id": 20},
                "text": "hello",
            },
        }
    )

    async def failing(_envelope: ChannelEnvelope) -> list[OutboundMessage]:
        raise ValueError("handler failed")

    processor = ChannelProcessor(
        store,
        DurableOrderAdapter(store),
        failing,
        owner="worker",
        platform=Platform.TELEGRAM,
    )
    with pytest.raises(ValueError, match="handler failed"):
        await processor.process_inbox_once()
    record = store.get_inbox(Platform.TELEGRAM, "0:2")
    assert record is not None and record.status is InboxStatus.FAILED


@pytest.mark.asyncio
async def test_inbox_heartbeat_keeps_slow_handler_lease(tmp_path: Path) -> None:
    path = tmp_path / "journal.sqlite3"
    store = ChannelStore(
        path,
        authorization_policy=AuthorizationPolicy(
            allowed_user_ids=[10],
            allowed_chat_ids=[20],
            allowed_actions=["message"],
        ),
    )
    store.ingest_telegram_update(
        {
            "update_id": 3,
            "message": {
                "message_id": 2,
                "from": {"id": 10},
                "chat": {"id": 20},
                "text": "slow",
            },
        }
    )
    started = asyncio.Event()
    release = asyncio.Event()

    async def slow_handler(_envelope: ChannelEnvelope) -> list[OutboundMessage]:
        started.set()
        await release.wait()
        return []

    processor = ChannelProcessor(
        store,
        DurableOrderAdapter(store),
        slow_handler,
        owner="slow-worker",
        platform=Platform.TELEGRAM,
        lease_seconds=HEARTBEAT_LEASE_SECONDS,
    )
    processing = asyncio.create_task(processor.process_inbox_once())
    await started.wait()
    await asyncio.sleep(HEARTBEAT_OBSERVATION_SECONDS)
    contender = ChannelStore(path)
    assert contender.recover_inbox_leases() == 0
    assert contender.claim_inbox("contender", platform=Platform.TELEGRAM) is None
    release.set()
    assert await processing
    contender.close()


@pytest.mark.asyncio
async def test_outbox_heartbeat_keeps_slow_send_lease(tmp_path: Path) -> None:
    path = tmp_path / "journal.sqlite3"
    store = ChannelStore(path)
    message = OutboundMessage(
        platform=Platform.TELEGRAM,
        idempotency_key="slow-send",
        channel_id="20",
        thread_key="telegram:20",
        text="slow",
    )
    store.enqueue_outbox(message)
    started = asyncio.Event()
    release = asyncio.Event()

    class SlowAdapter(DurableOrderAdapter):
        async def send(self, outbound: OutboundMessage) -> RemoteReceipt:
            durable = self.store.get_outbox(outbound.idempotency_key)
            assert durable is not None and durable.status is OutboxStatus.SENDING
            started.set()
            await release.wait()
            return await super().send(outbound)

    processor = ChannelProcessor(
        store,
        SlowAdapter(store),
        lambda _envelope: pytest.fail("inbox handler must not run"),
        owner="slow-worker",
        platform=Platform.TELEGRAM,
        lease_seconds=HEARTBEAT_LEASE_SECONDS,
    )
    sending = asyncio.create_task(processor.send_outbox_once())
    await started.wait()
    await asyncio.sleep(HEARTBEAT_OBSERVATION_SECONDS)
    contender = ChannelStore(path)
    assert contender.recover_outbox_leases() == 0
    assert contender.claim_outbox("contender", platform=Platform.TELEGRAM) is None
    release.set()
    assert await sending
    record = contender.get_outbox("slow-send")
    assert record is not None and record.status is OutboxStatus.SENT
    contender.close()


@pytest.mark.asyncio
async def test_two_processors_share_one_external_run_key(tmp_path: Path) -> None:
    policy = AuthorizationPolicy(
        allowed_user_ids=[10],
        allowed_chat_ids=[20],
        allowed_actions=["message"],
    )
    stores = (
        ChannelStore(tmp_path / "first.sqlite3", authorization_policy=policy),
        ChannelStore(tmp_path / "second.sqlite3", authorization_policy=policy),
    )
    journals = (
        Journal(tmp_path / "runs.sqlite3"),
        Journal(tmp_path / "runs.sqlite3"),
    )
    for store in stores:
        store.ingest_telegram_update(
            {
                "update_id": 4,
                "message": {
                    "message_id": 2,
                    "from": {"id": 10},
                    "chat": {"id": 20},
                    "text": "same",
                },
            }
        )
    run_ids: list[str] = []

    def handler(store: ChannelStore, journal: Journal) -> ChannelHandler:
        async def create(envelope: ChannelEnvelope) -> list[OutboundMessage]:
            run = await asyncio.to_thread(
                journal.create_run,
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
            run_ids.append(run.id)
            return []

        return create

    processors = tuple(
        ChannelProcessor(
            store,
            DurableOrderAdapter(store),
            handler(store, journal),
            owner=f"worker-{index}",
            platform=Platform.TELEGRAM,
        )
        for index, (store, journal) in enumerate(zip(stores, journals, strict=True))
    )
    assert all(await asyncio.gather(*(item.process_inbox_once() for item in processors)))
    assert len(set(run_ids)) == 1
    assert len(journals[0].list_runs()) == 1
    for journal in journals:
        journal.close()
    for store in stores:
        store.close()
