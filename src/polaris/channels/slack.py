"""Single-workspace Slack Socket Mode adapter backed by the durable channel store."""

from __future__ import annotations

import asyncio
import hashlib
import logging
import random
import re
from collections.abc import Awaitable, Callable, Mapping, Sequence
from contextlib import suppress
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any, Protocol, cast, runtime_checkable
from urllib.parse import quote

from .auth import AuthorizationPolicy
from .formatting import chunk_outbound
from .models import (
    AuthDecision,
    ChannelEnvelope,
    IngestResult,
    MessageOperation,
    OutboundMessage,
    Platform,
    RemoteReceipt,
)
from .store import ChannelStore

SLACK_TEXT_LIMIT = 38_999
SLACK_BLOCK_LIMIT = 50
SLACK_SECTION_TEXT_LIMIT = 3_000
SLACK_HEADER_TEXT_LIMIT = 150
_LEADING_APP_MENTION = re.compile(r"^\s*<@[A-Z0-9]+>\s*", re.IGNORECASE)

SocketRequestHandler = Callable[[object], Awaitable[IngestResult | None]]


@runtime_checkable
class SocketTransport(Protocol):
    """Injectable Socket Mode transport; production uses slack-sdk's aiohttp client."""

    def set_request_handler(self, handler: SocketRequestHandler) -> None: ...

    async def connect(self) -> None: ...

    async def reconnect(self) -> None: ...

    async def close(self) -> None: ...

    async def ack(self, envelope_id: str, payload: Mapping[str, Any] | None = None) -> None: ...

    async def is_connected(self) -> bool: ...


@runtime_checkable
class SlackWebClient(Protocol):
    """Small AsyncWebClient surface used by outbound delivery."""

    async def chat_postMessage(self, **kwargs: Any) -> object: ...

    async def chat_update(self, **kwargs: Any) -> object: ...

    async def chat_postEphemeral(self, **kwargs: Any) -> object: ...


class SlackError(RuntimeError):
    delivery_unknown = False


class SlackTransportError(SlackError):
    delivery_unknown = True


class SlackAPIError(SlackError):
    def __init__(
        self,
        message: str,
        *,
        status_code: int | None = None,
        retry_after: float | None = None,
    ) -> None:
        super().__init__(message)
        self.status_code = status_code
        self.retry_after = retry_after


class SlackConnectionError(SlackTransportError):
    pass


@dataclass(frozen=True, slots=True)
class NormalizedSlackEvent:
    envelope: ChannelEnvelope
    decision: AuthDecision
    reason: str
    envelope_id: str | None
    ignored: bool = False


class _RedactingLogger:
    def __init__(self, logger: logging.Logger, redact: Callable[[object], str]) -> None:
        self._logger = logger
        self._redact = redact

    @property
    def level(self) -> int:
        return self._logger.level

    def _log(self, method: str, message: object, *args: object, **kwargs: object) -> None:
        kwargs.pop("exc_info", None)
        safe_args = tuple(self._redact(value) for value in args)
        getattr(self._logger, method)(self._redact(message), *safe_args, **kwargs)

    def debug(self, message: object, *args: object, **kwargs: object) -> None:
        self._log("debug", message, *args, **kwargs)

    def info(self, message: object, *args: object, **kwargs: object) -> None:
        self._log("info", message, *args, **kwargs)

    def warning(self, message: object, *args: object, **kwargs: object) -> None:
        self._log("warning", message, *args, **kwargs)

    warn = warning

    def error(self, message: object, *args: object, **kwargs: object) -> None:
        self._log("error", message, *args, **kwargs)

    def exception(self, message: object, *args: object, **kwargs: object) -> None:
        self._log("error", message, *args, **kwargs)


