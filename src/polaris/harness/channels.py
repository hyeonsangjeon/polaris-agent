"""Shared Telegram and Slack command routing with durable completion delivery."""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Awaitable, Callable, Mapping
from contextlib import suppress
from typing import TYPE_CHECKING, Any

from polaris.channels import (
    AuthorizationPolicy,
    ChannelAdapter,
    ChannelEnvelope,
    ChannelProcessor,
    ChannelStore,
    MessageOperation,
    OutboundMessage,
    OutboundPreparer,
    Platform,
    SlackAdapter,
    TelegramAdapter,
)
from polaris.config import AppConfig, secret_from_env
from polaris.journal import RunStatus
from polaris.memory import MemoryKind, MemoryScope, TrustLevel

if TYPE_CHECKING:
    from polaris.service import AgentService

Sleep = Callable[[float], Awaitable[None]]
_TERMINAL = {RunStatus.COMPLETED, RunStatus.FAILED, RunStatus.CANCELLED}


def _thread_metadata(envelope: ChannelEnvelope) -> dict[str, Any]:
    metadata = dict(envelope.metadata)
    metadata["user_id"] = envelope.user_id
    return metadata


class ChannelRouter:
    """Route the same conservative command set for every remote platform."""

    def __init__(self, service: AgentService, store: ChannelStore) -> None:
        self.service = service
        self.store = store

    def _scope(self, envelope: ChannelEnvelope) -> MemoryScope:
        return MemoryScope(
            self.service.config.memory.profile_id,
            f"{envelope.platform.value}:{envelope.user_id}",
        )

    def _provider(self, platform: Platform) -> str | None:
        if platform is Platform.TELEGRAM:
            return self.service.config.channels.telegram.default_provider
        return self.service.config.channels.slack.default_provider

    @staticmethod
    def _message(
        envelope: ChannelEnvelope,
        text: str,
        suffix: str,
        *,
        run_id: str | None = None,
        callback: bool = False,
    ) -> OutboundMessage:
        metadata = _thread_metadata(envelope)
        if run_id is not None:
            metadata["run_id"] = run_id
        operation = (
            MessageOperation.ANSWER_CALLBACK
            if callback and envelope.callback_query_id is not None
            else MessageOperation.SEND
        )
        return OutboundMessage(
            platform=envelope.platform,
            idempotency_key=(
                f"channel:{envelope.platform.value}:{envelope.external_event_id}:{suffix}"
            ),
            channel_id=envelope.channel_id,
            thread_key=envelope.thread_key,
            text=text,
            operation=operation,
            message_id=(
                envelope.message_id
                if operation is MessageOperation.ANSWER_CALLBACK
                else None
            ),
            callback_query_id=envelope.callback_query_id,
            metadata=metadata,
        )

    async def handle(self, envelope: ChannelEnvelope) -> list[OutboundMessage]:
        callback = envelope.callback_data
        text = (envelope.text or "").strip()
        if callback is not None:
            return [await self._approval(envelope, callback, callback=True)]
        if not text:
            return [self._message(envelope, "No command text was provided.", "empty")]
        command, separator, argument = text.partition(" ")
        lowered = command.casefold()
        if not command.startswith("/") or lowered == "/run":
            prompt = argument.strip() if lowered == "/run" else text
            if not prompt:
                return [self._message(envelope, "Usage: /run <prompt>", "usage")]
            run = await self.service.submit_single(
                prompt,
                provider=self._provider(envelope.platform),
                memory_scope=self._scope(envelope),
                external_key=envelope.downstream_key,
            )
            self.store.link_inbox_run(
                envelope.platform,
                envelope.external_event_id,
                envelope.downstream_key,
                run.id,
            )
            return [
                self._message(
                    envelope,
                    f"Run created: {run.id}",
                    "run-created",
                    run_id=run.id,
                )
            ]
        if lowered == "/status":
            return [self._message(envelope, self._status(argument.strip()), "status")]
        if lowered in {"/approve", "/deny"}:
            value = f"{lowered[1:]}:{argument.strip()}"
            return [await self._approval(envelope, value)]
        if lowered == "/memory":
            return [await self._memory(envelope, argument)]
        if lowered == "/cron":
            return [self._cron(envelope, argument)]
        if lowered == "/help":
            return [self._message(envelope, self.help_text(), "help")]
        return [self._message(envelope, "Unknown command. Use /help.", "unknown-command")]

    def _status(self, run_id: str) -> str:
        if not run_id:
            return "Usage: /status RUN_ID"
        run = self.service.get(run_id)
        lines = [f"Run {run.id}: {run.status.value}"]
        if run.status is RunStatus.COMPLETED:
            replay = self.service.replay(run.id)
            output = getattr(replay, "final_output", None)
            if output:
                lines.append(str(output))
        elif run.status is RunStatus.PAUSED:
            approvals = self.service.approvals(run.id, pending_only=True)
            if approvals:
                lines.append("Pending approval: " + ", ".join(item.id for item in approvals))
        return "\n".join(lines)

    async def _approval(
        self,
        envelope: ChannelEnvelope,
        value: str,
        *,
        callback: bool = False,
    ) -> OutboundMessage:
        action, separator, approval_id = value.partition(":")
        action = action.casefold().removeprefix("/")
        if not separator or action not in {"approve", "deny"} or not approval_id.strip():
            return self._message(
                envelope,
                "Approval action must be approve:ID or deny:ID.",
                "approval-usage",
                callback=callback,
            )
        approved = action == "approve"
        record = await self.service.decide_approval(
            approval_id.strip(),
            approved,
            decided_by=f"{envelope.platform.value}:{envelope.user_id}",
            reason="remote explicit decision",
        )
        return self._message(
            envelope,
            f"Approval {record.id} {record.decision}.",
            f"approval-{record.id}",
            callback=callback,
        )

    async def _memory(
        self, envelope: ChannelEnvelope, argument: str
    ) -> OutboundMessage:
        operation, _separator, value = argument.strip().partition(" ")
        scope = self._scope(envelope)
        if operation.casefold() == "add" and value.strip():
            entry = self.service.memory_add(
                scope,
                value.strip(),
                kind=MemoryKind.FACT,
                trust_level=TrustLevel.USER_ASSERTED,
                provenance_session_id=envelope.thread_key,
                provenance_message_id=envelope.message_id,
                idempotency_key=(
                    f"channel:{envelope.platform.value}:"
                    f"{envelope.external_event_id}:memory:add"
                ),
            )
            text = f"Memory added: {entry.id}"
        elif operation.casefold() == "search" and value.strip():
            hits = self.service.memory_search(scope, value.strip())
            text = "\n".join(f"{hit.id}: {hit.content}" for hit in hits) or "No memories found."
        elif operation.casefold() == "list" and not value.strip():
            entries = self.service.memory_list(scope)
            text = "\n".join(f"{entry.id}: {entry.content}" for entry in entries) or "No memories."
        else:
            text = "Usage: /memory add TEXT | /memory search QUERY | /memory list"
        return self._message(envelope, text, "memory")

    def _cron(self, envelope: ChannelEnvelope, argument: str) -> OutboundMessage:
        if argument.strip().casefold() != "list":
            text = "Remote scheduling is read-only in v0.2. Usage: /cron list"
        else:
            jobs = self.service.list_jobs()
            text = (
                "\n".join(
                    f"{job.id}: {job.name} ({job.state.value})"
                    for job in jobs
                )
                or "No scheduled jobs."
            )
        return self._message(envelope, text, "cron")

    @staticmethod
    def help_text() -> str:
        return (
            "/run <prompt>\n/status RUN_ID\n/approve ID\n/deny ID\n"
            "/memory add TEXT\n/memory search QUERY\n/memory list\n/cron list\n/help"
        )


