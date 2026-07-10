from __future__ import annotations

import asyncio
from collections.abc import Mapping, Sequence
from pathlib import Path

import pytest

from polaris.artifacts import ArtifactStore
from polaris.config import AppConfig, ProviderSpec, ToolConfig
from polaris.journal import Budget, Journal, RunStatus, SafetyClass
from polaris.providers import CompletionResult, Message, Provider, ProviderConfig
from polaris.providers.base import JsonValue
from polaris.service import AgentService
from polaris.tools import ToolRegistry


class FakeProvider(Provider):
    def __init__(self) -> None:
        self.config = ProviderConfig("fake-model", "http://127.0.0.1:1")
        self.closed = False

    async def complete(
        self,
        messages: Sequence[Message],
        tools: Sequence[Mapping[str, object]] | None = None,
        response_schema: Mapping[str, object] | None = None,
    ) -> CompletionResult:
        return CompletionResult(Message("assistant", "done"), "fake-model")

    async def list_models(self) -> tuple[str, ...]:
        return ("fake-model",)

    async def doctor(self) -> Mapping[str, JsonValue]:
        return {"ok": True, "model": "fake-model"}

    async def aclose(self) -> None:
        self.closed = True


class FailingProvider(FakeProvider):
    async def complete(
        self,
        messages: Sequence[Message],
        tools: Sequence[Mapping[str, object]] | None = None,
        response_schema: Mapping[str, object] | None = None,
    ) -> CompletionResult:
        raise RuntimeError("provider exploded")

    async def list_models(self) -> tuple[str, ...]:
        raise RuntimeError("models unavailable")

    async def doctor(self) -> Mapping[str, JsonValue]:
        raise RuntimeError("doctor unavailable")


class SlowProvider(FakeProvider):
    def __init__(self) -> None:
        super().__init__()
        self.started = asyncio.Event()

    async def complete(
        self,
        messages: Sequence[Message],
        tools: Sequence[Mapping[str, object]] | None = None,
        response_schema: Mapping[str, object] | None = None,
    ) -> CompletionResult:
        self.started.set()
        await asyncio.Event().wait()
        raise AssertionError("unreachable")


def make_config(root: Path) -> AppConfig:
    provider = ProviderSpec.model_validate(
        {
            "kind": "ollama",
            "model": "fake-model",
            "base_url": "http://127.0.0.1:1",
        }
    )
    return AppConfig(
        data_dir=root,
        providers={"fake": provider},
        tools=ToolConfig(roots=(root,)),
    )


def make_service(root: Path, journal: Journal | None = None) -> tuple[AgentService, FakeProvider]:
    provider = FakeProvider()
    service = AgentService(
        make_config(root),
        journal=journal or Journal(":memory:"),
        artifact_store=ArtifactStore(root / "artifacts"),
        providers={"fake": provider},
        tools=ToolRegistry(),
    )
    return service, provider


@pytest.mark.asyncio
async def test_submit_execute_status_timeline_and_replay(tmp_path: Path) -> None:
    service, provider = make_service(tmp_path)
    run = await service.submit_single("hello", provider="fake", schedule=False)
    assert run.status is RunStatus.CREATED

    await service.resume(run.id)
    await service._tasks[run.id]

    assert service.get(run.id).status is RunStatus.COMPLETED
    assert service.replay(run.id).final_output == "done"  # type: ignore[attr-defined]
    assert any(event.type == "run.created" for event in service.timeline(run.id))
    await service.close()
    assert provider.closed


@pytest.mark.asyncio
async def test_startup_recovers_durable_created_run(tmp_path: Path) -> None:
    journal = Journal(tmp_path / "journal.sqlite3")
    first, _ = make_service(tmp_path, journal)
    run = await first.submit_single("recover me", provider="fake", schedule=False)
    resumed = await first.startup()
    assert resumed == (run.id,)
    await first._tasks[run.id]
    assert first.status(run.id).status is RunStatus.COMPLETED
    await first.close()


@pytest.mark.asyncio
async def test_approval_decision_is_durable(tmp_path: Path) -> None:
    service, _ = make_service(tmp_path)
    run = await service.submit_single("approval", provider="fake", schedule=False)
    approval = service.journal.request_approval(run.id, request={"tool": "write"})
    decided = await service.decide_approval(approval.id, True, schedule=False)

    assert decided.status == "approved"
    assert service.approvals(run.id)[0].decision == "approved"
    await service.close()


@pytest.mark.asyncio
async def test_background_failure_is_journaled_and_provider_errors_are_reported(
    tmp_path: Path,
) -> None:
    provider = FailingProvider()
    service = AgentService(
        make_config(tmp_path),
        journal=Journal(":memory:"),
        artifact_store=ArtifactStore(tmp_path / "artifacts"),
        providers={"fake": provider},
        tools=ToolRegistry(),
    )
    run = await service.submit_single("fail", provider="fake")
    with pytest.raises(RuntimeError, match="provider exploded"):
        await service._tasks[run.id]
    assert service.get(run.id).status is RunStatus.FAILED
    assert service.timeline(run.id)[-1].type == "service.task_failed"
    assert (await service.provider_doctor())["fake"] == {
        "ok": False,
        "error_type": "RuntimeError",
        "message": "doctor unavailable",
    }
    assert await service.models() == {
        "fake": {"error_type": "RuntimeError", "message": "models unavailable"}
    }
    await service.close()


