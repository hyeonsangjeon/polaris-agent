from __future__ import annotations

import json
import logging
from collections.abc import Mapping
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, cast

import httpx
import pytest

from polaris.channels import (
    AuthorizationPolicy,
    OutboundMessage,
    ParseMode,
    Platform,
    TelegramAPIError,
    utf16_units,
)
from polaris.channels.store import ChannelStore
from polaris.channels.telegram import (
    TelegramAdapter,
    TelegramConflictError,
    TelegramOffsetState,
    TelegramTransportError,
)


def authorized_store(path: Path) -> ChannelStore:
    return ChannelStore(
        path,
        authorization_policy=AuthorizationPolicy(
            allowed_user_ids=[10],
            allowed_chat_ids=[20],
            allowed_actions=["message", "command", "callback"],
        ),
    )


def body(request: httpx.Request) -> dict[str, Any]:
    value = json.loads(request.content)
    assert isinstance(value, dict)
    return value


@pytest.mark.asyncio
async def test_connect_disables_webhook_without_dropping_and_poll_uses_offset(
    tmp_path: Path,
) -> None:
    requests: list[tuple[str, dict[str, Any]]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        method = request.url.path.rsplit("/", 1)[-1]
        payload = body(request)
        requests.append((method, payload))
        if method == "getMe":
            result: object = {"id": 7, "username": "bot"}
        elif method == "deleteWebhook":
            result = True
        else:
            result = []
        return httpx.Response(200, json={"ok": True, "result": result})

    store = authorized_store(tmp_path / "journal.sqlite3")
    store.set_telegram_offset(42)
    adapter = TelegramAdapter("123:secret", store, transport=httpx.MockTransport(handler))
    await adapter.connect()
    await adapter.poll_once()
    await adapter.close()

    assert ("deleteWebhook", {"drop_pending_updates": False}) in requests
    get_updates = next(payload for method, payload in requests if method == "getUpdates")
    assert get_updates["offset"] == 42
    assert get_updates["allowed_updates"] == ["message", "callback_query"]


@pytest.mark.asyncio
async def test_connect_starts_new_epoch_when_get_me_identity_changes(tmp_path: Path) -> None:
    path = tmp_path / "identity.sqlite3"
    first_store = authorized_store(path)
    first_store.set_telegram_offset(10_000)

    def first_handler(request: httpx.Request) -> httpx.Response:
        method = request.url.path.rsplit("/", 1)[-1]
        result: object = {"id": 1, "username": "first"} if method == "getMe" else True
        return httpx.Response(200, json={"ok": True, "result": result})

    first = TelegramAdapter(
        "1:first",
        first_store,
        transport=httpx.MockTransport(first_handler),
    )
    await first.connect()
    await first.close()
    first_store.close()

    requests: list[dict[str, Any]] = []

    def second_handler(request: httpx.Request) -> httpx.Response:
        method = request.url.path.rsplit("/", 1)[-1]
        if method == "getMe":
            result: object = {"id": 2, "username": "second"}
        elif method == "deleteWebhook":
            result = True
        else:
            requests.append(body(request))
            result = [
                {
                    "update_id": 5,
                    "message": {
                        "message_id": 5,
                        "from": {"id": 10},
                        "chat": {"id": 20},
                        "text": "new bot",
                    },
                }
            ]
        return httpx.Response(200, json={"ok": True, "result": result})

    second_store = authorized_store(path)
    second = TelegramAdapter(
        "2:second",
        second_store,
        transport=httpx.MockTransport(second_handler),
    )
    await second.connect()
    results = await second.poll_once()

    assert requests[0]["offset"] == 0
    assert results[0].envelope is not None
    assert results[0].envelope.external_event_id == "1:5"
    state = second_store.get_telegram_offset_state("default")
    assert state.offset == 6
    assert state.epoch == 1
    assert state.bot_id == "2"
    await second.close()


@pytest.mark.asyncio
async def test_polling_ingests_every_update_and_advances_after_each(tmp_path: Path) -> None:
    updates = [
        {
            "update_id": update_id,
            "message": {
                "message_id": update_id,
                "from": {"id": 10},
                "chat": {"id": 20},
                "text": f"message-{update_id}",
            },
        }
        for update_id in (50, 51)
    ]

    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"ok": True, "result": updates})

    store = authorized_store(tmp_path / "journal.sqlite3")
    adapter = TelegramAdapter("123:secret", store, transport=httpx.MockTransport(handler))
    results = await adapter.poll_once()
    assert [result.accepted for result in results] == [True, True]
    assert store.get_telegram_offset() == 52
    await adapter.close()


