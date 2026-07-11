"""Pure recorded replay of a durable agent run."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any

from polaris.journal import EventRecord, Journal, StepRecord, StepStatus, canonical_json
from polaris.providers import Message, Usage

from .serialization import deserialize_completion


@dataclass(frozen=True, slots=True)
class ReplayResult:
    timeline: tuple[EventRecord, ...]
    messages: tuple[Message, ...]
    final_output: str | None
    provider_usage: Usage
    actual_models: tuple[str, ...]


def recorded_tool_output(
    journal: Journal, run_id: str, original: StepRecord
) -> tuple[bool, object]:
    """Resolve a recorded tool result through receipts and recovery lineage."""
    current = original
    visited: set[str] = set()
    steps = journal.list_steps(run_id)
    while current.id not in visited:
        visited.add(current.id)
        if current.status is StepStatus.COMMITTED:
            return True, _tool_result(current.output)
        receipt = journal.find_receipt(f"runtime:reconciled:{current.id}")
        if receipt is not None:
            return True, _tool_result(receipt.payload)
        replacement = next(
            (
                candidate
                for candidate in steps
                if candidate.kind == "tool" and candidate.input.get("recovery_of") == current.id
            ),
            None,
        )
        if replacement is None:
            return False, None
        current = replacement
    return False, None


def _tool_result(output: object) -> object:
    if isinstance(output, Mapping) and "result" in output:
        return output["result"]
    return output


def recorded_replay(journal: Journal, run_id: str) -> ReplayResult:
    run = journal.get_run(run_id)
    prompt: Any = (
        run.request.get("prompt") if isinstance(run.request, Mapping) else run.request
    )
    messages: list[Message] = []
    if isinstance(run.config, Mapping) and run.config.get("system_prompt"):
        messages.append(Message("system", str(run.config["system_prompt"])))
    user_content = prompt if isinstance(prompt, str) else canonical_json(prompt)
    if isinstance(run.config, Mapping):
        memory_context = run.config.get("memory_context")
        if isinstance(memory_context, str) and memory_context:
            user_content = f"{user_content}\n\n{memory_context}"
    messages.append(Message("user", user_content))

    final_output: str | None = None
    for step in journal.list_steps(run_id):
        if step.kind != "model" or step.status is not StepStatus.COMMITTED:
            continue
        completion = deserialize_completion(step.output)
        messages.append(completion.message)
        if not completion.tool_calls:
            content = completion.message.content
            final_output = (
                content
                if isinstance(content, str)
                else ""
                if content is None
                else canonical_json(list(content))
            )
            continue
        tool_steps = {
            int(candidate.input["call_index"]): candidate
            for candidate in journal.list_steps(run_id)
            if candidate.kind == "tool"
            and candidate.input.get("iteration") == step.input.get("iteration")
            and "recovery_of" not in candidate.input
        }
        for index, call in enumerate(completion.tool_calls):
            tool_step = tool_steps.get(index)
            if tool_step is None:
                break
            found, result = recorded_tool_output(journal, run_id, tool_step)
            if not found:
                break
            messages.append(
                Message(
                    "tool",
                    canonical_json(result),
                    name=call.name,
                    tool_call_id=call.id,
                )
            )

    provider_calls = [
        call for call in journal.list_provider_calls(run_id) if call.status == "completed"
    ]
    models: list[str] = []
    for provider_call in provider_calls:
        if provider_call.model is not None and provider_call.model not in models:
            models.append(provider_call.model)
    input_tokens = sum(call.input_tokens for call in provider_calls)
    output_tokens = sum(call.output_tokens for call in provider_calls)
    return ReplayResult(
        timeline=tuple(journal.materialized_timeline(run_id)),
        messages=tuple(messages),
        final_output=final_output,
        provider_usage=Usage(
            prompt_tokens=input_tokens,
            completion_tokens=output_tokens,
            total_tokens=input_tokens + output_tokens,
        ),
        actual_models=tuple(models),
    )


replay = recorded_replay
