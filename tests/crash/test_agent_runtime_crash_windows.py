from __future__ import annotations

from collections.abc import Mapping, Sequence
from pathlib import Path
from time import sleep

import pytest

from polaris.journal import Journal, RunStatus
from polaris.providers import CompletionResult, Message, Provider, ProviderConfig, ToolCall
from polaris.runtime import (
    AgentRuntime,
    RuntimeConfig,
    serialize_completion,
    serialize_tool_call,
)
from polaris.tools import SafetyClass, ToolEntry, ToolRegistry
from polaris.tools.registry import JsonValue

LEASE_SECONDS = 2.0
LEASE_EXPIRY_WAIT = 2.5


class CountingProvider(Provider):
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
        return CompletionResult(Message("assistant", "external"), "actual")

    async def list_models(self) -> tuple[str, ...]:
        return ()

    async def doctor(self) -> Mapping[str, JsonValue]:
        return {}

    async def aclose(self) -> None:
        return None


def setup(tmp_path: Path) -> tuple[Journal, CountingProvider, AgentRuntime, str]:
    journal = Journal(tmp_path / "windows.sqlite3")
    provider = CountingProvider()
    runtime = AgentRuntime(
        journal,
        provider,
        "fake",
        ToolRegistry(),
        RuntimeConfig(lease_seconds=LEASE_SECONDS),
    )
    run = runtime.create_run("window")
    journal.mark_run_status(run.id, RunStatus.RUNNING)
    return journal, provider, runtime, run.id


@pytest.mark.asyncio
async def test_prepared_model_step_is_claimed_and_called_once(tmp_path: Path) -> None:
    journal, provider, runtime, run_id = setup(tmp_path)
    messages = [
        Message("system", runtime.config.system_prompt),
        Message("user", "window"),
    ]
    journal.create_step(
        run_id,
        "model",
        "completion-0-attempt-1",
        {
            "iteration": 0,
            "attempt": 1,
            "provider": "fake",
            "requested_model": "requested",
            "messages": [
                {
                    "role": message.role,
                    "content": message.content,
                    "name": None,
                    "tool_calls": [],
                    "tool_call_id": None,
                }
                for message in messages
            ],
            "tools": [],
        },
        "read_only",
        sequence=0,
    )
    result = await runtime.execute(run_id)

    assert result.status is RunStatus.COMPLETED
    assert provider.calls == 1
    journal.close()


@pytest.mark.asyncio
async def test_expired_executing_model_step_is_retried(tmp_path: Path) -> None:
    journal, provider, runtime, run_id = setup(tmp_path)
    step = journal.create_step(
        run_id,
        "model",
        "completion-0-attempt-1",
        {
            "iteration": 0,
            "attempt": 1,
            "provider": "fake",
            "requested_model": "requested",
            "messages": [],
            "tools": [],
        },
        "read_only",
        sequence=0,
    )
    claimed = journal.claim_ready_step("crashed-worker", LEASE_SECONDS, run_id)
    assert claimed is not None
    journal.mark_executing(step.id, "crashed-worker")
    sleep(LEASE_EXPIRY_WAIT)

    result = await runtime.execute(run_id)

    assert result.status is RunStatus.COMPLETED
    assert provider.calls == 1
    assert journal.get_step(step.id).attempt_count == 2
    journal.close()


@pytest.mark.asyncio
async def test_committed_model_step_is_never_called_again(tmp_path: Path) -> None:
    journal, provider, runtime, run_id = setup(tmp_path)
    output = CompletionResult(Message("assistant", "durable"), "recorded")
    step = journal.create_step(
        run_id,
        "model",
        "completion-0-attempt-1",
        {
            "iteration": 0,
            "attempt": 1,
            "provider": "fake",
            "requested_model": "requested",
            "messages": [],
            "tools": [],
        },
        "read_only",
        sequence=0,
    )
    claimed = journal.claim_ready_step("writer", 30, run_id)
    assert claimed is not None
    journal.mark_executing(step.id, "writer")
    journal.commit_step(step.id, "writer", serialize_completion(output))

    result = await runtime.execute(run_id)

    assert result.status is RunStatus.COMPLETED
    assert result.final_text == "durable"
    assert provider.calls == 0
    journal.close()


@pytest.mark.parametrize(
    ("window", "expected_executions", "expected_attempts"),
    [
        ("prepared", 1, 1),
        ("executing", 1, 2),
        ("committed", 0, 1),
    ],
)
@pytest.mark.asyncio
async def test_tool_crash_windows(
    tmp_path: Path,
    window: str,
    expected_executions: int,
    expected_attempts: int,
) -> None:
    journal = Journal(tmp_path / f"tool-{window}.sqlite3")
    provider = CountingProvider()
    executions = 0

    async def handler(arguments: Mapping[str, JsonValue]) -> JsonValue:
        nonlocal executions
        executions += 1
        return {"ok": True}

    tools = ToolRegistry()
    tools.register(
        ToolEntry(
            "read",
            "test",
            {"parameters": {"type": "object"}},
            handler,
            safety_class=SafetyClass.READ_ONLY,
        )
    )
    runtime = AgentRuntime(
        journal,
        provider,
        "fake",
        tools,
        RuntimeConfig(lease_seconds=LEASE_SECONDS),
    )
    run = runtime.create_run("tool window")
    journal.mark_run_status(run.id, RunStatus.RUNNING)
    call = ToolCall("call", "read", {"path": "state"})
    model = journal.create_step(
        run.id,
        "model",
        "completion-0-attempt-1",
        {
            "iteration": 0,
            "attempt": 1,
            "provider": "fake",
            "requested_model": "requested",
            "messages": [],
            "tools": [],
        },
        "read_only",
        sequence=0,
    )
    assert journal.claim_ready_step("writer", 30, run.id) is not None
    journal.mark_executing(model.id, "writer")
    journal.commit_step(
        model.id,
        "writer",
        serialize_completion(
            CompletionResult(Message("assistant", "", tool_calls=(call,)), "recorded")
        ),
    )
    tool = journal.create_step(
        run.id,
        "tool",
        "read",
        {
            "iteration": 0,
            "call_index": 0,
            "tool_call": serialize_tool_call(call),
            "arguments": {"path": "state"},
        },
        "read_only",
        sequence=1,
    )
    if window in {"executing", "committed"}:
        assert journal.claim_ready_step("tool-worker", LEASE_SECONDS, run.id) is not None
        journal.mark_executing(tool.id, "tool-worker")
    if window == "executing":
        sleep(LEASE_EXPIRY_WAIT)
    elif window == "committed":
        journal.commit_step(
            tool.id,
            "tool-worker",
            {"tool_call_id": "call", "name": "read", "result": {"ok": True}},
        )

    result = await runtime.execute(run.id)

    assert result.status is RunStatus.COMPLETED
    assert executions == expected_executions
    assert journal.get_step(tool.id).attempt_count == expected_attempts
    assert provider.calls == 1
    journal.close()