class ChannelHarness:
    """Own adapters, bounded pumps, and the durable completion monitor."""

    def __init__(
        self,
        service: AgentService,
        config: AppConfig,
        *,
        store: ChannelStore | None = None,
        telegram_adapter: ChannelAdapter | None = None,
        slack_adapter: ChannelAdapter | None = None,
        env: dict[str, str] | None = None,
        sleep: Sleep = asyncio.sleep,
    ) -> None:
        self.service = service
        self.config = config
        self._sleep = sleep
        self._logger = logging.getLogger(__name__)
        telegram = config.channels.telegram
        slack = config.channels.slack
        self.policy = AuthorizationPolicy(
            allowed_actions=("message", "command", "callback"),
            platform_users={
                Platform.TELEGRAM: telegram.allowed_user_ids,
                Platform.SLACK: slack.allowed_user_ids,
            },
            platform_channels={
                Platform.TELEGRAM: telegram.allowed_chat_ids,
                Platform.SLACK: slack.allowed_channel_ids,
            },
        )
        self.store = store or ChannelStore(
            config.paths.journal_file,
            authorization_policy=self.policy,
        )
        self.router = ChannelRouter(service, self.store)
        self._secrets: tuple[str, ...] = ()
        secrets: list[str] = []
        self.adapters: dict[Platform, ChannelAdapter] = {}
        if telegram.enabled:
            token = secret_from_env(telegram.token_env, env)
            if token is None:
                raise ValueError(
                    f"Telegram token environment variable {telegram.token_env!r} is not set"
                )
            secrets.append(token)
            self.adapters[Platform.TELEGRAM] = telegram_adapter or TelegramAdapter(
                token,
                self.store,
                authorization_policy=self.policy,
                long_poll_timeout=telegram.long_poll_timeout,
                request_timeout=telegram.request_timeout,
                close_timeout=telegram.close_timeout,
                max_conflicts=telegram.max_conflicts,
                backoff_base=telegram.backoff_base,
                backoff_max=telegram.backoff_max,
            )
        if slack.enabled:
            bot_token = secret_from_env(slack.bot_token_env, env)
            app_token = secret_from_env(slack.app_token_env, env)
            if bot_token is None or app_token is None:
                raise ValueError("configured Slack token environment variables are not set")
            secrets.extend((bot_token, app_token))
            self.adapters[Platform.SLACK] = slack_adapter or SlackAdapter(
                bot_token,
                app_token,
                self.store,
                authorization_policy=self.policy,
                connect_timeout=slack.connect_timeout,
                close_timeout=slack.close_timeout,
                watchdog_interval=slack.watchdog_interval,
                backoff_base=slack.reconnect_backoff_base,
                backoff_max=slack.reconnect_backoff_max,
            )
        self._secrets = tuple(secrets)
        self.processors = {
            platform: ChannelProcessor(
                self.store,
                adapter,
                self.router.handle,
                owner=f"channel-{platform.value}",
                platform=platform,
            )
            for platform, adapter in self.adapters.items()
        }
        self._tasks: set[asyncio.Task[None]] = set()
        self._failures: list[str] = []
        self._started = False

    def _redact(self, value: object) -> str:
        text = str(value)
        for secret in self._secrets:
            text = text.replace(secret, "[REDACTED]")
        return text

    def _observe(self, task: asyncio.Task[None]) -> None:
        self._tasks.discard(task)
        if task.cancelled():
            return
        exception = task.exception()
        if exception is not None:
            message = f"{type(exception).__name__}: {self._redact(exception)}"
            self._failures.append(message)
            self._logger.error("channel background task failed: %s", message)

    def _track(self, task: asyncio.Task[None]) -> None:
        self._tasks.add(task)
        task.add_done_callback(self._observe)

    async def startup(self) -> None:
        if self._started:
            return
        self.store.recover_inbox_leases()
        self.store.recover_outbox_leases()
        connected: list[ChannelAdapter] = []
        try:
            for adapter in self.adapters.values():
                await adapter.connect()
                connected.append(adapter)
            for adapter in self.adapters.values():
                start_polling = getattr(adapter, "start_polling", None)
                if callable(start_polling):
                    self._track(start_polling())
            for platform, processor in self.processors.items():
                self._track(
                    asyncio.create_task(
                        self._pump(processor),
                        name=f"channel-pump:{platform.value}",
                    )
                )
            if self.adapters:
                self._track(
                    asyncio.create_task(
                        self._monitor_completions(),
                        name="channel-completion-monitor",
                    )
                )
        except BaseException:
            for adapter in reversed(connected):
                with suppress(Exception):
                    await adapter.close()
            raise
        self._started = True

    async def _pump(self, processor: ChannelProcessor) -> None:
        failures = 0
        while True:
            try:
                worked = await processor.process_once()
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                failures += 1
                message = f"{type(exc).__name__}: {self._redact(exc)}"
                self._logger.warning("channel pump error: %s", message)
                await self._sleep(min(5.0, 0.1 * (2 ** min(failures, 5))))
            else:
                failures = 0
                if not worked:
                    await self._sleep(0.1)

    async def _monitor_completions(self) -> None:
        while True:
            try:
                self._enqueue_completion_notifications_once()
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                message = f"{type(exc).__name__}: {self._redact(exc)}"
                self._logger.warning("channel completion monitor error: %s", message)
            await self._sleep(0.25)

    def _prepare_outbound(self, message: OutboundMessage) -> tuple[OutboundMessage, ...]:
        adapter = self.adapters.get(message.platform)
        if adapter is not None and isinstance(adapter, OutboundPreparer):
            prepared = tuple(adapter.prepare_outbound(message))
        else:
            prepared = (message,)
        if not prepared:
            raise ValueError("outbound preparer returned no messages")
        if any(item.platform is not message.platform for item in prepared):
            raise ValueError("outbound preparer returned a message for another platform")
        if len({item.idempotency_key for item in prepared}) != len(prepared):
            raise ValueError("outbound preparer returned duplicate idempotency keys")
        return prepared

    def _enqueue_outbound(self, message: OutboundMessage) -> tuple[OutboundMessage, ...]:
        prepared = self._prepare_outbound(message)
        for chunk in prepared:
            self.store.enqueue_outbox(chunk)
        return prepared

    def _enqueue_completion_notifications_once(self, *, page_size: int = 500) -> int:
        cursor: tuple[str, Platform | str, str] | None = None
        enqueued = 0
        while True:
            page = self.store.list_linked_inbox(limit=page_size, after=cursor)
            if not page:
                return enqueued
            for inbox in page:
                run_id = inbox.run_id
                if run_id is None:
                    continue
                run = self.service.get(run_id)
                suffix: str | None = None
                approval_ids: str | None = None
                if run.status in _TERMINAL:
                    suffix = "terminal"
                elif run.status is RunStatus.PAUSED:
                    pending = self.service.approvals(run.id, pending_only=True)
                    if pending:
                        approval_ids = ",".join(item.id for item in pending)
                        suffix = f"approval:{approval_ids}"
                if suffix is None:
                    continue
                notification_key = (
                    f"channel:{inbox.envelope.platform.value}:"
                    f"{inbox.envelope.external_event_id}:{suffix}"
                )
                if self.store.notification_exists(notification_key):
                    continue
                if run.status in _TERMINAL:
                    text = f"Run {run.id}: {run.status.value}"
                    if run.status is RunStatus.COMPLETED:
                        output = getattr(self.service.replay(run.id), "final_output", None)
                        if output:
                            text = f"{text}\n{output}"
                else:
                    assert approval_ids is not None
                    text = f"Run {run.id} is paused for approval: {approval_ids}"
                metadata = _thread_metadata(inbox.envelope)
                metadata["run_id"] = run.id
                message = OutboundMessage(
                    platform=inbox.envelope.platform,
                    idempotency_key=notification_key,
                    channel_id=inbox.envelope.channel_id,
                    thread_key=inbox.envelope.thread_key,
                    text=text,
                    metadata=metadata,
                )
                prepared = self._enqueue_outbound(message)
                self.store.record_notification(
                    notification_key,
                    inbox.envelope.platform,
                    inbox.envelope.external_event_id,
                    tuple(item.idempotency_key for item in prepared),
                )
                enqueued += 1
            last = page[-1]
            cursor = (
                last.updated_at,
                last.envelope.platform,
                last.envelope.external_event_id,
            )
            if len(page) < page_size:
                return enqueued

    def enqueue_delivery(
        self,
        target: Mapping[str, Any],
        run_id: str,
    ) -> OutboundMessage:
        platform = Platform(str(target.get("platform", "")))
        channel_id = str(target.get("channel_id", "")).strip()
        if not channel_id:
            raise ValueError("delivery target requires channel_id")
        if platform not in self.adapters:
            raise ValueError(f"{platform.value} delivery is not enabled")
        configured_channels = (
            self.config.channels.telegram.allowed_chat_ids
            if platform is Platform.TELEGRAM
            else self.config.channels.slack.allowed_channel_ids
        )
        allowed_channels = {str(value) for value in configured_channels}
        if channel_id not in allowed_channels:
            raise ValueError(
                f"{platform.value} delivery target is not in the configured allowlist"
            )
        thread_key = str(target.get("thread_key") or f"{platform.value}:{channel_id}")
        run = self.service.get(run_id)
        text = f"Scheduled run {run.id}: {run.status.value}"
        if run.status is RunStatus.COMPLETED:
            output = getattr(self.service.replay(run.id), "final_output", None)
            if output:
                text = f"{text}\n{output}"
        message = OutboundMessage(
            platform=platform,
            idempotency_key=f"scheduler:{run_id}:delivery",
            channel_id=channel_id,
            thread_key=thread_key,
            text=text,
            metadata={
                key: value
                for key, value in target.items()
                if key not in {"platform", "channel_id", "thread_key"}
            },
        )
        self._enqueue_outbound(message)
        return message

    def status(self) -> dict[str, object]:
        return {
            "started": self._started,
            "telegram_enabled": self.config.channels.telegram.enabled,
            "slack_enabled": self.config.channels.slack.enabled,
            "running_tasks": sum(not task.done() for task in self._tasks),
            "failures": list(self._failures),
            "unknown_outbox": len(self.store.list_unknown_outbox()),
        }

    async def close(self) -> None:
        if not self._started and not self._tasks:
            self.store.close()
            return
        tasks = tuple(self._tasks)
        for task in tasks:
            task.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)
        for platform, adapter in reversed(tuple(self.adapters.items())):
            timeout = (
                self.config.channels.telegram.close_timeout
                if platform is Platform.TELEGRAM
                else self.config.channels.slack.close_timeout
            )
            with suppress(Exception, TimeoutError):
                await asyncio.wait_for(adapter.close(), timeout=timeout)
        self.store.close()
        self._started = False