class _SlackSDKSocketTransport:
    def __init__(self, client: Any) -> None:
        self.client = client
        self._handler: SocketRequestHandler | None = None

        async def listener(_client: object, request: object) -> None:
            if self._handler is not None:
                await self._handler(request)

        client.socket_mode_request_listeners.append(listener)

    def set_request_handler(self, handler: SocketRequestHandler) -> None:
        self._handler = handler

    async def connect(self) -> None:
        await self.client.connect()

    async def reconnect(self) -> None:
        await self.client.connect_to_new_endpoint(force=True)

    async def close(self) -> None:
        await self.client.close()

    async def ack(self, envelope_id: str, payload: Mapping[str, Any] | None = None) -> None:
        body: dict[str, Any] = {"envelope_id": envelope_id}
        if payload is not None:
            body["payload"] = dict(payload)
        await self.client.send_socket_mode_response(body)

    async def is_connected(self) -> bool:
        return bool(await self.client.is_connected())


def _as_mapping(value: object) -> Mapping[str, Any] | None:
    return value if isinstance(value, Mapping) else None


def _usable_envelope_id(value: object) -> str | None:
    if not isinstance(value, str):
        return None
    value = value.strip()
    return value or None


def _request_parts(request: object) -> tuple[str | None, str, Mapping[str, Any]]:
    if isinstance(request, Mapping):
        envelope_value = request.get("envelope_id")
        request_type = str(request.get("type") or "unsupported")
        payload = _as_mapping(request.get("payload")) or {}
    else:
        envelope_value = getattr(request, "envelope_id", None)
        request_type = str(getattr(request, "type", None) or "unsupported")
        payload = _as_mapping(getattr(request, "payload", None)) or {}
    envelope_id = _usable_envelope_id(envelope_value)
    return envelope_id, request_type, payload


def _event_identity(
    payload: Mapping[str, Any],
    envelope_id: str | None,
    *,
    channel_id: str,
    timestamp: str,
    event_type: str,
) -> str:
    event_id = payload.get("event_id")
    if event_id:
        return str(event_id)
    if envelope_id:
        return envelope_id
    if channel_id and timestamp:
        return f"{channel_id}:{timestamp}:{event_type}"
    raise ValueError("Slack event has no event_id, envelope_id, or channel/timestamp fallback")


def _ignored_envelope(
    *,
    external_event_id: str,
    event_type: str,
    user_id: str = "",
    channel_id: str = "",
) -> ChannelEnvelope:
    return ChannelEnvelope(
        platform=Platform.SLACK,
        external_event_id=external_event_id,
        event_type=event_type,
        user_id=user_id,
        channel_id=channel_id,
        thread_key="",
        downstream_key=f"slack:event:{external_event_id}",
        action="ignored",
        received_at=datetime.now(UTC).isoformat(timespec="seconds").replace("+00:00", "Z"),
        metadata={},
    )


