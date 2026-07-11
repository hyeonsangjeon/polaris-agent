from __future__ import annotations

import asyncio
import time
from collections.abc import Awaitable, Callable, Mapping
from pathlib import Path
from typing import Any, cast

import pytest

from polaris.channels import (
    SLACK_TEXT_LIMIT,
    AuthorizationPolicy,
    MessageOperation,
    OutboundMessage,
    Platform,
    SlackAdapter,
    SlackAPIError,
    SlackConnectionError,
    SlackTransportError,
    normalize_slack_event,
)
from polaris.channels.store import ChannelStore


def policy(*, actions: tuple[str, ...] = ("message", "callback")) -> AuthorizationPolicy:
    return AuthorizationPolicy(
        platform_users={Platform.SLACK: ["U1"]},
        platform_channels={Platform.SLACK: ["C1"]},
        allowed_actions=actions,
    )


def event_request(
    *,
    event_id: str | None = "Ev1",
    envelope_id: str | None = "En1",
    event_type: str = "message",
    text: str = "hello",
    thread_ts: str | None = None,
) -> dict[str, Any]:
    event: dict[str, Any] = {
        "type": event_type,
        "user": "U1",
        "channel": "C1",
        "ts": "100.001",
        "text": text,
    }
    if thread_ts is not None:
        event["thread_ts"] = thread_ts
    payload: dict[str, Any] = {"event": event}
    if event_id is not None:
        payload["event_id"] = event_id
    result: dict[str, Any] = {"type": "events_api", "payload": payload}
    if envelope_id is not None:
        result["envelope_id"] = envelope_id
    return result


class FakeSocket:
    def __init__(self) -> None:
        self.handler: Callable[[object], Awaitable[object]] | None = None
        self.acks: list[tuple[str, Mapping[str, Any] | None]] = []
        self.connected = False
        self.connect_calls = 0
        self.reconnect_calls = 0
        self.close_calls = 0

    def set_request_handler(self, handler: Callable[[object], Awaitable[object]]) -> None:
        self.handler = handler

    async def connect(self) -> None:
        self.connect_calls += 1
        self.connected = True

    async def reconnect(self) -> None:
        self.reconnect_calls += 1
        self.connected = True

    async def close(self) -> None:
        self.close_calls += 1
        self.connected = False

    async def ack(
        self, envelope_id: str, payload: Mapping[str, Any] | None = None
    ) -> None:
        self.acks.append((envelope_id, payload))

    async def is_connected(self) -> bool:
        return self.connected


class FakeWeb:
    def __init__(self) -> None:
        self.calls: list[tuple[str, dict[str, Any]]] = []
        self.responses: list[object] = []

    async def _call(self, method: str, kwargs: dict[str, Any]) -> object:
        self.calls.append((method, kwargs))
        if self.responses:
            response = self.responses.pop(0)
            if isinstance(response, BaseException):
                raise response
            return response
        return {"ok": True, "ts": str(len(self.calls))}

    async def chat_postMessage(self, **kwargs: Any) -> object:
        return await self._call("chat_postMessage", kwargs)

    async def chat_update(self, **kwargs: Any) -> object:
        return await self._call("chat_update", kwargs)

    async def chat_postEphemeral(self, **kwargs: Any) -> object:
        return await self._call("chat_postEphemeral", kwargs)


class FakeAsyncSlackResponse:
    def __init__(
        self,
        data: object,
        *,
        status_code: int = 200,
        headers: Mapping[str, Any] | None = None,
    ) -> None:
        self.data = data
        self.status_code = status_code
        self.headers = dict(headers or {})


def adapter(path: Path, socket: FakeSocket, web: FakeWeb, **kwargs: Any) -> SlackAdapter:
    return SlackAdapter(
        "xoxb-secret",
        "xapp-secret",
        ChannelStore(path, authorization_policy=policy()),
        socket_transport=socket,
        web_client=web,
        **kwargs,
    )


def test_external_event_id_priority_and_thread_normalization() -> None:
    by_event = normalize_slack_event(event_request(), policy())
    assert by_event.envelope.external_event_id == "Ev1"
    assert by_event.envelope.thread_key == "slack:C1:100.001"
    assert by_event.envelope.metadata["thread_ts"] == "100.001"

    by_envelope = normalize_slack_event(event_request(event_id=None), policy())
    assert by_envelope.envelope.external_event_id == "En1"

    by_fallback = normalize_slack_event(
        event_request(event_id=None, envelope_id=None), policy()
    )
    assert by_fallback.envelope.external_event_id == "C1:100.001:message"

    reply = normalize_slack_event(event_request(thread_ts="90.000"), policy())
    assert reply.envelope.thread_key == "slack:C1:90.000"


