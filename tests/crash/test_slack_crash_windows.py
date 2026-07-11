from __future__ import annotations

from collections.abc import Awaitable, Callable, Mapping
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest

from polaris.channels import (
    AuthorizationPolicy,
    ChannelEnvelope,
    ChannelProcessor,
    OutboundMessage,
    OutboxStatus,
    Platform,
    SlackAdapter,
)
from polaris.channels.store import ChannelStore


class FakeSocket:
    def __init__(self) -> None:
        self.acks: list[tuple[str, Mapping[str, Any] | None]] = []

    def set_request_handler(self, handler: Callable[[object], Awaitable[object]]) -> None:
        self.handler = handler

    async def connect(self) -> None:
        return None

    async def reconnect(self) -> None:
        return None

    async def close(self) -> None:
        return None

    async def ack(
        self, envelope_id: str, payload: Mapping[str, Any] | None = None
    ) -> None:
        self.acks.append((envelope_id, payload))

    async def is_connected(self) -> bool:
        return True


class FakeWeb:
    def __init__(self) -> None:
        self.calls: list[dict[str, Any]] = []

    async def chat_postMessage(self, **kwargs: Any) -> Mapping[str, Any]:
        self.calls.append(kwargs)
        return {"ok": True, "ts": "sent"}

    async def chat_update(self, **kwargs: Any) -> Mapping[str, Any]:
        self.calls.append(kwargs)
        return {"ok": True, "ts": "sent"}

    async def chat_postEphemeral(self, **kwargs: Any) -> Mapping[str, Any]:
        self.calls.append(kwargs)
        return {"ok": True}


def event_request() -> dict[str, Any]:
    return {
        "type": "events_api",
        "envelope_id": "En1",
        "payload": {
            "event_id": "Ev1",
            "event": {
                "type": "message",
                "user": "U1",
                "channel": "C1",
                "ts": "100.001",
                "text": "hello",
            },
        },
    }


class Clock:
    def __init__(self) -> None:
        self.value = datetime(2026, 1, 1, tzinfo=UTC)

    def __call__(self) -> datetime:
        return self.value

    def advance(self, seconds: int) -> None:
        self.value += timedelta(seconds=seconds)


def policy() -> AuthorizationPolicy:
    return AuthorizationPolicy(
        platform_users={Platform.SLACK: ["U1"]},
        platform_channels={Platform.SLACK: ["C1"]},
        allowed_actions=["message"],
    )


@pytest.mark.asyncio
async def test_crash_after_ack_reclaims_inbox_exactly_once(tmp_path: Path) -> None:
    clock = Clock()
    store = ChannelStore(
        tmp_path / "journal.sqlite3", authorization_policy=policy(), clock=clock
    )
    socket = FakeSocket()
    slack = SlackAdapter(
        "xoxb-test",
        "xapp-test",
        store,
        socket_transport=socket,
        web_client=FakeWeb(),
    )
    await slack.handle_socket_request(event_request())
    assert socket.acks == [("En1", None)]
    first = store.claim_inbox("crashed", platform=Platform.SLACK, lease_seconds=5)
    assert first is not None
    clock.advance(6)
    assert store.recover_inbox_leases() == 1
    replacement = store.claim_inbox("replacement", platform=Platform.SLACK)
    assert replacement is not None
    assert replacement.envelope.downstream_key == first.envelope.downstream_key
    store.complete_inbox(Platform.SLACK, "Ev1", "replacement")
    assert store.claim_inbox("again", platform=Platform.SLACK) is None
    await slack.close()


@pytest.mark.asyncio
async def test_crash_while_outbox_sending_becomes_unknown_without_resend(
    tmp_path: Path,
) -> None:
    clock = Clock()
    store = ChannelStore(tmp_path / "journal.sqlite3", clock=clock)
    store.enqueue_outbox(
        OutboundMessage(
            platform=Platform.SLACK,
            idempotency_key="reply",
            channel_id="C1",
            thread_key="slack:C1:100.001",
            text="hello",
        )
    )
    web = FakeWeb()
    slack = SlackAdapter(
        "xoxb-test",
        "xapp-test",
        store,
        socket_transport=FakeSocket(),
        web_client=web,
    )

    async def handler(_envelope: ChannelEnvelope) -> list[OutboundMessage]:
        return []

    processor = ChannelProcessor(
        store,
        slack,
        handler,
        owner="worker",
        platform=Platform.SLACK,
        lease_seconds=5,
    )
    original = store.mark_outbox_sent

    def crash(*_args: object, **_kwargs: object) -> object:
        raise SystemExit("crash before receipt commit")

    store.mark_outbox_sent = crash  # type: ignore[method-assign,assignment]
    with pytest.raises(SystemExit):
        await processor.send_outbox_once()
    store.mark_outbox_sent = original  # type: ignore[method-assign]
    clock.advance(6)
    assert store.recover_outbox_leases() == 1
    record = store.get_outbox("reply")
    assert record is not None and record.status is OutboxStatus.UNKNOWN
    assert await processor.send_outbox_once() is False
    assert len(web.calls) == 1
    await slack.close()
