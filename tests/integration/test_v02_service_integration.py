from __future__ import annotations

import asyncio
from collections.abc import Mapping, Sequence
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from polaris.channels import (
    AuthDecision,
    ChannelEnvelope,
    OutboundMessage,
    OutboxStatus,
    Platform,
    RemoteReceipt,
    chunk_outbound,
)
from polaris.config import (
    AppConfig,
    ChannelsConfig,
    MemoryConfig,
    ProviderSpec,
    SchedulerConfig,
    SlackConfig,
    TelegramConfig,
    ToolConfig,
)
from polaris.journal import RunStatus
from polaris.memory import MemoryConflictError, MemoryScope
from polaris.providers import CompletionResult, Message, Provider, ProviderConfig, ToolCall
from polaris.providers.base import JsonValue
from polaris.scheduler import JobPayload, JobRunStatus, ScheduleSpec
from polaris.service import AgentService
from polaris.tools import SafetyClass, ToolArguments, ToolEntry, ToolRegistry


class FakeProvider(Provider):
    def __init__(self, *, blocked: bool = False) -> None:
        self.config = ProviderConfig("fake-model", "http://127.0.0.1:1")
        self.messages: list[Sequence[Message]] = []
        self.tools: list[Sequence[Mapping[str, object]] | None] = []
        self.started = asyncio.Event()
        self.release = asyncio.Event()
        self.blocked = blocked

    async def complete(
        self,
        messages: Sequence[Message],
        tools: Sequence[Mapping[str, object]] | None = None,
        response_schema: Mapping[str, object] | None = None,
    ) -> CompletionResult:
        self.messages.append(messages)
        self.tools.append(tools)
        self.started.set()
        if self.blocked:
            await self.release.wait()
        return CompletionResult(Message("assistant", "done"), "fake-model")

    async def list_models(self) -> tuple[str, ...]:
        return ("fake-model",)

    async def doctor(self) -> Mapping[str, JsonValue]:
        return {"ok": True}

    async def aclose(self) -> None:
        return None


class ApprovalProvider(FakeProvider):
    def __init__(self) -> None:
        super().__init__()
        self.call_count = 0

    async def complete(
        self,
        messages: Sequence[Message],
        tools: Sequence[Mapping[str, object]] | None = None,
        response_schema: Mapping[str, object] | None = None,
    ) -> CompletionResult:
        self.call_count += 1
        if self.call_count == 1:
            return CompletionResult(
                Message(
                    "assistant",
                    None,
                    tool_calls=(ToolCall("call-1", "approval_tool", {}),),
                ),
                "fake-model",
            )
        return CompletionResult(Message("assistant", "done"), "fake-model")


class FakeAdapter:
    def __init__(self, platform: Platform) -> None:
        self.platform = platform
        self.connected = False
        self.closed = False
        self.sent: list[OutboundMessage] = []
        self._poll_task: asyncio.Task[None] | None = None

    async def connect(self) -> None:
        self.connected = True

    async def close(self) -> None:
        self.closed = True

    async def send(self, message: OutboundMessage) -> RemoteReceipt:
        self.sent.append(message)
        return RemoteReceipt(
            self.platform,
            message.idempotency_key,
            str(len(self.sent)),
            message.channel_id,
            message.operation,
        )

    async def _poll(self) -> None:
        await asyncio.Event().wait()

    def start_polling(self) -> asyncio.Task[None]:
        self._poll_task = asyncio.create_task(self._poll())
        return self._poll_task


def provider_spec() -> ProviderSpec:
    return ProviderSpec.model_validate(
        {
            "kind": "ollama",
            "model": "fake-model",
            "base_url": "http://127.0.0.1:1",
        }
    )


def config(
    root: Path,
    *,
    channels: ChannelsConfig | None = None,
    scheduler_enabled: bool = False,
) -> AppConfig:
    return AppConfig(
        data_dir=root,
        providers={"fake": provider_spec()},
        tools=ToolConfig(roots=(root,)),
        memory=MemoryConfig(profile_id="profile"),
        scheduler=SchedulerConfig(enabled=scheduler_enabled, tick_seconds=0.05),
        channels=channels or ChannelsConfig(),
    )


