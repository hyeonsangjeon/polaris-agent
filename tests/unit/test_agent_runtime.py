from __future__ import annotations

from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any

import pytest

from polaris.journal import Budget, Journal, RunStatus, StepStatus
from polaris.providers import (
    CompletionResult,
    Message,
    Provider,
    ProviderConfig,
    ProviderError,
    ToolCall,
    Usage,
)
from polaris.runtime import (
    AgentRuntime,
    DefaultApprovalPolicy,
    RuntimeConfig,
    deserialize_completion,
    deserialize_message,
    deserialize_tool_call,
    recorded_replay,
    serialize_completion,
    serialize_message,
    serialize_tool_call,
)
from polaris.tools import SafetyClass, ToolEntry, ToolRegistry
from polaris.tools.registry import JsonValue


class FakeProvider(Provider):
    def __init__(self, responses: Sequence[CompletionResult | BaseException]) -> None:
        self.config = ProviderConfig(model="requested", base_url="http://localhost")
        self.responses = list(responses)
        self.calls: list[tuple[Message, ...]] = []

    async def complete(
        self,
        messages: Sequence[Message],
        tools: Sequence[Mapping[str, JsonValue]] | None = None,
        response_schema: Mapping[str, JsonValue] | None = None,
    ) -> CompletionResult:
        self.calls.append(tuple(messages))
        response = self.responses.pop(0)
        if isinstance(response, BaseException):
            raise response
        return response

    async def list_models(self) -> tuple[str, ...]:
        return ("requested",)

    async def doctor(self) -> Mapping[str, JsonValue]:
        return {"ok": True}

    async def aclose(self) -> None:
        return None


def completion(
    text: str = "",
    *,
    calls: tuple[ToolCall, ...] = (),
    model: str = "actual",
    prompt_tokens: int = 2,
    completion_tokens: int = 3,
) -> CompletionResult:
    return CompletionResult(
        Message("assistant", text, tool_calls=calls),
        model,
        Usage(
            prompt_tokens=prompt_tokens,
            completion_tokens=completion_tokens,
            total_tokens=prompt_tokens + completion_tokens,
        ),
        finish_reason="stop",
        response_id="response-1",
    )


def registry(
    handler: Any,
    *,
    safety: SafetyClass = SafetyClass.READ_ONLY,
    reconcile_handler: Any = None,
) -> ToolRegistry:
    tools = ToolRegistry()
    tools.register(
        ToolEntry(
            "echo",
            "test",
            {"parameters": {"type": "object"}},
            handler,
            safety_class=safety,
            reconcile_handler=reconcile_handler,
        )
    )
    return tools


def runtime(
    tmp_path: Path,
    provider: FakeProvider,
    tools: ToolRegistry | None = None,
    *,
    config: RuntimeConfig | None = None,
    estimator: Any = None,
) -> tuple[AgentRuntime, Journal]:
    journal = Journal(tmp_path / "runtime.sqlite3")
    return (
        AgentRuntime(
            journal,
            provider,
            "fake",
            tools or ToolRegistry(),
            config,
            estimator,
        ),
        journal,
    )


@pytest.mark.asyncio
async def test_no_tool_completion_records_model_usage_and_actual_model(
    tmp_path: Path,
) -> None:
    provider = FakeProvider([completion("done", model="actual-v2")])
    agent, journal = runtime(tmp_path, provider)
    result = await agent.run("hello")

    assert result.status is RunStatus.COMPLETED
    assert result.final_text == "done"
    assert result.actual_models == ("actual-v2",)
    assert result.usage.total_tokens == 5
    run = journal.get_run(result.run_id)
    assert run.config["provider"] == "fake"
    assert run.config["model"] == "requested"
    assert journal.list_provider_calls(result.run_id)[0].model == "actual-v2"
    journal.close()


@pytest.mark.asyncio
async def test_tool_roundtrip_reuses_committed_steps_on_resume(tmp_path: Path) -> None:
    executions: list[dict[str, JsonValue]] = []

    async def echo(arguments: Mapping[str, JsonValue]) -> JsonValue:
        executions.append(dict(arguments))
        return {"value": arguments["value"]}

    call = ToolCall("call-1", "echo", {"value": 7})
    provider = FakeProvider([completion(calls=(call,)), completion("finished")])
    agent, journal = runtime(tmp_path, provider, registry(echo))
    result = await agent.run("use a tool")
    resumed = await agent.execute(result.run_id)
    replay = agent.replay(result.run_id)

    assert result.final_text == resumed.final_text == "finished"
    assert executions == [{"value": 7}]
    assert len(provider.calls) == 2
    assert provider.calls[1][-1] == Message(
        "tool", '{"value":7}', name="echo", tool_call_id="call-1"
    )
    assert replay.messages[-2] == Message(
        "tool", '{"value":7}', name="echo", tool_call_id="call-1"
    )
    assert executions == [{"value": 7}]
    assert len(provider.calls) == 2
    assert [step.status for step in journal.list_steps(result.run_id)] == [
        StepStatus.COMMITTED,
        StepStatus.COMMITTED,
        StepStatus.COMMITTED,
    ]
    journal.close()