def test_app_mention_strips_leading_bot_mention_for_shared_commands() -> None:
    normalized = normalize_slack_event(
        event_request(
            event_id="EvMention",
            envelope_id="EnMention",
            event_type="app_mention",
            text="<@UBOT123> /run inspect the deployment",
        ),
        policy(),
    )

    assert normalized.envelope.text == "/run inspect the deployment"


@pytest.mark.asyncio
async def test_ack_happens_only_after_durable_insert_and_db_failure_is_not_acked(
    tmp_path: Path,
) -> None:
    order: list[str] = []
    socket = FakeSocket()
    slack = adapter(tmp_path / "journal.sqlite3", socket, FakeWeb())
    original = slack.store.ingest_envelope

    def ingest(*args: Any, **kwargs: Any) -> object:
        result = original(*args, **kwargs)
        order.append("insert")
        return result

    async def ack(
        envelope_id: str, payload: Mapping[str, Any] | None = None
    ) -> None:
        del envelope_id, payload
        order.append("ack")

    slack.store.ingest_envelope = ingest  # type: ignore[method-assign,assignment]
    socket.ack = ack  # type: ignore[method-assign]
    await slack.handle_socket_request(event_request())
    assert order == ["insert", "ack"]

    def fail(*args: Any, **kwargs: Any) -> object:
        del args, kwargs
        raise OSError("database unavailable")

    slack.store.ingest_envelope = fail  # type: ignore[method-assign,assignment]
    with pytest.raises(OSError):
        await slack.handle_socket_request(event_request(event_id="Ev2", envelope_id="En2"))
    assert order == ["insert", "ack"]
    await slack.close()


@pytest.mark.asyncio
async def test_unhandled_envelope_is_acked_and_minimally_ignored(tmp_path: Path) -> None:
    socket = FakeSocket()
    slack = adapter(tmp_path / "journal.sqlite3", socket, FakeWeb())
    result = await slack.handle_socket_request(
        {"type": "hello", "envelope_id": "unknown-envelope", "payload": {"secret": "omit"}}
    )
    assert result is not None
    assert result.decision.value == "ignored"
    assert socket.acks == [("unknown-envelope", None)]
    assert slack.store.get_inbox(Platform.SLACK, "unknown-envelope") is not None
    assert slack.store.claim_inbox("worker", platform=Platform.SLACK) is None
    await slack.close()


@pytest.mark.asyncio
async def test_ignored_and_malformed_envelopes_are_not_acked_without_durability(
    tmp_path: Path,
) -> None:
    socket = FakeSocket()
    slack = adapter(tmp_path / "journal.sqlite3", socket, FakeWeb())

    def fail(*args: Any, **kwargs: Any) -> object:
        del args, kwargs
        raise OSError("database unavailable")

    slack.store.ingest_envelope = fail  # type: ignore[method-assign,assignment]
    with pytest.raises(OSError):
        await slack.handle_socket_request(
            {"type": "hello", "envelope_id": "ignored-db-failure"}
        )
    assert socket.acks == []

    result = await slack.handle_socket_request({"type": "events_api", "payload": {}})
    assert result is None
    assert socket.acks == []
    await slack.close()

    timeout_socket = FakeSocket()
    timeout_slack = adapter(
        tmp_path / "timeout.sqlite3",
        timeout_socket,
        FakeWeb(),
        ingest_timeout=0.001,
    )

    def timeout(*args: Any, **kwargs: Any) -> object:
        del args, kwargs
        time.sleep(0.02)
        return object()

    timeout_slack.store.ingest_envelope = timeout  # type: ignore[method-assign,assignment]
    with pytest.raises(TimeoutError):
        await timeout_slack.handle_socket_request(
            {"type": "hello", "envelope_id": "ignored-timeout"}
        )
    assert timeout_socket.acks == []
    await timeout_slack.close()