async def wait_until(predicate: object, timeout: float = 2) -> None:
    check = predicate
    assert callable(check)
    async with asyncio.timeout(timeout):
        while not check():
            await asyncio.sleep(0.01)


@pytest.mark.asyncio
async def test_memory_snapshot_is_frozen_and_scope_tools_restart(tmp_path: Path) -> None:
    provider = FakeProvider()
    first = AgentService(config(tmp_path), providers={"fake": provider}, tools=ToolRegistry())
    scope = MemoryScope("profile", "local")
    first.memory_add(scope, "favorite color is blue")
    run = await first.submit_single(
        "remember",
        provider="fake",
        schedule=False,
        memory_scope=scope,
    )
    frozen_context = run.config["memory_context"]
    frozen_hash = run.config["memory_snapshot_hash"]
    first.memory_add(scope, "later write")

    assert "favorite color is blue" in frozen_context
    assert "later write" not in frozen_context
    assert first.get(run.id).config["memory_snapshot_hash"] == frozen_hash
    runtime = first._runtime("fake", run.config)
    definitions = runtime.tools.get_definitions(names=("memory_search",))
    assert "profile_id" not in str(definitions)
    result = await runtime.tools.execute("memory_search", {"query": "favorite"})
    assert isinstance(result, dict)
    hits = result["hits"]
    assert isinstance(hits, list) and isinstance(hits[0], dict)
    assert hits[0]["content"] == "favorite color is blue"
    await first.close()

    restarted = AgentService(
        config(tmp_path),
        providers={"fake": FakeProvider()},
        tools=ToolRegistry(),
    )
    persisted = restarted.get(run.id)
    assert persisted.config["memory_context"] == frozen_context
    restarted_runtime = restarted._runtime("fake", persisted.config)
    assert "memory_search" in restarted_runtime.tools.names()
    assert (
        restarted_runtime._initial_messages(persisted)[-1].content
        == f"remember\n\n{frozen_context}"
    )
    await restarted.close()


@pytest.mark.asyncio
async def test_channel_memory_add_is_idempotent_per_event(tmp_path: Path) -> None:
    service = AgentService(
        config(tmp_path),
        providers={"fake": FakeProvider()},
        tools=ToolRegistry(),
    )
    envelope = ChannelEnvelope(
        Platform.TELEGRAM,
        "3:42",
        "message",
        "7",
        "100",
        "telegram:100",
        "telegram:update:3:42",
        text="/memory add remember this",
        message_id="9",
        action="command",
    )

    first = await service.channels.router.handle(envelope)
    repeated = await service.channels.router.handle(envelope)
    entries = service.memory_list(MemoryScope("profile", "telegram:7"))

    assert first[0].text == repeated[0].text
    assert len(entries) == 1
    with pytest.raises(MemoryConflictError, match="different memory content"):
        await service.channels.router.handle(
            ChannelEnvelope(
                Platform.TELEGRAM,
                "3:42",
                "message",
                "7",
                "100",
                "telegram:100",
                "telegram:update:3:42",
                text="/memory add conflicting retry",
                message_id="9",
                action="command",
            )
        )
    assert len(service.memory_list(MemoryScope("profile", "telegram:7"))) == 1
    await service.close()


@pytest.mark.asyncio
async def test_scheduler_waits_for_agent_and_enqueues_delivery(tmp_path: Path) -> None:
    provider = FakeProvider(blocked=True)
    selected, env = channel_config(tmp_path, Platform.TELEGRAM)
    service = AgentService(
        selected,
        providers={"fake": provider},
        tools=ToolRegistry(),
        env=env,
        telegram_adapter=FakeAdapter(Platform.TELEGRAM),
    )
    job = service.create_job(
        ScheduleSpec.once(datetime.now(UTC) - timedelta(seconds=1)),
        JobPayload.single(
            {"prompt": "scheduled", "provider": "fake"},
            {"platform": "telegram", "channel_id": "100"},
        ),
        name="one",
    )
    ticking = asyncio.create_task(service.scheduler.tick())
    await provider.started.wait()
    active = service.list_job_runs(job_id=job.id)
    assert active[0].status is JobRunStatus.RUNNING
    assert active[0].polaris_run_id is not None
    provider.release.set()
    await ticking
    await service.scheduler.drain()

    completed = service.list_job_runs(job_id=job.id)[0]
    assert completed.status is JobRunStatus.SUCCEEDED
    assert completed.polaris_run_id is not None
    assert completed.delivery_status.value == "succeeded"
    deliveries = service.channel_store.list_outbox()
    assert deliveries[0].status is OutboxStatus.PENDING
    assert deliveries[0].message.idempotency_key.endswith(":delivery")
    await service.close()