@pytest.mark.asyncio
async def test_durable_approval_pending_then_rejected_cancels_run(tmp_path: Path) -> None:
    async def echo(arguments: Mapping[str, JsonValue]) -> JsonValue:
        pytest.fail("rejected tool must not execute")

    provider = FakeProvider([completion(calls=(ToolCall("call", "echo", {}),))])
    agent, journal = runtime(
        tmp_path,
        provider,
        registry(echo, safety=SafetyClass.OPAQUE_SIDE_EFFECT),
    )
    run = agent.create_run("danger")
    paused = await agent.execute(run.id)
    approval = journal.list_pending_approvals(run.id)[0]
    journal.decide_approval(approval.id, False, "tester")
    rejected = await agent.execute(run.id)

    assert paused.status is RunStatus.PAUSED
    assert rejected.status is RunStatus.CANCELLED
    assert journal.get_step(approval.step_id or "").status is StepStatus.CANCELLED
    journal.close()


@pytest.mark.asyncio
async def test_preapproved_non_read_tool_executes_without_approval(tmp_path: Path) -> None:
    executions = 0

    async def echo(arguments: Mapping[str, JsonValue]) -> JsonValue:
        nonlocal executions
        executions += 1
        return "ok"

    provider = FakeProvider(
        [
            completion(calls=(ToolCall("call", "echo", {}),)),
            completion("done"),
        ]
    )
    config = RuntimeConfig(approval_policy=DefaultApprovalPolicy(frozenset({"echo"})))
    agent, journal = runtime(
        tmp_path,
        provider,
        registry(echo, safety=SafetyClass.IDEMPOTENT),
        config=config,
    )
    result = await agent.run("approved")

    assert result.status is RunStatus.COMPLETED
    assert executions == 1
    assert journal.list_approvals(run_id=result.run_id) == []
    journal.close()


@pytest.mark.asyncio
async def test_budget_reservation_settlement_and_cost(tmp_path: Path) -> None:
    provider = FakeProvider([completion("done", prompt_tokens=4, completion_tokens=2)])
    config = RuntimeConfig(
        reservation_calls=1,
        reservation_tokens=10,
        reservation_micro_usd=50,
    )
    agent, journal = runtime(tmp_path, provider, config=config, estimator=lambda result: 12)
    run = agent.create_run(
        "budget",
        budget=Budget(call_limit=1, token_limit=10, micro_usd_limit=50),
    )
    result = await agent.execute(run.id)
    budget = journal.get_run(run.id).budget

    assert result.status is RunStatus.COMPLETED
    assert budget.reserved_calls == budget.reserved_tokens == 0
    assert (budget.used_calls, budget.used_tokens, budget.used_micro_usd) == (1, 6, 12)
    assert [event.type for event in journal.list_events(run.id)].count("budget.reserved") == 1
    journal.close()


@pytest.mark.asyncio
async def test_repeated_tool_set_stops_no_progress(tmp_path: Path) -> None:
    async def echo(arguments: Mapping[str, JsonValue]) -> JsonValue:
        return "same"

    repeated = ToolCall("changing-id-1", "echo", {"x": 1})
    repeated_2 = ToolCall("changing-id-2", "echo", {"x": 1})
    provider = FakeProvider([completion(calls=(repeated,)), completion(calls=(repeated_2,))])
    agent, journal = runtime(
        tmp_path,
        provider,
        registry(echo),
        config=RuntimeConfig(no_progress_threshold=2),
    )
    result = await agent.run("loop")

    assert result.status is RunStatus.FAILED
    assert "runtime.no_progress" in [event.type for event in journal.list_events(result.run_id)]
    journal.close()


@pytest.mark.asyncio
async def test_provider_failure_is_recorded_and_retryable(tmp_path: Path) -> None:
    provider = FakeProvider([ProviderError("timeout"), completion("recovered")])
    agent, journal = runtime(tmp_path, provider)
    result = await agent.run("retry")

    assert result.status is RunStatus.COMPLETED
    assert len(provider.calls) == 2
    calls = journal.list_provider_calls(result.run_id)
    assert [call.status for call in calls] == ["failed", "completed"]
    warning = next(
        event
        for event in journal.list_events(result.run_id)
        if event.type == "provider.retryable_failure"
    )
    assert "duplicate billing" in warning.payload["warning"]
    assert journal.get_run(result.run_id).budget.used_calls == 2
    journal.close()


@pytest.mark.asyncio
async def test_recorded_replay_performs_no_external_calls(tmp_path: Path) -> None:
    provider = FakeProvider([completion("recorded")])
    agent, journal = runtime(tmp_path, provider)
    result = await agent.run("replay")
    provider.calls.clear()

    replay = recorded_replay(journal, result.run_id)

    assert replay.final_output == "recorded"
    assert replay.provider_usage.total_tokens == 5
    assert replay.actual_models == ("actual",)
    assert provider.calls == []
    journal.close()


def test_serialization_preserves_ids_content_and_immutable_mappings() -> None:
    call = ToolCall("id", "tool", {"nested": {"x": 1}})
    message = Message(
        "assistant",
        [{"type": "text", "text": "hello"}],
        tool_calls=(call,),
    )
    result = CompletionResult(message, "model", Usage(total_tokens=1))

    restored_call = deserialize_tool_call(serialize_tool_call(call))
    restored_message = deserialize_message(serialize_message(message))
    restored_result = deserialize_completion(serialize_completion(result))

    assert restored_call == call
    assert restored_message == message
    assert restored_result == result
    with pytest.raises(TypeError):
        restored_call.arguments["new"] = 1  # type: ignore[index]
