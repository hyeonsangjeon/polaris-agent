"""Raw-httpx Telegram Bot API adapter using durable long polling."""

from __future__ import annotations

import asyncio
import logging
import random
from collections.abc import Awaitable, Callable, Mapping
from contextlib import suppress
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any, Protocol, runtime_checkable
from urllib.parse import quote

import httpx

from .auth import AuthorizationPolicy
from .formatting import TELEGRAM_TEXT_LIMIT, chunk_outbound, safe_html, utf16_units
from .models import (
    AuthDecision,
    ChannelEnvelope,
    IngestResult,
    MessageOperation,
    OutboundMessage,
    ParseMode,
    Platform,
    RemoteReceipt,
)
from .store import ChannelStore

TELEGRAM_UPDATE_ID_EPOCH_RESET_AFTER = timedelta(days=7)


class TelegramError(RuntimeError):
    delivery_unknown = False


class TelegramTransportError(TelegramError):
    delivery_unknown = True


class TelegramAPIError(TelegramError):
    def __init__(
        self,
        message: str,
        *,
        status_code: int | None = None,
        error_code: int | None = None,
        retry_after: float | None = None,
    ) -> None:
        super().__init__(message)
        self.status_code = status_code
        self.error_code = error_code
        self.retry_after = retry_after


class TelegramConflictError(TelegramAPIError):
    pass


@dataclass(frozen=True, slots=True)
class NormalizedTelegramUpdate:
    envelope: ChannelEnvelope | None
    decision: AuthDecision
    reason: str
    event_type: str
    action: str
    user_id: str | None
    channel_id: str | None


@dataclass(frozen=True, slots=True)
class TelegramOffsetState:
    """Durable offset state needed to detect Telegram's update-ID epoch reset."""

    offset: int | None
    last_activity_at: datetime | None
    epoch: int = 0
    bot_id: str | None = None


@runtime_checkable
class TelegramOffsetStateStore(Protocol):
    """Optional store extension for restart-safe update-ID epoch detection."""

    def get_telegram_offset_state(self, stream_key: str) -> TelegramOffsetState: ...

    def begin_telegram_epoch(self, next_offset: int, stream_key: str) -> int: ...


@runtime_checkable
class TelegramIdentityStore(Protocol):
    """Store extension that binds a polling stream to its getMe identity."""

    def bind_telegram_identity(self, bot_id: str, stream_key: str) -> int: ...


class _TelegramTokenFilter(logging.Filter):
    def __init__(self, redact: Callable[[object], str]) -> None:
        super().__init__()
        self._redact = redact

    def _value(self, value: object) -> object:
        safe = self._redact(value)
        try:
            original = str(value)
        except Exception:
            return safe
        return value if safe == original else safe

    def filter(self, record: logging.LogRecord) -> bool:
        record.msg = self._value(record.msg)
        if isinstance(record.args, Mapping):
            record.args = {key: self._value(value) for key, value in record.args.items()}
        elif isinstance(record.args, tuple):
            record.args = tuple(self._value(value) for value in record.args)
        return True


def _ignored(event_type: str, reason: str) -> NormalizedTelegramUpdate:
    return NormalizedTelegramUpdate(
        envelope=None,
        decision=AuthDecision.IGNORED,
        reason=reason,
        event_type=event_type,
        action="ignored",
        user_id=None,
        channel_id=None,
    )