@pytest.mark.asyncio
async def test_paused_scheduled_agent_resumes_to_successful_delivery(tmp_path: Path) -> None:
    async def approval_tool(_arguments: ToolArguments) -> str:
        return "approved"

    tools = ToolRegistry()
    tools.register(
        ToolEntry(
            "approval_tool",
            "test",
            {"type": "object", "properties": {}},
            approval_tool,
            safety_class=SafetyClass.OPAQUE_SIDE_EFFECT,
        )
    )
    selected, env = channel_config(tmp_path, Platform.TELEGRAM)
    service = AgentService(
        selected,
        providers={"fake": ApprovalProvider()},
        tools=tools,
        env=env,
        telegram_adapter=FakeAdapter(Platform.TELEGRAM),
    )
    job = service.create_job(
        ScheduleSpec.once(datetime.now(UTC) - timedelta(seconds=1)),
        JobPayload.single(
            {"prompt": "scheduled approval", "provider": "fake"},
            {"platform": "telegram", "channel_id": "100"},
        ),
    )
    ticking = asyncio.create_task(service.scheduler.tick())
    await wait_until(
        lambda: bool(service.list_job_runs(job_id=job.id))
        and service.list_job_runs(job_id=job.id)[0].polaris_run_id is not None
    )
    occurrence = service.list_job_runs(job_id=job.id)[0]
    assert occurrence.polaris_run_id is not None
    await wait_until(
        lambda: service.get(occurrence.polaris_run_id or "").status is RunStatus.PAUSED
    )
    assert ticking.done()

    approval = service.approvals(occurrence.polaris_run_id, pending_only=True)[0]
    await service.approve(approval.id)
    await ticking
    await service.scheduler.drain()

    completed = service.list_job_runs(job_id=job.id)[0]
    assert completed.status is JobRunStatus.SUCCEEDED
    assert completed.delivery_status.value == "succeeded"
    assert len(service.channel_store.list_outbox()) == 1
    await service.close()


@pytest.mark.asyncio
async def test_cancel_running_scheduled_job_cancels_agent_and_delivery(tmp_path: Path) -> None:
    provider = FakeProvider(blocked=True)
    selected, env = channel_config(tmp_path, Platform.TELEGRAM)
    service = AgentService(
        selected,
        providers={"fake": provider},
        tools=ToolRegistry(),
        env=env,
        telegram_adapter=FakeAdapter(Platform.TELEGRAM),
    )
    job = service.create_job(
        ScheduleSpec.once(datetime.now(UTC) - timedelta(seconds=1)),
        JobPayload.single(
            {"prompt": "cancel scheduled", "provider": "fake"},
            {"platform": "telegram", "channel_id": "100"},
        ),
    )
    ticking = asyncio.create_task(service.scheduler.tick())
    await provider.started.wait()
    occurrence = service.list_job_runs(job_id=job.id)[0]
    assert occurrence.polaris_run_id is not None

    service.cancel_job(job.id)
    requested = service.list_job_runs(job_id=job.id)[0]
    assert requested.cancel_requested
    await ticking
    await service.scheduler.drain()

    cancelled = service.list_job_runs(job_id=job.id)[0]
    assert cancelled.status is JobRunStatus.CANCELLED
    assert cancelled.delivery_status.value == "suppressed"
    assert service.get(occurrence.polaris_run_id).status is RunStatus.CANCELLED
    assert service.channel_store.list_outbox() == ()
    await service.close()