@pytest.mark.asyncio
async def test_thread_reply_payload_update_and_safe_ephemeral(tmp_path: Path) -> None:
    socket = FakeSocket()
    web = FakeWeb()
    slack = adapter(tmp_path / "journal.sqlite3", socket, web)
    receipt = await slack.send(
        OutboundMessage(
            platform=Platform.SLACK,
            idempotency_key="reply",
            channel_id="C1",
            thread_key="slack:C1:90.000",
            text="response",
        )
    )
    assert receipt.remote_message_id == "1"
    assert web.calls[0] == (
        "chat_postMessage",
        {"channel": "C1", "text": "response", "thread_ts": "90.000"},
    )

    await slack.send(
        OutboundMessage(
            platform=Platform.SLACK,
            idempotency_key="update",
            channel_id="C1",
            thread_key="slack:C1:90.000",
            text="approved",
            operation=MessageOperation.EDIT,
            message_id="100.001",
        )
    )
    assert web.calls[1] == (
        "chat_update",
        {"channel": "C1", "text": "approved", "ts": "100.001"},
    )

    await slack.send(
        OutboundMessage(
            platform=Platform.SLACK,
            idempotency_key="feedback",
            channel_id="C1",
            thread_key="slack:C1:90.000",
            text="not authorized",
            operation=MessageOperation.ANSWER_CALLBACK,
            metadata={"user_id": "U1"},
        )
    )
    assert web.calls[2] == (
        "chat_postEphemeral",
        {"channel": "C1", "text": "not authorized", "user": "U1"},
    )
    await slack.close()


class SlackApiError(Exception):
    def __init__(self, response: object) -> None:
        super().__init__("SDK error with xoxb-secret")
        self.response = response


@pytest.mark.asyncio
async def test_rate_limit_chunking_and_block_fallback(tmp_path: Path) -> None:
    sleeps: list[float] = []

    async def sleep(delay: float) -> None:
        sleeps.append(delay)

    socket = FakeSocket()
    web = FakeWeb()
    web.responses = [
        SlackApiError(
            FakeAsyncSlackResponse(
                {"ok": False, "error": "ratelimited"},
                status_code=429,
                headers={"retry-after": "120"},
            )
        ),
        {"ok": True, "ts": "done"},
    ]
    slack = adapter(
        tmp_path / "journal.sqlite3",
        socket,
        web,
        sleep=sleep,
        max_retry_after=5,
    )
    await slack.send(
        OutboundMessage(
            platform=Platform.SLACK,
            idempotency_key="limited",
            channel_id="C1",
            thread_key="slack:C1",
            text="hello",
        )
    )
    assert sleeps == [5]

    parts = slack.prepare_outbound(
        OutboundMessage(
            platform=Platform.SLACK,
            idempotency_key="long",
            channel_id="C1",
            thread_key="slack:C1",
            text="x" * (SLACK_TEXT_LIMIT + 1),
            metadata={"blocks": [{"type": "section", "text": {"text": "ok"}}]},
        )
    )
    assert len(parts) == 2
    assert all(len(part.text) <= SLACK_TEXT_LIMIT for part in parts)
    assert all("blocks" not in part.metadata for part in parts)

    web.responses = [{"ok": True, "ts": "plain"}]
    await slack.send(
        OutboundMessage(
            platform=Platform.SLACK,
            idempotency_key="invalid-block",
            channel_id="C1",
            thread_key="slack:C1",
            text="fallback",
            metadata={
                "blocks": [
                    {"type": "section", "text": {"type": "mrkdwn", "text": "x" * 3001}}
                ]
            },
        )
    )
    assert web.calls[-1][1] == {"channel": "C1", "text": "fallback"}
    await slack.close()


@pytest.mark.asyncio
async def test_sdk_response_normalization_and_delivery_classification(tmp_path: Path) -> None:
    socket = FakeSocket()
    web = FakeWeb()
    web.responses = [FakeAsyncSlackResponse({"ok": True, "ts": "sdk-ts"})]
    slack = adapter(tmp_path / "journal.sqlite3", socket, web)
    receipt = await slack.send(
        OutboundMessage(
            platform=Platform.SLACK,
            idempotency_key="sdk-response",
            channel_id="C1",
            thread_key="slack:C1",
            text="hello",
        )
    )
    assert receipt.remote_message_id == "sdk-ts"

    for response in (
        FakeAsyncSlackResponse({"ok": False, "error": "internal_error"}, status_code=503),
        FakeAsyncSlackResponse(b"not-json"),
        SlackApiError(
            FakeAsyncSlackResponse(
                {"ok": False, "error": "internal_error"}, status_code=503
            )
        ),
        SlackApiError(object()),
        type("MalformedResponse", (), {"data": {"ok": True, "ts": "unknown"}})(),
    ):
        web.responses = [response]
        with pytest.raises(SlackTransportError) as caught:
            await slack.send(
                OutboundMessage(
                    platform=Platform.SLACK,
                    idempotency_key="delivery-unknown",
                    channel_id="C1",
                    thread_key="slack:C1",
                    text="hello",
                )
            )
        assert caught.value.delivery_unknown is True
        assert "secret" not in str(caught.value)

    web.responses = [
        SlackApiError(
            FakeAsyncSlackResponse({"ok": False, "error": "invalid_auth"}, status_code=401)
        )
    ]
    with pytest.raises(SlackAPIError) as rejected:
        await slack.send(
            OutboundMessage(
                platform=Platform.SLACK,
                idempotency_key="rejected",
                channel_id="C1",
                thread_key="slack:C1",
                text="hello",
            )
        )
    assert rejected.value.delivery_unknown is False

    web.responses = [
        FakeAsyncSlackResponse({"ok": False, "error": "invalid_blocks"}, status_code=400)
    ]
    with pytest.raises(SlackAPIError) as direct_rejection:
        await slack.send(
            OutboundMessage(
                platform=Platform.SLACK,
                idempotency_key="direct-rejection",
                channel_id="C1",
                thread_key="slack:C1",
                text="hello",
            )
        )
    assert direct_rejection.value.status_code == 400
    await slack.close()