def normalize_telegram_update(
    update: Mapping[str, Any], policy: AuthorizationPolicy
) -> NormalizedTelegramUpdate:
    """Normalize supported private Telegram updates and apply one shared policy."""
    update_id = update.get("update_id")
    if isinstance(update_id, bool) or not isinstance(update_id, int):
        raise ValueError("Telegram update_id must be an integer")
    message_value = update.get("message")
    callback_value = update.get("callback_query")
    event_type: str
    action: str
    text: str | None
    callback_data: str | None
    callback_query_id: str | None
    message_id: str | None
    if isinstance(message_value, Mapping):
        message = message_value
        if message.get("is_topic_message") or "message_thread_id" in message:
            return _ignored("message", "forum topics are not supported")
        text_value = message.get("text")
        sender = message.get("from")
        chat = message.get("chat")
        if not isinstance(text_value, str):
            return _ignored("message", "only text messages are supported")
        if not isinstance(sender, Mapping) or not isinstance(chat, Mapping):
            return _ignored("message", "message identity is missing")
        event_type = "message"
        action = "command" if text_value.startswith("/") else "message"
        text = text_value
        callback_data = None
        callback_query_id = None
        message_id = str(message["message_id"]) if "message_id" in message else None
    elif isinstance(callback_value, Mapping):
        callback = callback_value
        callback_message = callback.get("message")
        sender = callback.get("from")
        if not isinstance(callback_message, Mapping) or not isinstance(sender, Mapping):
            return _ignored("callback_query", "inline callbacks are not supported")
        if callback_message.get("is_topic_message") or "message_thread_id" in callback_message:
            return _ignored("callback_query", "forum topics are not supported")
        chat = callback_message.get("chat")
        data_value = callback.get("data")
        if not isinstance(chat, Mapping) or not isinstance(data_value, str):
            return _ignored("callback_query", "callback data or chat identity is missing")
        event_type = "callback_query"
        action = "callback"
        text = None
        callback_data = data_value
        callback_query_id = str(callback["id"]) if "id" in callback else None
        message_id = (
            str(callback_message["message_id"]) if "message_id" in callback_message else None
        )
    else:
        return _ignored("unsupported", "update type is not supported")
    user_value = sender.get("id")
    channel_value = chat.get("id")
    if user_value is None or channel_value is None:
        return _ignored(event_type, "user or chat identity is missing")
    user_id = str(user_value)
    channel_id = str(channel_value)
    auth = policy.evaluate(Platform.TELEGRAM, user_id, channel_id, action)
    envelope = ChannelEnvelope(
        platform=Platform.TELEGRAM,
        external_event_id=str(update_id),
        event_type=event_type,
        user_id=user_id,
        channel_id=channel_id,
        thread_key=f"telegram:{channel_id}",
        downstream_key=f"telegram:update:{update_id}",
        text=text,
        message_id=message_id,
        callback_query_id=callback_query_id,
        callback_data=callback_data,
        action=action,
        received_at=datetime.now(UTC).isoformat(timespec="seconds").replace("+00:00", "Z"),
        metadata={},
    )
    return NormalizedTelegramUpdate(
        envelope=envelope,
        decision=auth.decision,
        reason=auth.reason,
        event_type=event_type,
        action=action,
        user_id=user_id,
        channel_id=channel_id,
    )


