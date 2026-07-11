"""Durable inbox handler and outbox dispatcher."""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator, Awaitable, Callable
from contextlib import asynccontextmanager, suppress
from typing import Protocol, runtime_checkable

from .models import ChannelAdapter, ChannelEnvelope, OutboundMessage, Platform
from .store import ChannelStore

ChannelHandler = Callable[[ChannelEnvelope], Awaitable[list[OutboundMessage]]]


@runtime_checkable
class OutboundPreparer(Protocol):
    def prepare_outbound(self, message: OutboundMessage) -> list[OutboundMessage]: ...


@asynccontextmanager
async def _lease_heartbeat(
    heartbeat: Callable[[], None],
    lease_seconds: float,
) -> AsyncIterator[None]:
    parent = asyncio.current_task()
    if parent is None:
        raise RuntimeError("lease heartbeat requires an asyncio task")
    failure: BaseException | None = None

    async def pulse() -> None:
        nonlocal failure
        try:
            while True:
                await asyncio.sleep(min(10.0, lease_seconds / 3))
                heartbeat()
        except asyncio.CancelledError:
            raise
        except BaseException as exc:
            failure = exc
            parent.cancel()

    task = asyncio.create_task(pulse(), name="channel-lease-heartbeat")
    try:
        yield
    except asyncio.CancelledError:
        if failure is not None:
            raise failure from None
        raise
    finally:
        task.cancel()
        with suppress(asyncio.CancelledError):
            await task
    if failure is not None:
        raise failure


class ChannelProcessor:
    """Run handlers only after durable ingest and send only after durable enqueue."""

    def __init__(
        self,
        store: ChannelStore,
        adapter: ChannelAdapter,
        handler: ChannelHandler,
        *,
        owner: str,
        platform: Platform | str,
        lease_seconds: float = 60,
    ) -> None:
        if not owner:
            raise ValueError("owner must not be empty")
        self.store = store
        self.adapter = adapter
        self.handler = handler
        self.owner = owner
        self.platform = Platform(platform)
        self.lease_seconds = lease_seconds

    async def process_inbox_once(self) -> bool:
        record = self.store.claim_inbox(
            self.owner,
            platform=self.platform,
            lease_seconds=self.lease_seconds,
        )
        if record is None:
            return False
        envelope = record.envelope
        async with _lease_heartbeat(
            lambda: self.store.heartbeat_inbox(
                envelope.platform,
                envelope.external_event_id,
                self.owner,
                lease_seconds=self.lease_seconds,
            ),
            self.lease_seconds,
        ):
            try:
                messages = await self.handler(envelope)
                prepared: list[OutboundMessage] = []
                for message in messages:
                    if message.platform is not self.platform:
                        raise ValueError(
                            "handler returned an outbound message for another platform"
                        )
                    if isinstance(self.adapter, OutboundPreparer):
                        prepared.extend(self.adapter.prepare_outbound(message))
                    else:
                        prepared.append(message)
                run_id_value = prepared[0].metadata.get("run_id") if prepared else None
                run_id = str(run_id_value) if run_id_value is not None else None
                self.store.complete_inbox_with_outbox(
                    envelope.platform,
                    envelope.external_event_id,
                    self.owner,
                    prepared,
                    run_id=run_id,
                )
            except Exception as exc:
                self.store.fail_inbox(
                    envelope.platform,
                    envelope.external_event_id,
                    self.owner,
                    str(exc),
                )
                raise
        return True

    async def send_outbox_once(self) -> bool:
        record = self.store.claim_outbox(
            self.owner,
            platform=self.platform,
            lease_seconds=self.lease_seconds,
        )
        if record is None:
            return False
        async with _lease_heartbeat(
            lambda: self.store.heartbeat_outbox(
                record.message.idempotency_key,
                self.owner,
                lease_seconds=self.lease_seconds,
            ),
            self.lease_seconds,
        ):
            try:
                receipt = await self.adapter.send(record.message)
            except Exception as exc:
                if getattr(exc, "delivery_unknown", True):
                    self.store.mark_outbox_unknown(
                        record.message.idempotency_key, self.owner, str(exc)
                    )
                else:
                    self.store.mark_outbox_failed(
                        record.message.idempotency_key, self.owner, str(exc)
                    )
                raise
            self.store.mark_outbox_sent(record.message.idempotency_key, self.owner, receipt)
        return True

    async def process_once(self) -> bool:
        processed = await self.process_inbox_once()
        sent = await self.send_outbox_once()
        return processed or sent
