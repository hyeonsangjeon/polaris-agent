from __future__ import annotations

from collections.abc import Mapping, Sequence
from pathlib import Path
from time import sleep

import pytest

from polaris.journal import Journal, RunStatus, StepStatus
from polaris.providers import (
    CompletionResult,
    Message,
    Provider,
    ProviderConfig,
    ToolCall,
)
from polaris.runtime import AgentRuntime, DefaultApprovalPolicy, RuntimeConfig
from polaris.tools import SafetyClass, ToolEntry, ToolRegistry
from polaris.tools.registry import JsonValue

LEASE_SECONDS = 0.2
LEASE_EXPIRY_WAIT = 0.3


class RecoveryProvider(Provider):
    def __init__(self) -> None:
        self.config = ProviderConfig(model="requested", base_url="http://localhost")
        self.calls = 0

    async def complete(
        self,
        messages: Sequence[Message],
        tools: Sequence[Mapping[str, JsonValue]] | None = None,
        response_schema: Mapping[str, JsonValue] | None = None,
    ) -> CompletionResult:
        self.calls += 1
        if self.calls == 1:
            return CompletionResult(
                Message(
                    "assistant",
                    "",
                    tool_calls=(ToolCall("call", "side_effect", {"id": "job"}),),
                ),
                "actual",
            )
        return CompletionResult(Message("assistant", "recovered"), "actual")

    async def list_models(self) -> tuple[str, ...]:
        return ()

    async def doctor(self) -> Mapping[str, JsonValue]:
        return {}

    async def aclose(self) -> None:
        return None


def make_agent(
    journal: Journal,
    provider: RecoveryProvider,
    handler: object,
    safety: SafetyClass,
    reconcile: object = None,
) -> AgentRuntime:
    tools = ToolRegistry()
    tools.register(
        ToolEntry(
            "side_effect",
            "test",
            {"parameters": {"type": "object"}},
            handler,  # type: ignore[arg-type]
            safety_class=safety,
            reconcile_handler=reconcile,  # type: ignore[arg-type]
        )
    )
    return AgentRuntime(
        journal,
        provider,
        "fake",
        tools,
        RuntimeConfig(
            lease_seconds=LEASE_SECONDS,
            approval_policy=DefaultApprovalPolicy(frozenset({"side_effect"})),
        ),
    )


@pytest.mark.asyncio
async def test_read_only_tool_retries_after_expired_executing_lease(
    tmp_path: Path,
) -> None:
    provider = RecoveryProvider()
    executions = 0

    async def handler(arguments: Mapping[str, JsonValue]) -> JsonValue:
        nonlocal executions
        executions += 1
        if executions == 1:
            raise SystemExit("crash")
        return "retried"

    journal = Journal(tmp_path / "read.sqlite3")
    agent = make_agent(journal, provider, handler, SafetyClass.READ_ONLY)
    run = agent.create_run("recover")
    with pytest.raises(SystemExit):
        await agent.execute(run.id)
    sleep(LEASE_EXPIRY_WAIT)
    assert run.id in agent.recover()
    result = await agent.execute(run.id)

    assert result.status is RunStatus.COMPLETED
    assert executions == 2
    assert provider.calls == 2
    journal.close()


@pytest.mark.asyncio
async def test_reconcilable_tool_reconcile_avoids_duplicate_execute(
    tmp_path: Path,
) -> None:
    provider = RecoveryProvider()
    executions = 0
    reconciliations = 0

    async def handler(arguments: Mapping[str, JsonValue]) -> JsonValue:
        nonlocal executions
        executions += 1
        raise SystemExit("after external side effect")

    async def reconcile(arguments: Mapping[str, JsonValue]) -> JsonValue:
        nonlocal reconciliations
        reconciliations += 1
        return {"found": True, "result": {"id": arguments["id"]}}

    journal = Journal(tmp_path / "reconcile.sqlite3")
    agent = make_agent(
        journal,
        provider,
        handler,
        SafetyClass.RECONCILABLE,
        reconcile,
    )
    run = agent.create_run("recover")
    with pytest.raises(SystemExit):
        await agent.execute(run.id)
    sleep(LEASE_EXPIRY_WAIT)
    result = await agent.execute(run.id)

    assert result.status is RunStatus.COMPLETED
    assert executions == 1
    assert reconciliations == 1
    assert any(event.type == "tool.reconciled" for event in journal.list_events(run.id))
    assert any(
        step.kind == "reconcile" and step.status is StepStatus.COMMITTED
        for step in journal.list_steps(run.id)
    )
    replay = agent.replay(run.id)
    assert replay.messages[-2] == Message(
        "tool", '{"id":"job"}', name="side_effect", tool_call_id="call"
    )
    assert executions == 1
    assert reconciliations == 1
    journal.close()