def normalize_slack_event(
    request: object,
    policy: AuthorizationPolicy,
    *,
    top_level_as_thread_root: bool = True,
) -> NormalizedSlackEvent:
    """Normalize Slack messages and approval block actions using one authorization policy."""
    envelope_id, request_type, payload = _request_parts(request)
    event = _as_mapping(payload.get("event"))
    if request_type == "events_api" or event is not None:
        event = event or {}
        event_type = str(event.get("type") or "unsupported")
        user_id = str(event.get("user") or "")
        channel_id = str(event.get("channel") or "")
        timestamp = str(event.get("ts") or event.get("event_ts") or "")
        external_id = _event_identity(
            payload,
            envelope_id,
            channel_id=channel_id,
            timestamp=timestamp,
            event_type=event_type,
        )
        if event_type not in {"message", "app_mention"}:
            return NormalizedSlackEvent(
                _ignored_envelope(
                    external_event_id=external_id,
                    event_type=event_type,
                    user_id=user_id,
                    channel_id=channel_id,
                ),
                AuthDecision.IGNORED,
                "event type is not supported",
                envelope_id,
                True,
            )
        if event.get("bot_id") or event.get("subtype"):
            return NormalizedSlackEvent(
                _ignored_envelope(
                    external_event_id=external_id,
                    event_type=event_type,
                    user_id=user_id,
                    channel_id=channel_id,
                ),
                AuthDecision.IGNORED,
                "bot and subtype messages are ignored",
                envelope_id,
                True,
            )
        text = event.get("text")
        if not user_id or not channel_id or not timestamp or not isinstance(text, str):
            return NormalizedSlackEvent(
                _ignored_envelope(
                    external_event_id=external_id,
                    event_type=event_type,
                    user_id=user_id,
                    channel_id=channel_id,
                ),
                AuthDecision.IGNORED,
                "message identity, timestamp, or text is missing",
                envelope_id,
                True,
            )
        if event_type == "app_mention":
            text = _LEADING_APP_MENTION.sub("", text, count=1)
        supplied_thread = event.get("thread_ts")
        thread_ts = (
            str(supplied_thread)
            if supplied_thread
            else (timestamp if top_level_as_thread_root else None)
        )
        action = "message"
        auth = policy.evaluate(Platform.SLACK, user_id, channel_id, action)
        envelope = ChannelEnvelope(
            platform=Platform.SLACK,
            external_event_id=external_id,
            event_type=event_type,
            user_id=user_id,
            channel_id=channel_id,
            thread_key=(
                f"slack:{channel_id}:{thread_ts}" if thread_ts else f"slack:{channel_id}"
            ),
            downstream_key=f"slack:event:{external_id}",
            text=text,
            message_id=timestamp,
            action=action,
            received_at=datetime.now(UTC).isoformat(timespec="seconds").replace("+00:00", "Z"),
            metadata={"thread_ts": thread_ts, "event_ts": timestamp},
        )
        return NormalizedSlackEvent(
            envelope, auth.decision, auth.reason, envelope_id, auth.decision is AuthDecision.IGNORED
        )

    if request_type == "interactive" or payload.get("type") == "block_actions":
        action_values = payload.get("actions")
        actions = action_values if isinstance(action_values, Sequence) else ()
        first_action = _as_mapping(actions[0]) if actions else None
        user = _as_mapping(payload.get("user")) or {}
        channel = _as_mapping(payload.get("channel")) or {}
        message = _as_mapping(payload.get("message")) or {}
        container = _as_mapping(payload.get("container")) or {}
        user_id = str(user.get("id") or "")
        channel_id = str(channel.get("id") or container.get("channel_id") or "")
        timestamp = str(
            container.get("message_ts") or message.get("ts") or payload.get("action_ts") or ""
        )
        external_id = _event_identity(
            payload,
            envelope_id,
            channel_id=channel_id,
            timestamp=timestamp,
            event_type="block_actions",
        )
        if first_action is None or not user_id or not channel_id:
            return NormalizedSlackEvent(
                _ignored_envelope(
                    external_event_id=external_id,
                    event_type="block_actions",
                    user_id=user_id,
                    channel_id=channel_id,
                ),
                AuthDecision.IGNORED,
                "block action identity or action is missing",
                envelope_id,
                True,
            )
        thread_value = message.get("thread_ts")
        thread_ts = str(thread_value or timestamp) if timestamp else None
        action_id = str(first_action.get("action_id") or "")
        callback_data = first_action.get("value")
        if callback_data is None:
            callback_data = action_id
        action = "callback"
        auth = policy.evaluate(Platform.SLACK, user_id, channel_id, action)
        envelope = ChannelEnvelope(
            platform=Platform.SLACK,
            external_event_id=external_id,
            event_type="block_actions",
            user_id=user_id,
            channel_id=channel_id,
            thread_key=(
                f"slack:{channel_id}:{thread_ts}" if thread_ts else f"slack:{channel_id}"
            ),
            downstream_key=f"slack:event:{external_id}",
            message_id=timestamp or None,
            callback_query_id=envelope_id,
            callback_data=str(callback_data),
            action=action,
            received_at=datetime.now(UTC).isoformat(timespec="seconds").replace("+00:00", "Z"),
            metadata={"thread_ts": thread_ts, "action_id": action_id},
        )
        return NormalizedSlackEvent(
            envelope, auth.decision, auth.reason, envelope_id, auth.decision is AuthDecision.IGNORED
        )

    external_id = _event_identity(
        payload,
        envelope_id,
        channel_id="",
        timestamp="",
        event_type=request_type,
    )
    return NormalizedSlackEvent(
        _ignored_envelope(external_event_id=external_id, event_type=request_type),
        AuthDecision.IGNORED,
        "Socket Mode envelope type is not supported",
        envelope_id,
        True,
    )