@pytest.mark.asyncio
async def test_scheduled_delivery_revalidates_enabled_channel_allowlist(
    tmp_path: Path,
) -> None:
    selected, env = channel_config(tmp_path, Platform.TELEGRAM)
    adapter = FakeAdapter(Platform.TELEGRAM)
    service = AgentService(
        selected,
        providers={"fake": FakeProvider()},
        tools=ToolRegistry(),
        env=env,
        telegram_adapter=adapter,
    )
    run = await service.submit_single("delivery", provider="fake", schedule=False)
    await service._execute(run.id)

    allowed = service.channels.enqueue_delivery(
        {"platform": "telegram", "channel_id": "100"},
        run.id,
    )
    assert allowed.channel_id == "100"
    with pytest.raises(ValueError, match="allowlist"):
        service.channels.enqueue_delivery(
            {"platform": "telegram", "channel_id": "not-allowed"},
            run.id,
        )
    with pytest.raises(ValueError, match="not enabled"):
        service.channels.enqueue_delivery(
            {"platform": "slack", "channel_id": "100"},
            run.id,
        )
    await service.close()


def channel_config(root: Path, platform: Platform) -> tuple[AppConfig, dict[str, str]]:
    if platform is Platform.TELEGRAM:
        channels = ChannelsConfig(
            telegram=TelegramConfig(
                enabled=True,
                token_env="TELEGRAM_TOKEN",
                allowed_user_ids=("7",),
                allowed_chat_ids=("100",),
                default_provider="fake",
            )
        )
        env = {"TELEGRAM_TOKEN": "telegram-secret"}
    else:
        channels = ChannelsConfig(
            slack=SlackConfig(
                enabled=True,
                bot_token_env="SLACK_BOT",
                app_token_env="SLACK_APP",
                allowed_user_ids=("7",),
                allowed_channel_ids=("100",),
                default_provider="fake",
            )
        )
        env = {"SLACK_BOT": "xoxb-secret", "SLACK_APP": "xapp-secret"}
    return config(root, channels=channels), env


@pytest.mark.asyncio
@pytest.mark.parametrize("platform", [Platform.TELEGRAM, Platform.SLACK])
async def test_channel_message_run_and_restart_are_idempotent(
    tmp_path: Path, platform: Platform
) -> None:
    selected, env = channel_config(tmp_path, platform)
    adapter = FakeAdapter(platform)
    if platform is Platform.TELEGRAM:
        service = AgentService(
            selected,
            providers={"fake": FakeProvider()},
            tools=ToolRegistry(),
            env=env,
            telegram_adapter=adapter,
        )
    else:
        service = AgentService(
            selected,
            providers={"fake": FakeProvider()},
            tools=ToolRegistry(),
            env=env,
            slack_adapter=adapter,
        )
    await service.startup()
    envelope = ChannelEnvelope(
        platform,
        "event-1",
        "message",
        "7",
        "100",
        f"{platform.value}:100:thread",
        f"{platform.value}:event:event-1",
        text="/run hello",
        message_id="1",
        action="command" if platform is Platform.TELEGRAM else "message",
        metadata={"thread_ts": "thread"} if platform is Platform.SLACK else {},
    )
    assert service.channel_store.ingest_envelope(envelope, {"text": "/run hello"}).accepted
    await wait_until(lambda: len(adapter.sent) >= 2)
    assert len(service.list_runs()) == 1
    assert adapter.sent[0].metadata["run_id"] == service.list_runs()[0].id
    assert adapter.sent[-1].thread_key == envelope.thread_key

    duplicate = service.channel_store.ingest_envelope(envelope, {"text": "/run hello"})
    assert duplicate.duplicate
    await asyncio.sleep(0.1)
    assert len(service.list_runs()) == 1
    await service.close()

    restarted_adapter = FakeAdapter(platform)
    if platform is Platform.TELEGRAM:
        restarted = AgentService(
            selected,
            providers={"fake": FakeProvider()},
            tools=ToolRegistry(),
            env=env,
            telegram_adapter=restarted_adapter,
        )
    else:
        restarted = AgentService(
            selected,
            providers={"fake": FakeProvider()},
            tools=ToolRegistry(),
            env=env,
            slack_adapter=restarted_adapter,
        )
    await restarted.startup()
    await asyncio.sleep(0.35)
    assert restarted_adapter.sent == []
    assert len(restarted.list_runs()) == 1
    await restarted.close()