class TelegramAdapter:
    """Telegram Bot API adapter without webhook or public ingress support."""

    def __init__(
        self,
        token: str,
        store: ChannelStore,
        *,
        authorization_policy: AuthorizationPolicy | None = None,
        stream_key: str | None = None,
        client: httpx.AsyncClient | None = None,
        transport: httpx.AsyncBaseTransport | None = None,
        long_poll_timeout: int = 30,
        request_timeout: float = 10,
        close_timeout: float = 5,
        max_conflicts: int = 3,
        max_send_retries: int = 3,
        max_retry_after: float = 60,
        backoff_base: float = 0.5,
        backoff_max: float = 30,
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
        jitter: Callable[[], float] = random.random,
        clock: Callable[[], datetime] = lambda: datetime.now(UTC),
        update_id_epoch_reset_after: timedelta = TELEGRAM_UPDATE_ID_EPOCH_RESET_AFTER,
    ) -> None:
        if not token:
            raise ValueError("Telegram token must not be empty")
        if long_poll_timeout < 0 or request_timeout <= 0 or close_timeout <= 0:
            raise ValueError("timeouts must be positive")
        if max_conflicts < 1 or max_send_retries < 0:
            raise ValueError("retry limits are invalid")
        if max_retry_after <= 0 or backoff_base <= 0 or backoff_max <= 0:
            raise ValueError("retry delays must be positive")
        if update_id_epoch_reset_after <= timedelta(0):
            raise ValueError("update ID epoch reset interval must be positive")
        self._token = token
        self._base_url = f"https://api.telegram.org/bot{token}"
        self.store = store
        self.authorization_policy = authorization_policy or store.authorization_policy
        self.stream_key = stream_key or store.telegram_stream_key
        self.long_poll_timeout = long_poll_timeout
        self.request_timeout = request_timeout
        self.close_timeout = close_timeout
        self.max_conflicts = max_conflicts
        self.max_send_retries = max_send_retries
        self.max_retry_after = max_retry_after
        self.backoff_base = backoff_base
        self.backoff_max = backoff_max
        self._sleep = sleep
        self._jitter = jitter
        self._clock = clock
        self.update_id_epoch_reset_after = update_id_epoch_reset_after
        self._last_update_at = self._aware_utc(clock())
        self._epoch_offset: int | None = None
        self._probing_update_epoch = False
        self._httpx_logger = logging.getLogger("httpx")
        self._httpx_log_filter = _TelegramTokenFilter(self._redact)
        self._httpx_logger.addFilter(self._httpx_log_filter)
        self._owns_client = client is None
        try:
            self._client = client or httpx.AsyncClient(transport=transport, trust_env=False)
        except BaseException:
            self._httpx_logger.removeFilter(self._httpx_log_filter)
            raise
        self._identity: dict[str, Any] | None = None
        self._closed = False
        self._stop = asyncio.Event()
        self._poll_task: asyncio.Task[None] | None = None

    def __repr__(self) -> str:
        identity = None if self._identity is None else self._identity.get("id")
        return f"TelegramAdapter(identity={identity!r}, connected={self._identity is not None})"

    @property
    def identity(self) -> dict[str, Any] | None:
        return None if self._identity is None else dict(self._identity)

    def _redact(self, value: object) -> str:
        text = str(value)
        for secret in (self._token, quote(self._token, safe="")):
            text = text.replace(secret, "[REDACTED]")
        return text.replace(self._base_url, "https://api.telegram.org/bot[REDACTED]")

    @staticmethod
    def _aware_utc(value: datetime) -> datetime:
        if value.tzinfo is None:
            return value.replace(tzinfo=UTC)
        return value.astimezone(UTC)

    async def _request(
        self,
        method: str,
        payload: Mapping[str, Any] | None = None,
        *,
        timeout: float | None = None,
    ) -> Any:
        if self._closed:
            raise TelegramError("Telegram adapter is closed")
        url = f"{self._base_url}/{method}"
        try:
            response = await self._client.post(
                url,
                json=dict(payload or {}),
                timeout=self.request_timeout if timeout is None else timeout,
            )
        except (httpx.HTTPError, TimeoutError) as exc:
            raise TelegramTransportError(
                f"Telegram {method} transport failed: {self._redact(exc)}"
            ) from None
        try:
            body = response.json()
        except ValueError:
            body = None
        error_code = None
        description = f"HTTP {response.status_code}"
        retry_after: float | None = None
        if isinstance(body, Mapping):
            raw_error_code = body.get("error_code")
            if isinstance(raw_error_code, int):
                error_code = raw_error_code
            if body.get("description") is not None:
                description = self._redact(body["description"])
            parameters = body.get("parameters")
            if isinstance(parameters, Mapping):
                raw_retry_after = parameters.get("retry_after")
                if isinstance(raw_retry_after, (int, float)) and not isinstance(
                    raw_retry_after, bool
                ):
                    retry_after = float(raw_retry_after)
        if retry_after is None and (response.status_code == 429 or error_code == 429):
            header = response.headers.get("Retry-After")
            try:
                retry_after = None if header is None else float(header)
            except ValueError:
                retry_after = None
        if response.status_code >= 500:
            raise TelegramTransportError(
                f"Telegram {method} delivery outcome is unknown"
            ) from None
        if 400 <= response.status_code < 500:
            effective_code = response.status_code
        else:
            effective_code = error_code or response.status_code
        if effective_code >= 500:
            raise TelegramTransportError(
                f"Telegram {method} delivery outcome is unknown"
            ) from None
        if (
            200 <= response.status_code < 300
            and isinstance(body, Mapping)
            and body.get("ok") is True
        ):
            return body.get("result")
        if not 400 <= effective_code < 500:
            raise TelegramTransportError(
                f"Telegram {method} returned an invalid response"
            ) from None
        exception_type = TelegramConflictError if effective_code == 409 else TelegramAPIError
        raise exception_type(
            f"Telegram {method} failed: {description}",
            status_code=response.status_code,
            error_code=error_code,
            retry_after=retry_after,
        )

    async def connect(self) -> None:
        identity = await self._request("getMe")
        if not isinstance(identity, Mapping) or "id" not in identity:
            raise TelegramError("Telegram getMe returned an invalid identity")
        self._identity = dict(identity)
        if isinstance(self.store, TelegramIdentityStore):
            self.store.bind_telegram_identity(str(identity["id"]), self.stream_key)
            self._epoch_offset = None
            self._probing_update_epoch = False
        await self._request("deleteWebhook", {"drop_pending_updates": False})

    async def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        self._stop.set()
        try:
            task = self._poll_task
            if task is not None and task is not asyncio.current_task() and not task.done():
                task.cancel()
                done, _pending = await asyncio.wait({task}, timeout=self.close_timeout)
                if task in done:
                    with suppress(asyncio.CancelledError, TelegramError):
                        task.result()
            if self._owns_client:
                with suppress(TimeoutError):
                    await asyncio.wait_for(self._client.aclose(), timeout=self.close_timeout)
        finally:
            self._httpx_logger.removeFilter(self._httpx_log_filter)

    async def poll_once(self) -> list[IngestResult]:
        offset = self._poll_offset()
        payload: dict[str, Any] = {
            "timeout": self.long_poll_timeout,
            "allowed_updates": ["message", "callback_query"],
        }
        if offset is not None:
            payload["offset"] = offset
        result = await self._request(
            "getUpdates",
            payload,
            timeout=self.long_poll_timeout + self.request_timeout,
        )
        if not isinstance(result, list):
            raise TelegramError("Telegram getUpdates returned an invalid update list")
        updates: list[Mapping[str, Any]] = []
        for update in result:
            if not isinstance(update, Mapping):
                raise TelegramError("Telegram getUpdates returned a malformed update")
            update_id = update.get("update_id")
            if isinstance(update_id, bool) or not isinstance(update_id, int):
                raise TelegramError("Telegram getUpdates returned an update without an integer id")
            updates.append(update)
        if updates and self._probing_update_epoch:
            first_update_id = min(int(update["update_id"]) for update in updates)
            if isinstance(self.store, TelegramOffsetStateStore):
                self.store.begin_telegram_epoch(first_update_id, self.stream_key)
        ingested: list[IngestResult] = []
        for update in sorted(updates, key=lambda item: int(item["update_id"])):
            ingested.append(
                self.store.ingest_telegram_update(
                    update,
                    self.authorization_policy,
                    stream_key=self.stream_key,
                )
            )
        if updates:
            self._last_update_at = self._aware_utc(self._clock())
            if self._probing_update_epoch or self._epoch_offset is not None:
                self._epoch_offset = max(int(update["update_id"]) for update in updates) + 1
                self._probing_update_epoch = False
        return ingested

    def _poll_offset(self) -> int | None:
        if self._epoch_offset is not None:
            return self._epoch_offset
        offset = self.store.get_telegram_offset(self.stream_key)
        last_activity_at = self._last_update_at
        if isinstance(self.store, TelegramOffsetStateStore):
            state = self.store.get_telegram_offset_state(self.stream_key)
            offset = state.offset
            if state.last_activity_at is not None:
                last_activity_at = self._aware_utc(state.last_activity_at)
        now = self._aware_utc(self._clock())
        if offset is not None and now - last_activity_at >= self.update_id_epoch_reset_after:
            self._probing_update_epoch = True
            return None
        return offset

    async def poll_forever(self) -> None:
        failures = 0
        conflicts = 0
        while not self._stop.is_set():
            try:
                await self.poll_once()
            except asyncio.CancelledError:
                raise
            except TelegramConflictError:
                conflicts += 1
                if conflicts >= self.max_conflicts:
                    raise TelegramConflictError(
                        f"Telegram polling stopped after {conflicts} conflicting pollers",
                        status_code=409,
                        error_code=409,
                    ) from None
                failures += 1
                await self._sleep(self._backoff(failures))
            except TelegramError:
                conflicts = 0
                failures += 1
                await self._sleep(self._backoff(failures))
            else:
                conflicts = 0
                failures = 0

    def start_polling(self) -> asyncio.Task[None]:
        if self._poll_task is not None and not self._poll_task.done():
            return self._poll_task
        self._stop.clear()
        self._poll_task = asyncio.create_task(self.poll_forever(), name="telegram-long-poll")
        return self._poll_task

    def _backoff(self, failures: int) -> float:
        exponent = min(30, max(0, failures - 1))
        base = min(self.backoff_max, self.backoff_base * (2**exponent))
        return float(min(self.backoff_max, base * (0.5 + self._jitter())))

    def prepare_outbound(self, message: OutboundMessage) -> list[OutboundMessage]:
        if message.platform is not Platform.TELEGRAM:
            raise ValueError("Telegram adapter cannot prepare another platform")
        if message.operation is MessageOperation.SEND:
            return chunk_outbound(message, limit=TELEGRAM_TEXT_LIMIT)
        limit = (
            200
            if message.operation is MessageOperation.ANSWER_CALLBACK
            else TELEGRAM_TEXT_LIMIT
        )
        if utf16_units(message.text) > limit:
            if message.operation is MessageOperation.ANSWER_CALLBACK:
                raise ValueError("answerCallbackQuery text exceeds 200 UTF-16 units")
            raise ValueError("Telegram edit text exceeds 4096 UTF-16 units")
        return [message]

    async def send(self, message: OutboundMessage) -> RemoteReceipt:
        if message.platform is not Platform.TELEGRAM:
            raise ValueError("Telegram adapter cannot send another platform")
        prepared = self.prepare_outbound(message)
        remote_ids: list[str] = []
        for part in prepared:
            result = await self._send_part_with_retry(part)
            if part.operation is MessageOperation.ANSWER_CALLBACK and result is True:
                continue
            if isinstance(result, Mapping):
                message_id = result.get("message_id")
                if message_id is not None:
                    remote_ids.append(str(message_id))
                    continue
            raise TelegramTransportError(
                "Telegram send returned an invalid delivery response"
            )
        return RemoteReceipt(
            platform=Platform.TELEGRAM,
            idempotency_key=message.idempotency_key,
            remote_message_id=remote_ids[-1] if remote_ids else None,
            channel_id=message.channel_id,
            operation=message.operation,
            remote_message_ids=tuple(remote_ids),
        )

    async def _send_part_with_retry(self, message: OutboundMessage) -> Any:
        attempts = 0
        while True:
            try:
                method, payload = self._send_payload(message)
                return await self._request(method, payload)
            except TelegramAPIError as exc:
                is_rate_limit = exc.status_code == 429 or exc.error_code == 429
                if not is_rate_limit or attempts >= self.max_send_retries:
                    raise
                attempts += 1
                delay = exc.retry_after
                if delay is None:
                    delay = self._backoff(attempts)
                await self._sleep(min(self.max_retry_after, max(0.0, delay)))

    @staticmethod
    def _send_payload(message: OutboundMessage) -> tuple[str, dict[str, Any]]:
        text = message.text
        payload: dict[str, Any]
        if message.parse_mode is ParseMode.HTML:
            text = text if message.metadata.get("trusted_html") is True else safe_html(text)
        if message.operation is MessageOperation.ANSWER_CALLBACK:
            if not message.callback_query_id:
                raise ValueError("answerCallbackQuery requires callback_query_id")
            if utf16_units(text) > 200:
                raise ValueError("answerCallbackQuery text exceeds 200 UTF-16 units")
            return "answerCallbackQuery", {
                "callback_query_id": message.callback_query_id,
                "text": text,
            }
        payload = {
            "chat_id": message.channel_id,
            "text": text,
            "disable_notification": message.disable_notification,
        }
        if message.parse_mode is ParseMode.HTML:
            payload["parse_mode"] = "HTML"
        if message.operation is MessageOperation.EDIT:
            if not message.message_id:
                raise ValueError("editMessageText requires message_id")
            payload["message_id"] = message.message_id
            payload.pop("disable_notification")
            return "editMessageText", payload
        return "sendMessage", payload