@pytest.mark.asyncio
async def test_cancel_resume_terminal_and_approval_helpers(tmp_path: Path) -> None:
    service, _ = make_service(tmp_path)
    run = await service.submit_single("cancel", provider="fake", schedule=False)
    cancelled = await service.cancel(run.id)
    assert cancelled.status is RunStatus.CANCELLED
    assert (await service.cancel(run.id)).status is RunStatus.CANCELLED
    with pytest.raises(Exception, match="already cancelled"):
        await service.resume(run.id)

    pending_run = await service.submit_single("approve", provider="fake", schedule=False)
    first = service.journal.request_approval(pending_run.id, request={})
    second = service.journal.request_approval(pending_run.id, request={})
    assert (await service.approve(first.id, decided_by="test")).decision == "approved"
    assert (await service.reject(second.id, decided_by="test")).decision == "rejected"
    assert service.tool_names() == ()
    await service.close()


@pytest.mark.asyncio
async def test_cancel_active_task_and_close_is_idempotent(tmp_path: Path) -> None:
    provider = SlowProvider()
    service = AgentService(
        make_config(tmp_path),
        journal=Journal(":memory:"),
        artifact_store=ArtifactStore(tmp_path / "artifacts"),
        providers={"fake": provider},
        tools=ToolRegistry(),
    )
    run = await service.submit_single("wait", provider="fake")
    await provider.started.wait()
    assert (await service.cancel(run.id)).status is RunStatus.CANCELLED
    assert service.timeline(run.id)[-1].type == "service.task_cancelled"
    await service.close()
    await service.close()


@pytest.mark.asyncio
async def test_startup_skips_uncertain_and_missing_provider_runs(tmp_path: Path) -> None:
    service, _ = make_service(tmp_path)
    uncertain = await service.submit_single("unsafe", provider="fake", schedule=False)
    step = service.journal.create_step(
        uncertain.id,
        "tool",
        "side-effect",
        {},
        SafetyClass.OPAQUE_SIDE_EFFECT,
    )
    claimed = service.journal.claim_ready_step("worker", 30, uncertain.id)
    assert claimed is not None and claimed.id == step.id
    service.journal.mark_uncertain(step.id, "worker", "outcome unknown")
    missing = service.journal.create_run(
        "single",
        {"prompt": "missing"},
        {"provider": "not-configured"},
    )
    assert await service.startup() == ()
    assert service.timeline(uncertain.id)[-1].type == "service.recovery_skipped"
    assert service.timeline(missing.id)[-1].type == "service.recovery_skipped"
    with pytest.raises(Exception, match="uncertain opaque"):
        await service.resume(uncertain.id)
    await service.close()


@pytest.mark.asyncio
async def test_default_provider_list_filters_and_artifact_helpers(tmp_path: Path) -> None:
    service, _ = make_service(tmp_path)
    run = await service.submit_single("default", schedule=False)
    service.artifact_store.record_artifact(
        service.journal, run.id, "note.txt", "hello", media_type="text/plain"
    )
    assert service.artifacts(run.id)[0].name == "note.txt"
    assert service.list_runs("created")[0].id == run.id
    assert service.timeline(run.id, after_id=0, limit=1)[0].type == "run.created"
    assert service.approvals(run.id, pending_only=True) == []
    await service.close()


@pytest.mark.asyncio
async def test_submit_foundry_router_uses_thin_routed_strategy(tmp_path: Path) -> None:
    router_spec = ProviderSpec.model_validate(
        {
            "kind": "foundry_router",
            "model": "model-router",
            "base_url": "https://resource.services.ai.azure.com/openai/v1",
            "api_mode": "responses",
            "azure_auth": "entra",
        }
    )
    config = AppConfig(
        data_dir=tmp_path,
        providers={"router": router_spec},
        tools=ToolConfig(roots=(tmp_path,)),
    )
    provider = FakeProvider()
    service = AgentService(
        config,
        journal=Journal(":memory:"),
        artifact_store=ArtifactStore(tmp_path / "artifacts"),
        providers={"router": provider},
        tools=ToolRegistry(),
    )
    run = await service.submit_foundry_router(
        "Route this research",
        provider="router",
        budget=Budget(call_limit=3, token_limit=300),
        schedule=False,
    )
    children = [item for item in service.list_runs() if item.parent_run_id == run.id]
    assert run.mode == "fan-out"
    assert len(children) == 1
    strategy = [
        event
        for event in service.timeline(run.id)
        if event.type == "ensemble.strategy_selected"
    ]
    assert strategy[0].payload["strategy"] == "foundry_model_router"
    await service.close()