@pytest.mark.asyncio
async def test_approval_authorization_monitor_and_unknown_resolution(tmp_path: Path) -> None:
    selected, env = channel_config(tmp_path, Platform.TELEGRAM)
    adapter = FakeAdapter(Platform.TELEGRAM)
    service = AgentService(
        selected,
        providers={"fake": FakeProvider()},
        tools=ToolRegistry(),
        env=env,
        telegram_adapter=adapter,
    )
    message = OutboundMessage(
        Platform.TELEGRAM,
        "unknown-1",
        "100",
        "telegram:100",
        "uncertain",
    )
    service.channel_store.enqueue_outbox(message)
    claimed = service.channel_store.claim_outbox("operator", platform=Platform.TELEGRAM)
    assert claimed is not None
    service.channel_store.mark_outbox_unknown("unknown-1", "operator", "timeout")
    assert service.unknown_outbox()[0].message.idempotency_key == "unknown-1"
    assert service.mark_outbox_sent("unknown-1", note="verified remotely").status.value == "sent"

    run = await service.submit_single("approval", provider="fake", schedule=False)
    service.journal.mark_run_status(run.id, "running")
    approval = service.journal.request_approval(run.id, request={"tool": "write"})
    service.journal.mark_run_status(run.id, "paused")
    await service.startup()

    callback = ChannelEnvelope(
        Platform.TELEGRAM,
        "callback-1",
        "callback_query",
        "7",
        "100",
        "telegram:100",
        "telegram:event:callback-1",
        callback_query_id="query-1",
        callback_data=f"approve:{approval.id}",
        action="callback",
    )
    service.channel_store.ingest_envelope(callback, {"callback": approval.id})
    await wait_until(lambda: service.journal.get_approval(approval.id).status == "approved")
    assert service.journal.get_approval(approval.id).decided_by == "telegram:7"

    unauthorized = ChannelEnvelope(
        Platform.TELEGRAM,
        "callback-2",
        "callback_query",
        "8",
        "100",
        "telegram:100",
        "telegram:event:callback-2",
        callback_data=f"deny:{approval.id}",
        action="callback",
    )
    result = service.channel_store.ingest_envelope(unauthorized, {"callback": approval.id})
    assert not result.accepted
    await service.close()


def test_completion_monitor_pages_past_500_and_records_notifications(
    tmp_path: Path,
) -> None:
    service = AgentService(
        config(tmp_path),
        providers={"fake": FakeProvider()},
        tools=ToolRegistry(),
    )
    for index in range(501):
        run = service.journal.create_run(
            "single",
            {"prompt": str(index)},
            {"provider": "fake"},
        )
        service.journal.mark_run_status(run.id, RunStatus.FAILED)
        envelope = ChannelEnvelope(
            Platform.TELEGRAM,
            f"event-{index:03d}",
            "message",
            "7",
            "100",
            "telegram:100",
            f"telegram:event:event-{index:03d}",
            text="run",
        )
        service.channel_store.ingest_envelope(
            envelope,
            {"index": index},
            decision=AuthDecision.ALLOW,
            reason="test fixture",
        )
        claimed = service.channel_store.claim_inbox("fixture")
        assert claimed is not None
        service.channel_store.link_inbox_run(
            envelope.platform,
            envelope.external_event_id,
            envelope.downstream_key,
            run.id,
        )
        service.channel_store.complete_inbox(
            envelope.platform,
            envelope.external_event_id,
            "fixture",
            run_id=run.id,
        )

    assert service.channels._enqueue_completion_notifications_once() == 501
    outbox = service.channel_store.list_outbox(limit=1000)
    assert len(outbox) == 501
    assert any("event-500:terminal" in item.message.idempotency_key for item in outbox)
    assert service.channels._enqueue_completion_notifications_once() == 0
    asyncio.run(service.close())