@pytest.mark.asyncio
async def test_send_retries_429_retry_after_and_chunks_utf16(tmp_path: Path) -> None:
    sent_payloads: list[dict[str, Any]] = []
    sleeps: list[float] = []
    calls = 0

    async def sleep(delay: float) -> None:
        sleeps.append(delay)

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        if calls == 1:
            return httpx.Response(
                429,
                json={
                    "ok": False,
                    "error_code": 429,
                    "description": "Too Many Requests",
                    "parameters": {"retry_after": 2},
                },
            )
        payload = body(request)
        sent_payloads.append(payload)
        return httpx.Response(
            200,
            json={"ok": True, "result": {"message_id": calls}},
        )

    store = ChannelStore(tmp_path / "journal.sqlite3")
    adapter = TelegramAdapter(
        "123:secret",
        store,
        transport=httpx.MockTransport(handler),
        sleep=sleep,
    )
    receipt = await adapter.send(
        OutboundMessage(
            platform=Platform.TELEGRAM,
            idempotency_key="reply",
            channel_id="20",
            thread_key="telegram:20",
            text=("x" * 4095) + "😀tail",
        )
    )
    assert sleeps == [2]
    assert len(receipt.remote_message_ids) == 2
    assert all(utf16_units(str(payload["text"])) <= 4096 for payload in sent_payloads)
    await adapter.close()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("status_code", "content"),
    [
        (503, b'{"ok": false, "description": "temporary"}'),
        (200, b"not-json"),
        (200, b'{"result": {"message_id": 1}}'),
        (200, b'{"ok": true, "result": true}'),
        (200, b'{"ok": false, "error_code": 500}'),
    ],
)
async def test_send_5xx_and_malformed_responses_are_delivery_unknown(
    tmp_path: Path,
    status_code: int,
    content: bytes,
) -> None:
    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(status_code, content=content)

    adapter = TelegramAdapter(
        "123:secret",
        ChannelStore(tmp_path / f"unknown-{status_code}-{len(content)}.sqlite3"),
        transport=httpx.MockTransport(handler),
    )
    with pytest.raises(TelegramTransportError) as caught:
        await adapter.send(
            OutboundMessage(
                platform=Platform.TELEGRAM,
                idempotency_key="unknown",
                channel_id="20",
                thread_key="telegram:20",
                text="hello",
            )
        )
    assert caught.value.delivery_unknown is True
    await adapter.close()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("status_code", "content"),
    [
        (400, b"not-json"),
        (
            200,
            b'{"ok": false, "error_code": 400, "description": "definitive rejection"}',
        ),
    ],
)
async def test_send_4xx_is_authoritative_rejection(
    tmp_path: Path,
    status_code: int,
    content: bytes,
) -> None:
    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(status_code, content=content)

    adapter = TelegramAdapter(
        "123:secret",
        ChannelStore(tmp_path / f"rejected-{status_code}.sqlite3"),
        transport=httpx.MockTransport(handler),
    )
    with pytest.raises(TelegramAPIError) as caught:
        await adapter.send(
            OutboundMessage(
                platform=Platform.TELEGRAM,
                idempotency_key="rejected",
                channel_id="20",
                thread_key="telegram:20",
                text="hello",
            )
        )
    assert caught.value.delivery_unknown is False
    assert caught.value.error_code == 400 or caught.value.status_code == 400
    await adapter.close()


@pytest.mark.asyncio
async def test_html_is_safe_by_default(tmp_path: Path) -> None:
    payloads: list[dict[str, Any]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        payloads.append(body(request))
        return httpx.Response(200, json={"ok": True, "result": {"message_id": 1}})

    adapter = TelegramAdapter(
        "123:secret",
        ChannelStore(tmp_path / "journal.sqlite3"),
        transport=httpx.MockTransport(handler),
    )
    await adapter.send(
        OutboundMessage(
            platform=Platform.TELEGRAM,
            idempotency_key="html",
            channel_id="20",
            thread_key="telegram:20",
            text="<b>untrusted</b>",
            parse_mode=ParseMode.HTML,
        )
    )
    assert payloads[0]["text"] == "&lt;b&gt;untrusted&lt;/b&gt;"
    assert payloads[0]["parse_mode"] == "HTML"
    await adapter.close()


@pytest.mark.asyncio
async def test_conflicts_are_bounded_and_reconnect_uses_backoff(tmp_path: Path) -> None:
    sleeps: list[float] = []
    calls = 0

    async def sleep(delay: float) -> None:
        sleeps.append(delay)

    def handler(_request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        if calls == 1:
            return httpx.Response(500, json={"ok": False, "description": "temporary"})
        return httpx.Response(
            409,
            json={"ok": False, "error_code": 409, "description": "other poller"},
        )

    adapter = TelegramAdapter(
        "123:secret",
        ChannelStore(tmp_path / "journal.sqlite3"),
        transport=httpx.MockTransport(handler),
        sleep=sleep,
        jitter=lambda: 0.5,
        max_conflicts=2,
    )
    with pytest.raises(TelegramConflictError):
        await adapter.poll_forever()
    assert sleeps == [0.5, 1.0]
    await adapter.close()


@pytest.mark.asyncio
async def test_token_is_redacted_from_repr_and_transport_errors(tmp_path: Path) -> None:
    token = "12345:SUPER_SECRET"

    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError(f"cannot reach {request.url}", request=request)

    adapter = TelegramAdapter(
        token,
        ChannelStore(tmp_path / "journal.sqlite3"),
        transport=httpx.MockTransport(handler),
    )
    assert token not in repr(adapter)
    with pytest.raises(TelegramTransportError) as caught:
        await adapter.poll_once()
    assert token not in str(caught.value)
    await adapter.close()


@pytest.mark.asyncio
async def test_httpx_request_logs_redact_token_and_filter_is_removed(
    tmp_path: Path,
    caplog: pytest.LogCaptureFixture,
) -> None:
    token = "12345:SUPER_SECRET"

    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"ok": True, "result": {"message_id": 1}})

    logger = logging.getLogger("httpx")
    filters_before = tuple(logger.filters)
    caplog.set_level(logging.INFO, logger="httpx")
    adapter = TelegramAdapter(
        token,
        ChannelStore(tmp_path / "logs.sqlite3"),
        transport=httpx.MockTransport(handler),
    )
    await adapter.send(
        OutboundMessage(
            platform=Platform.TELEGRAM,
            idempotency_key="logged",
            channel_id="20",
            thread_key="telegram:20",
            text="hello",
        )
    )
    messages = [record.getMessage() for record in caplog.records if record.name == "httpx"]
    assert messages
    assert all(token not in message for message in messages)
    assert any("bot[REDACTED]" in message for message in messages)
    await adapter.close()
    assert tuple(logger.filters) == filters_before


