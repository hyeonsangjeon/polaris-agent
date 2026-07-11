from __future__ import annotations

from collections.abc import Awaitable, Callable, Mapping
from pathlib import Path
from typing import Any

import pytest

from polaris.channels import AuthorizationPolicy, Platform, SlackAdapter
from polaris.channels.store import ChannelStore


class FakeSocket:
    def __init__(self) -> None:
        self.handler: Callable[[object], Awaitable[object]] | None = None

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
        del envelope_id, payload

    async def is_connected(self) -> bool:
        return True


class FakeWeb:
    async def chat_postMessage(self, **kwargs: Any) -> Mapping[str, Any]:
        del kwargs
        return {"ok": True}

    async def chat_update(self, **kwargs: Any) -> Mapping[str, Any]:
        del kwargs
        return {"ok": True}

    async def chat_postEphemeral(self, **kwargs: Any) -> Mapping[str, Any]:
        del kwargs
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


def store(path: Path) -> ChannelStore:
    return ChannelStore(
        path,
        authorization_policy=AuthorizationPolicy(
            platform_users={Platform.SLACK: ["U1"]},
            platform_channels={Platform.SLACK: ["C1"]},
            allowed_actions=["message", "callback"],
        ),
    )


@pytest.mark.asyncio
async def test_duplicate_is_durable_across_store_reopen(tmp_path: Path) -> None:
    path = tmp_path / "journal.sqlite3"
    first_store = store(path)
    first = SlackAdapter(
        "xoxb-test",
        "xapp-test",
        first_store,
        socket_transport=FakeSocket(),
        web_client=FakeWeb(),
    )
    assert (await first.handle_socket_request(event_request())).accepted  # type: ignore[union-attr]
    await first.close()
    first_store.close()

    second_store = store(path)
    second = SlackAdapter(
        "xoxb-test",
        "xapp-test",
        second_store,
        socket_transport=FakeSocket(),
        web_client=FakeWeb(),
    )
    duplicate = await second.handle_socket_request(event_request())
    assert duplicate is not None and duplicate.duplicate
    claimed = second_store.claim_inbox("worker", platform=Platform.SLACK)
    assert claimed is not None
    second_store.complete_inbox(Platform.SLACK, "Ev1", "worker")
    assert second_store.claim_inbox("worker", platform=Platform.SLACK) is None
    await second.close()