@pytest.mark.asyncio
async def test_connect_guard_redaction_and_transport_unknown(tmp_path: Path) -> None:
    first_socket = FakeSocket()
    first = adapter(tmp_path / "first.sqlite3", first_socket, FakeWeb())
    second = adapter(tmp_path / "second.sqlite3", FakeSocket(), FakeWeb())
    await first.connect()
    with pytest.raises(SlackConnectionError):
        await second.connect()
    assert "xoxb-secret" not in repr(first)
    assert "xapp-secret" not in repr(first)

    web = FakeWeb()
    web.responses = [OSError("failed xoxb-secret xapp-secret")]
    first._web_client = web
    with pytest.raises(SlackTransportError) as caught:
        await first.send(
            OutboundMessage(
                platform=Platform.SLACK,
                idempotency_key="transport",
                channel_id="C1",
                thread_key="slack:C1",
                text="hello",
            )
        )
    assert caught.value.delivery_unknown is True
    assert "secret" not in str(caught.value)
    await first.close()
    await second.close()


@pytest.mark.asyncio
async def test_owned_sdk_clients_ignore_proxy_environment(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("HTTPS_PROXY", "http://proxy.invalid:9999")
    slack = SlackAdapter(
        "xoxb-secret",
        "xapp-secret",
        ChannelStore(tmp_path / "journal.sqlite3"),
    )
    slack._ensure_sdk()
    transport = slack._socket_transport
    web_client = slack._web_client
    assert transport is not None and web_client is not None
    sdk_transport = cast(Any, transport)
    sdk_web_client = cast(Any, web_client)
    assert sdk_transport.client.proxy is None
    assert sdk_transport.client.aiohttp_client_session.trust_env is False
    assert sdk_web_client.proxy is None
    assert sdk_web_client.trust_env_in_session is False
    assert sdk_web_client.retry_handlers == []

    reconnect_forces: list[bool] = []

    async def connect_to_new_endpoint(*, force: bool = False) -> None:
        reconnect_forces.append(force)

    sdk_transport.client.connect_to_new_endpoint = connect_to_new_endpoint
    await sdk_transport.reconnect()
    assert reconnect_forces == [True]
    await slack.close()


@pytest.mark.asyncio
async def test_watchdog_backoff_and_close_timeout(tmp_path: Path) -> None:
    sleeps: list[float] = []
    socket = FakeSocket()

    async def sleep(delay: float) -> None:
        sleeps.append(delay)
        if delay == 1:
            socket.connected = False
        elif delay == 0.5:
            slack._stop.set()

    async def reconnect() -> None:
        socket.reconnect_calls += 1
        raise OSError("offline")

    socket.reconnect = reconnect  # type: ignore[method-assign]
    slack = adapter(
        tmp_path / "journal.sqlite3",
        socket,
        FakeWeb(),
        watchdog_interval=1,
        sleep=sleep,
        jitter=lambda: 0.5,
        close_timeout=0.01,
    )
    await slack.poll_forever()
    assert sleeps == [1, 0.5]
    assert socket.connect_calls == 1
    assert socket.reconnect_calls == 1

    async def hanging_close() -> None:
        await asyncio.Event().wait()

    socket.close = hanging_close  # type: ignore[method-assign]
    await asyncio.wait_for(slack.close(), timeout=0.1)