def valid_slack_blocks(value: object) -> list[dict[str, Any]] | None:
    if not isinstance(value, Sequence) or isinstance(value, (str, bytes)):
        return None
    if len(value) > SLACK_BLOCK_LIMIT:
        return None
    blocks: list[dict[str, Any]] = []
    for raw_block in value:
        block = _as_mapping(raw_block)
        if block is None or not isinstance(block.get("type"), str):
            return None
        block_type = block["type"]
        if block_type == "section":
            text = _as_mapping(block.get("text"))
            if text is not None:
                content = text.get("text")
                if not isinstance(content, str) or len(content) > SLACK_SECTION_TEXT_LIMIT:
                    return None
        elif block_type == "header":
            text = _as_mapping(block.get("text"))
            if text is None:
                return None
            content = text.get("text")
            if not isinstance(content, str) or len(content) > SLACK_HEADER_TEXT_LIMIT:
                return None
        blocks.append(dict(block))
    return blocks


class SlackAdapter:
    """One-workspace Socket Mode adapter.

    The in-process connection guard prevents accidental duplicate sockets. Deployments must still
    run a single channel daemon because a process-local guard cannot coordinate separate processes.
    """

    _active_connections: set[str] = set()

    def __init__(
        self,
        bot_token: str,
        app_token: str,
        store: ChannelStore,
        *,
        authorization_policy: AuthorizationPolicy | None = None,
        socket_transport: SocketTransport | None = None,
        web_client: SlackWebClient | None = None,
        top_level_as_thread_root: bool = True,
        ingest_timeout: float = 2,
        connect_timeout: float = 30,
        close_timeout: float = 5,
        watchdog_interval: float = 10,
        max_send_retries: int = 3,
        max_retry_after: float = 60,
        backoff_base: float = 0.5,
        backoff_max: float = 30,
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
        jitter: Callable[[], float] = random.random,
        logger: logging.Logger | None = None,
    ) -> None:
        if not bot_token.startswith("xoxb-"):
            raise ValueError("Slack bot token must start with xoxb-")
        if not app_token.startswith("xapp-"):
            raise ValueError("Slack app token must start with xapp-")
        if min(ingest_timeout, connect_timeout, close_timeout, watchdog_interval) <= 0:
            raise ValueError("Slack timeouts must be positive")
        if ingest_timeout > 2:
            raise ValueError("Slack ingest timeout must not exceed 2 seconds")
        if max_send_retries < 0:
            raise ValueError("Slack retry count must be non-negative")
        if min(max_retry_after, backoff_base, backoff_max) <= 0:
            raise ValueError("Slack retry delays must be positive")
        self._bot_token = bot_token
        self._app_token = app_token
        self.store = store
        self.authorization_policy = authorization_policy or store.authorization_policy
        self.top_level_as_thread_root = top_level_as_thread_root
        self.ingest_timeout = ingest_timeout
        self.connect_timeout = connect_timeout
        self.close_timeout = close_timeout
        self.watchdog_interval = watchdog_interval
        self.max_send_retries = max_send_retries
        self.max_retry_after = max_retry_after
        self.backoff_base = backoff_base
        self.backoff_max = backoff_max
        self._sleep = sleep
        self._jitter = jitter
        self._logger = logger or logging.getLogger(__name__)
        self._socket_transport = socket_transport
        self._web_client = web_client
        self._owns_socket_transport = socket_transport is None
        self._owns_web_client = web_client is None
        self._closed = False
        self._connected = False
        self._guard_held = False
        self._stop = asyncio.Event()
        self._poll_task: asyncio.Task[None] | None = None
        self._connection_key = hashlib.sha256(app_token.encode()).hexdigest()
        if self._socket_transport is not None:
            self._socket_transport.set_request_handler(self.handle_socket_request)

    def __repr__(self) -> str:
        return f"SlackAdapter(connected={self._connected}, closed={self._closed})"

    def _redact(self, value: object) -> str:
        text = str(value)
        for token in (self._bot_token, self._app_token):
            text = text.replace(token, "[REDACTED]")
            text = text.replace(quote(token, safe=""), "[REDACTED]")
        return text

    @staticmethod
    def _missing_extra(exc: ImportError) -> SlackError:
        return SlackError(
            "Slack support requires the optional channels extra: "
            "install polaris-agent-runtime[channels]"
        )

    def _sdk_logger(self) -> logging.Logger:
        return cast(logging.Logger, _RedactingLogger(self._logger, self._redact))

    def _ensure_web_client(self) -> None:
        if self._web_client is not None:
            return
        try:
            from slack_sdk.web.async_client import AsyncWebClient
        except ImportError as exc:
            raise self._missing_extra(exc) from exc
        client = AsyncWebClient(
            token=self._bot_token,
            proxy=None,
            trust_env_in_session=False,
            logger=self._sdk_logger(),
            retry_handlers=[],
        )
        client.proxy = None
        self._web_client = cast(SlackWebClient, client)

    def _ensure_socket_transport(self) -> None:
        if self._socket_transport is not None:
            return
        self._ensure_web_client()
        try:
            from slack_sdk.socket_mode.aiohttp import SocketModeClient
        except ImportError as exc:
            raise self._missing_extra(exc) from exc
        client = SocketModeClient(
            app_token=self._app_token,
            web_client=cast(Any, self._web_client),
            proxy=None,
            auto_reconnect_enabled=False,
            logger=self._sdk_logger(),
        )
        client.proxy = None
        self._socket_transport = _SlackSDKSocketTransport(client)
        self._socket_transport.set_request_handler(self.handle_socket_request)

    def _ensure_sdk(self) -> None:
        """Initialize both optional production clients (primarily useful for diagnostics)."""
        self._ensure_socket_transport()

    async def connect(self) -> None:
        if self._closed:
            raise SlackConnectionError("Slack adapter is closed")
        if self._connected:
            return
        if self._connection_key in self._active_connections:
            raise SlackConnectionError("a Slack Socket Mode connection is already active")
        self._active_connections.add(self._connection_key)
        self._guard_held = True
        try:
            self._ensure_socket_transport()
            assert self._socket_transport is not None
            await asyncio.wait_for(self._socket_transport.connect(), timeout=self.connect_timeout)
            self._connected = True
            self._stop.clear()
        except BaseException as exc:
            self._release_connection_guard()
            if isinstance(exc, (asyncio.CancelledError, KeyboardInterrupt, SystemExit)):
                raise
            raise SlackConnectionError(f"Slack connection failed: {self._redact(exc)}") from None

    def _release_connection_guard(self) -> None:
        if self._guard_held:
            self._active_connections.discard(self._connection_key)
            self._guard_held = False

    async def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        self._connected = False
        self._stop.set()
        task = self._poll_task
        if task is not None and task is not asyncio.current_task() and not task.done():
            task.cancel()
            with suppress(asyncio.CancelledError, TimeoutError):
                await asyncio.wait_for(task, timeout=self.close_timeout)
        transport = self._socket_transport
        if transport is not None:
            with suppress(Exception, TimeoutError):
                await asyncio.wait_for(transport.close(), timeout=self.close_timeout)
        if self._owns_web_client and self._web_client is not None:
            close = getattr(self._web_client, "close", None)
            if close is not None:
                with suppress(Exception, TimeoutError):
                    await asyncio.wait_for(close(), timeout=self.close_timeout)
        self._release_connection_guard()

    async def handle_socket_request(self, request: object) -> IngestResult | None:
        """Durably ingest accepted, ignored, and malformed envelopes before ACK."""
        try:
            normalized = normalize_slack_event(
                request,
                self.authorization_policy,
                top_level_as_thread_root=self.top_level_as_thread_root,
            )
        except (TypeError, ValueError):
            envelope_id, request_type, _payload = _request_parts(request)
            if envelope_id is None:
                return None
            normalized = NormalizedSlackEvent(
                _ignored_envelope(
                    external_event_id=envelope_id,
                    event_type=request_type,
                ),
                AuthDecision.IGNORED,
                "Socket Mode envelope is malformed",
                envelope_id,
                True,
            )
        minimal_payload = {
            "type": normalized.envelope.event_type,
            "external_event_id": normalized.envelope.external_event_id,
        }
        payload = minimal_payload if normalized.ignored else _request_parts(request)[2]
        result = await asyncio.wait_for(
            asyncio.to_thread(
                self.store.ingest_envelope,
                normalized.envelope,
                payload,
                decision=normalized.decision,
                reason=normalized.reason,
            ),
            timeout=self.ingest_timeout,
        )
        if normalized.envelope_id:
            await self._ack(normalized.envelope_id)
        return result

    async def _ack(
        self, envelope_id: str, payload: Mapping[str, Any] | None = None
    ) -> None:
        self._ensure_socket_transport()
        assert self._socket_transport is not None
        try:
            await self._socket_transport.ack(envelope_id, payload)
        except Exception as exc:
            raise SlackTransportError(f"Slack ACK failed: {self._redact(exc)}") from None

    async def _transport_connected(self) -> bool:
        self._ensure_socket_transport()
        assert self._socket_transport is not None
        try:
            return bool(await self._socket_transport.is_connected())
        except Exception:
            return False

    async def poll_forever(self) -> None:
        """Watch the event-driven socket and reconnect with bounded exponential jitter."""
        failures = 0
        if not self._connected:
            await self.connect()
        while not self._stop.is_set():
            await self._sleep(self.watchdog_interval)
            if self._stop.is_set():
                break
            if await self._transport_connected():
                failures = 0
                continue
            self._connected = False
            try:
                assert self._socket_transport is not None
                await asyncio.wait_for(
                    self._socket_transport.reconnect(), timeout=self.connect_timeout
                )
                self._connected = True
                failures = 0
            except asyncio.CancelledError:
                raise
            except Exception:
                failures += 1
                await self._sleep(self._backoff(failures))

    def start_polling(self) -> asyncio.Task[None]:
        if self._poll_task is not None and not self._poll_task.done():
            return self._poll_task
        self._stop.clear()
        self._poll_task = asyncio.create_task(self.poll_forever(), name="slack-socket-watchdog")
        return self._poll_task

    def _backoff(self, failures: int) -> float:
        exponent = min(30, max(0, failures - 1))
        base = min(self.backoff_max, self.backoff_base * (2**exponent))
        return float(min(self.backoff_max, base * (0.5 + self._jitter())))

    def prepare_outbound(self, message: OutboundMessage) -> list[OutboundMessage]:
        if message.platform is not Platform.SLACK:
            raise ValueError("Slack adapter cannot prepare another platform")
        if len(message.text) <= SLACK_TEXT_LIMIT:
            return [message]
        if message.operation is not MessageOperation.SEND:
            raise ValueError("Slack updates and action feedback cannot be split")
        parts = chunk_outbound(message, limit=SLACK_TEXT_LIMIT)
        if "blocks" not in message.metadata:
            return parts
        return [
            OutboundMessage(
                platform=part.platform,
                idempotency_key=part.idempotency_key,
                channel_id=part.channel_id,
                thread_key=part.thread_key,
                text=part.text,
                operation=part.operation,
                parse_mode=part.parse_mode,
                message_id=part.message_id,
                callback_query_id=part.callback_query_id,
                disable_notification=part.disable_notification,
                chunk_index=part.chunk_index,
                chunk_count=part.chunk_count,
                metadata={key: value for key, value in part.metadata.items() if key != "blocks"},
            )
            for part in parts
        ]

    async def send(self, message: OutboundMessage) -> RemoteReceipt:
        if message.platform is not Platform.SLACK:
            raise ValueError("Slack adapter cannot send another platform")
        self._ensure_web_client()
        remote_ids: list[str] = []
        for part in self.prepare_outbound(message):
            response = await self._send_part_with_retry(part)
            timestamp = response.get("ts") if isinstance(response, Mapping) else None
            if timestamp is not None:
                remote_ids.append(str(timestamp))
        return RemoteReceipt(
            platform=Platform.SLACK,
            idempotency_key=message.idempotency_key,
            remote_message_id=remote_ids[-1] if remote_ids else None,
            channel_id=message.channel_id,
            operation=message.operation,
            remote_message_ids=tuple(remote_ids),
        )

    async def _send_part_with_retry(self, message: OutboundMessage) -> Mapping[str, Any]:
        attempts = 0
        while True:
            try:
                return await self._send_part(message)
            except SlackAPIError as exc:
                if exc.status_code != 429 or attempts >= self.max_send_retries:
                    raise
                attempts += 1
                delay = exc.retry_after
                if delay is None:
                    delay = self._backoff(attempts)
                await self._sleep(min(self.max_retry_after, max(0.0, delay)))

    async def _send_part(self, message: OutboundMessage) -> Mapping[str, Any]:
        assert self._web_client is not None
        payload = self._send_payload(message)
        try:
            if message.operation is MessageOperation.EDIT:
                response = await self._web_client.chat_update(**payload)
            elif message.operation is MessageOperation.ANSWER_CALLBACK:
                if message.message_id:
                    response = await self._web_client.chat_update(**payload)
                else:
                    response = await self._web_client.chat_postEphemeral(**payload)
            else:
                response = await self._web_client.chat_postMessage(**payload)
        except Exception as exc:
            status_code, retry_after = self._exception_rate_limit(exc)
            if status_code == 429:
                raise SlackAPIError(
                    "Slack Web API rate limited the request",
                    status_code=429,
                    retry_after=retry_after,
                ) from None
            if exc.__class__.__name__ == "SlackApiError":
                response_data, response_status, _headers = self._response_parts(
                    getattr(exc, "response", None)
                )
                if response_status is None or response_status < 100 or response_status >= 500:
                    raise SlackTransportError(
                        "Slack Web API delivery outcome is unknown"
                    ) from None
                is_rejection = 400 <= response_status < 500 or (
                    200 <= response_status < 300
                    and response_data is not None
                    and response_data.get("ok") is False
                )
                if not is_rejection:
                    raise SlackTransportError(
                        "Slack Web API delivery outcome is unknown"
                    ) from None
                error = self._redact(
                    response_data.get("error", "unknown_error")
                    if response_data is not None
                    else "unknown_error"
                )
                raise SlackAPIError(
                    f"Slack Web API rejected the request: {error}",
                    status_code=response_status,
                ) from None
            raise SlackTransportError(
                f"Slack Web API transport failed: {self._redact(exc)}"
            ) from None
        data, status_code, headers = self._response_parts(response)
        if not isinstance(response, Mapping) and status_code is None:
            raise SlackTransportError("Slack Web API returned an invalid response")
        retry_after = self._retry_after(headers)
        if status_code == 429:
            raise SlackAPIError(
                "Slack Web API rate limited the request",
                status_code=429,
                retry_after=retry_after,
            )
        if status_code is not None and status_code >= 500:
            raise SlackTransportError("Slack Web API delivery outcome is unknown")
        if status_code is not None and status_code >= 400:
            error = self._redact(
                data.get("error", "unknown_error") if data is not None else "unknown_error"
            )
            raise SlackAPIError(
                f"Slack Web API rejected the request: {error}",
                status_code=status_code,
            )
        if data is None or (status_code is not None and status_code < 100):
            raise SlackTransportError("Slack Web API returned an invalid response")
        if data.get("ok") is False:
            error = self._redact(data.get("error", "unknown_error"))
            raise SlackAPIError(
                f"Slack Web API rejected the request: {error}",
                status_code=status_code,
            )
        if data.get("ok") is not True:
            raise SlackTransportError("Slack Web API returned an invalid response")
        return data

    @classmethod
    def _exception_rate_limit(cls, exc: Exception) -> tuple[int | None, float | None]:
        response = getattr(exc, "response", None)
        data, status, headers = cls._response_parts(response)
        if status is None and data is not None and data.get("error") == "ratelimited":
            status = 429
        return status, cls._retry_after(headers)

    @staticmethod
    def _response_parts(
        response: object,
    ) -> tuple[Mapping[str, Any] | None, int | None, Mapping[str, Any] | None]:
        data = _as_mapping(response)
        if data is None:
            data = _as_mapping(getattr(response, "data", None))
        status_value = getattr(response, "status_code", None)
        if status_value is None and isinstance(response, Mapping):
            status_value = response.get("status_code")
        status = (
            status_value
            if isinstance(status_value, int) and not isinstance(status_value, bool)
            else None
        )
        headers = _as_mapping(getattr(response, "headers", None))
        if headers is None and isinstance(response, Mapping):
            headers = _as_mapping(response.get("headers"))
        return data, status, headers

    @staticmethod
    def _retry_after(headers: Mapping[str, Any] | None) -> float | None:
        retry_value = None
        if headers is not None:
            retry_value = next(
                (
                    value
                    for key, value in headers.items()
                    if str(key).lower() == "retry-after"
                ),
                None,
            )
        try:
            return float(retry_value) if retry_value is not None else None
        except (TypeError, ValueError):
            return None

    @staticmethod
    def _thread_ts(message: OutboundMessage) -> str | None:
        value = message.metadata.get("thread_ts")
        if value:
            return str(value)
        prefix = f"slack:{message.channel_id}:"
        if message.thread_key.startswith(prefix):
            value = message.thread_key[len(prefix) :]
            return value or None
        return None

    def _send_payload(self, message: OutboundMessage) -> dict[str, Any]:
        payload: dict[str, Any] = {"channel": message.channel_id, "text": message.text}
        blocks = valid_slack_blocks(message.metadata.get("blocks"))
        if blocks is not None:
            payload["blocks"] = blocks
        if message.operation is MessageOperation.EDIT:
            if not message.message_id:
                raise ValueError("Slack chat_update requires message_id")
            payload["ts"] = message.message_id
            return payload
        if message.operation is MessageOperation.ANSWER_CALLBACK:
            if message.message_id:
                payload["ts"] = message.message_id
                return payload
            user_id = message.metadata.get("user_id")
            if not user_id:
                raise ValueError("Slack action feedback requires message_id or metadata user_id")
            payload["user"] = str(user_id)
            return payload
        thread_ts = self._thread_ts(message)
        if thread_ts:
            payload["thread_ts"] = thread_ts
        return payload
