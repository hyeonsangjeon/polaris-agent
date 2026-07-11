from __future__ import annotations

import sqlite3
from collections.abc import Awaitable, Callable, Mapping
from pathlib import Path
from typing import Any

import pytest

from polaris.channels import AuthDecision, AuthorizationPolicy, Platform, SlackAdapter
from polaris.channels.store import ChannelStore


class FakeSocket:
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


def event_request(*, text: str) -> dict[str, Any]:
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
                "text": text,
            },
        },
    }


@pytest.mark.asyncio
async def test_messages_and_block_actions_share_deny_default_without_payload_body(
    tmp_path: Path,
) -> None:
    path = tmp_path / "journal.sqlite3"
    policy = AuthorizationPolicy(
        platform_users={Platform.SLACK: ["U1"]},
        platform_channels={Platform.SLACK: ["C1"]},
        allowed_actions=["message"],
    )
    store = ChannelStore(path, authorization_policy=policy)
    slack = SlackAdapter(
        "xoxb-secret",
        "xapp-secret",
        store,
        socket_transport=FakeSocket(),
        web_client=FakeWeb(),
    )
    denied_message = event_request(text="DO-NOT-PERSIST-MESSAGE")
    denied_message["payload"]["event"]["user"] = "U-other"
    message_result = await slack.handle_socket_request(denied_message)
    assert message_result is not None and message_result.decision is AuthDecision.DENY
    assert message_result.envelope is None

    action: dict[str, Any] = {
        "type": "interactive",
        "envelope_id": "action-envelope",
        "payload": {
            "type": "block_actions",
            "user": {"id": "U1"},
            "channel": {"id": "C1"},
            "message": {"ts": "100.001"},
            "actions": [{"action_id": "approve", "value": "DO-NOT-PERSIST-ACTION"}],
        },
    }
    action_result = await slack.handle_socket_request(action)
    assert action_result is not None and action_result.decision is AuthDecision.DENY
    assert action_result.envelope is None
    await slack.close()
    store.close()

    connection = sqlite3.connect(path)
    rows = connection.execute(
        "SELECT payload_json, envelope_json FROM channel_inbox ORDER BY rowid"
    ).fetchall()
    assert rows == [(None, None), (None, None)]
    content = path.read_bytes()
    assert b"DO-NOT-PERSIST-MESSAGE" not in content
    assert b"DO-NOT-PERSIST-ACTION" not in content
    assert b"xoxb-secret" not in content
    assert b"xapp-secret" not in content