@pytest.mark.asyncio
async def test_completion_monitor_persists_prepared_chunks(tmp_path: Path) -> None:
    class ChunkingAdapter(FakeAdapter):
        def prepare_outbound(self, message: OutboundMessage) -> list[OutboundMessage]:
            return chunk_outbound(message, limit=20)

    selected, env = channel_config(tmp_path, Platform.TELEGRAM)
    service = AgentService(
        selected,
        providers={"fake": FakeProvider()},
        tools=ToolRegistry(),
        env=env,
        telegram_adapter=ChunkingAdapter(Platform.TELEGRAM),
    )
    run = await service.submit_single("completion", provider="fake", schedule=False)
    await service._execute(run.id)
    envelope = ChannelEnvelope(
        Platform.TELEGRAM,
        "completion-event",
        "message",
        "7",
        "100",
        "telegram:100",
        "telegram:event:completion-event",
        text="/run completion",
    )
    assert service.channel_store.ingest_envelope(
        envelope, {"text": envelope.text}
    ).accepted
    assert service.channel_store.claim_inbox("fixture") is not None
    service.channel_store.link_inbox_run(
        envelope.platform,
        envelope.external_event_id,
        envelope.downstream_key,
        run.id,
    )
    service.channel_store.complete_inbox(
        envelope.platform,
        envelope.external_event_id,
        "fixture",
        run_id=run.id,
    )

    assert service.channels._enqueue_completion_notifications_once() == 1
    chunks = service.channel_store.list_outbox(limit=100)
    assert len(chunks) > 1
    assert all(item.status is OutboxStatus.PENDING for item in chunks)
    assert all(":terminal:chunk:" in item.message.idempotency_key for item in chunks)

    def replay_must_not_run(_run_id: str) -> object:
        pytest.fail("already-notified completed runs must not be replayed")

    service.replay = replay_must_not_run  # type: ignore[method-assign,assignment]
    assert service.channels._enqueue_completion_notifications_once() == 0
    await service.close()


@pytest.mark.asyncio
async def test_scheduled_chunks_survive_partial_failure_without_resending_success(
    tmp_path: Path,
) -> None:
    class KnownDeliveryFailure(RuntimeError):
        delivery_unknown = False

    class ChunkingAdapter(FakeAdapter):
        def __init__(self) -> None:
            super().__init__(Platform.TELEGRAM)
            self.failed_once = False

        def prepare_outbound(self, message: OutboundMessage) -> list[OutboundMessage]:
            return chunk_outbound(message, limit=20)

        async def send(self, message: OutboundMessage) -> RemoteReceipt:
            if message.chunk_index == 1 and not self.failed_once:
                self.failed_once = True
                raise KnownDeliveryFailure("definitely not delivered")
            return await super().send(message)

    channels = ChannelsConfig(
        telegram=TelegramConfig(
            enabled=True,
            token_env="TELEGRAM_TOKEN",
            allowed_user_ids=(7,),
            allowed_chat_ids=(100,),
            default_provider="fake",
        )
    )
    selected = config(tmp_path, channels=channels)
    adapter = ChunkingAdapter()
    service = AgentService(
        selected,
        providers={"fake": FakeProvider()},
        tools=ToolRegistry(),
        env={"TELEGRAM_TOKEN": "secret"},
        telegram_adapter=adapter,
    )
    run = await service.submit_single("scheduled", provider="fake", schedule=False)
    await service._execute(run.id)
    service.channels.enqueue_delivery(
        {"platform": "telegram", "channel_id": "100"},
        run.id,
    )
    chunks = service.channel_store.list_outbox(limit=100)
    assert len(chunks) > 1
    first_key = chunks[0].message.idempotency_key
    failed_key = chunks[1].message.idempotency_key
    processor = service.channels.processors[Platform.TELEGRAM]
    assert await processor.send_outbox_once()
    with pytest.raises(KnownDeliveryFailure):
        await processor.send_outbox_once()
    assert service.channel_store.get_outbox(first_key).status is OutboxStatus.SENT  # type: ignore[union-attr]
    assert service.channel_store.get_outbox(failed_key).status is OutboxStatus.FAILED  # type: ignore[union-attr]
    await service.close()

    restarted = AgentService(
        selected,
        providers={"fake": FakeProvider()},
        tools=ToolRegistry(),
        env={"TELEGRAM_TOKEN": "secret"},
        telegram_adapter=adapter,
    )
    restarted.channel_store.retry_outbox(failed_key, note="known failure was not delivered")
    restarted_processor = restarted.channels.processors[Platform.TELEGRAM]
    while await restarted_processor.send_outbox_once():
        pass
    sent_keys = [item.idempotency_key for item in adapter.sent]
    assert sent_keys.count(first_key) == 1
    assert set(sent_keys) == {item.message.idempotency_key for item in chunks}
    await restarted.close()