@pytest.mark.asyncio
async def test_opaque_executing_crash_becomes_uncertain_and_pauses(
    tmp_path: Path,
) -> None:
    provider = RecoveryProvider()
    executions = 0

    async def handler(arguments: Mapping[str, JsonValue]) -> JsonValue:
        nonlocal executions
        executions += 1
        if executions == 1:
            raise SystemExit("unknown outcome")
        return "explicitly retried"

    journal = Journal(tmp_path / "opaque.sqlite3")
    agent = make_agent(journal, provider, handler, SafetyClass.OPAQUE_SIDE_EFFECT)
    run = agent.create_run("recover")
    with pytest.raises(SystemExit):
        await agent.execute(run.id)
    sleep(LEASE_EXPIRY_WAIT)
    result = await agent.execute(run.id)

    tool_step = next(step for step in journal.list_steps(run.id) if step.kind == "tool")
    assert result.status is RunStatus.PAUSED
    assert tool_step.status is StepStatus.UNCERTAIN
    approvals = journal.list_pending_approvals(run.id)
    assert approvals[-1].kind == "uncertain_outcome"
    journal.decide_approval(approvals[-1].id, True, "operator")
    resumed = await agent.execute(run.id)
    assert resumed.status is RunStatus.COMPLETED
    assert executions == 2
    replay = agent.replay(run.id)
    assert replay.messages[-2] == Message(
        "tool", '"explicitly retried"', name="side_effect", tool_call_id="call"
    )
    assert executions == 2
    journal.close()


@pytest.mark.asyncio
async def test_opaque_retry_crashing_twice_requires_fresh_approval(
    tmp_path: Path,
) -> None:
    provider = RecoveryProvider()
    executions = 0

    async def handler(arguments: Mapping[str, JsonValue]) -> JsonValue:
        nonlocal executions
        executions += 1
        if executions <= 2:
            raise SystemExit(f"unknown outcome {executions}")
        return "third attempt approved"

    journal = Journal(tmp_path / "opaque-twice.sqlite3")
    agent = make_agent(journal, provider, handler, SafetyClass.OPAQUE_SIDE_EFFECT)
    run = agent.create_run("recover twice")

    with pytest.raises(SystemExit):
        await agent.execute(run.id)
    sleep(LEASE_EXPIRY_WAIT)
    first_pause = await agent.execute(run.id)
    first_approval = journal.list_pending_approvals(run.id)[0]
    assert first_pause.status is RunStatus.PAUSED
    journal.decide_approval(first_approval.id, True, "operator")

    with pytest.raises(SystemExit):
        await agent.execute(run.id)
    sleep(LEASE_EXPIRY_WAIT)
    second_pause = await agent.execute(run.id)
    second_approval = journal.list_pending_approvals(run.id)[0]
    assert second_pause.status is RunStatus.PAUSED
    assert second_approval.kind == "uncertain_outcome"
    assert second_approval.step_id != first_approval.step_id
    assert executions == 2
    journal.decide_approval(second_approval.id, True, "operator")

    resumed = await agent.execute(run.id)
    tool_steps = [step for step in journal.list_steps(run.id) if step.kind == "tool"]
    assert resumed.status is RunStatus.COMPLETED
    assert executions == 3
    assert tool_steps[1].input["recovery_of"] == tool_steps[0].id
    assert tool_steps[2].input["recovery_of"] == tool_steps[1].id
    assert agent.replay(run.id).messages[-2].content == '"third attempt approved"'
    journal.close()