@pytest.mark.asyncio
async def test_poll_omits_stale_offset_for_update_id_epoch_probe(tmp_path: Path) -> None:
    now = datetime(2026, 7, 11)
    requests: list[dict[str, Any]] = []
    calls = 0
    delegate = authorized_store(tmp_path / "epoch.sqlite3")
    delegate.set_telegram_offset(10_000)

    class EpochAwareStore:
        authorization_policy = delegate.authorization_policy
        telegram_stream_key = delegate.telegram_stream_key

        def get_telegram_offset(self, stream_key: str | None = None) -> int | None:
            return delegate.get_telegram_offset(stream_key)

        def get_telegram_offset_state(self, stream_key: str) -> TelegramOffsetState:
            return TelegramOffsetState(
                offset=delegate.get_telegram_offset(stream_key),
                last_activity_at=now - timedelta(days=7),
            )

        def begin_telegram_epoch(self, next_offset: int, stream_key: str) -> int:
            return delegate.begin_telegram_epoch(next_offset, stream_key)

        def ingest_telegram_update(
            self,
            update: Mapping[str, Any],
            policy: AuthorizationPolicy | None = None,
            *,
            stream_key: str | None = None,
        ) -> Any:
            return delegate.ingest_telegram_update(update, policy, stream_key=stream_key)

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        requests.append(body(request))
        updates: list[dict[str, Any]] = []
        if calls == 1:
            updates.append(
                {
                    "update_id": 5,
                    "message": {
                        "message_id": 5,
                        "from": {"id": 10},
                        "chat": {"id": 20},
                        "text": "new epoch",
                    },
                }
            )
        return httpx.Response(200, json={"ok": True, "result": updates})

    adapter = TelegramAdapter(
        "123:secret",
        cast(Any, EpochAwareStore()),
        transport=httpx.MockTransport(handler),
        clock=lambda: now,
    )
    await adapter.poll_once()
    await adapter.poll_once()
    assert "offset" not in requests[0]
    assert requests[1]["offset"] == 6
    assert delegate.get_telegram_offset() == 6
    await adapter.close()
    delegate.close()

    reopened = authorized_store(tmp_path / "epoch.sqlite3")
    assert reopened.get_telegram_offset() == 6
    assert reopened.get_telegram_offset_state("default").last_activity_at is not None
    reopened.close()


@pytest.mark.asyncio
async def test_long_running_adapter_probes_without_offset_after_inactivity(
    tmp_path: Path,
) -> None:
    now = datetime(2026, 7, 11)
    clock_values = iter((now, now + timedelta(days=7)))
    requests: list[dict[str, Any]] = []
    store = ChannelStore(
        tmp_path / "local-epoch.sqlite3",
        authorization_policy=AuthorizationPolicy(
            allowed_user_ids=[10],
            allowed_chat_ids=[20],
            allowed_actions=["message", "command", "callback"],
        ),
        clock=lambda: now - timedelta(days=7),
    )
    store.set_telegram_offset(99)

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(body(request))
        return httpx.Response(200, json={"ok": True, "result": []})

    adapter = TelegramAdapter(
        "123:secret",
        store,
        transport=httpx.MockTransport(handler),
        clock=lambda: next(clock_values),
    )
    await adapter.poll_once()
    assert "offset" not in requests[0]
    await adapter.close()


@pytest.mark.asyncio
async def test_owned_http_client_ignores_proxy_environment(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("HTTPS_PROXY", "http://proxy.invalid:9999")
    adapter = TelegramAdapter("123:secret", ChannelStore(tmp_path / "journal.sqlite3"))
    assert adapter._client._trust_env is False
    await adapter.close()
