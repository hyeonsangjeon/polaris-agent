"""Single-agent durable model/tool runtime."""

from __future__ import annotations

import asyncio
from abc import ABC, abstractmethod
from collections.abc import AsyncIterator, Callable, Mapping, Sequence
from contextlib import asynccontextmanager, suppress
from dataclasses import dataclass, field
from types import MappingProxyType
from typing import Any, cast

from polaris.journal import (
    Budget,
    BudgetExceededError,
    Journal,
    RunRecord,
    RunStatus,
    StepRecord,
    StepStatus,
    canonical_json,
)
from polaris.journal import (
    SafetyClass as JournalSafetyClass,
)
from polaris.providers import (
    CompletionResult,
    Message,
    Provider,
    ProviderError,
    ToolCall,
    Usage,
)
from polaris.tools import SafetyClass, ToolEntry, ToolRegistry

from .replay import ReplayResult, recorded_replay, recorded_tool_output
from .serialization import (
    deserialize_completion,
    deserialize_message,
    serialize_completion,
    serialize_message,
    serialize_tool_call,
)

CostEstimator = Callable[[CompletionResult], int]


class ApprovalPolicy(ABC):
    """Determines whether a tool invocation needs a durable decision."""

    @abstractmethod
    def requires_approval(self, tool_name: str, safety: SafetyClass) -> bool:
        """Return whether the invocation must be approved before execution."""


@dataclass(frozen=True, slots=True)
class DefaultApprovalPolicy(ApprovalPolicy):
    """Auto-approve reads and explicitly preapproved tool names."""

    preapproved_tool_names: frozenset[str] = field(default_factory=frozenset)
    preapproved_tools: frozenset[str] = field(default_factory=frozenset)

    def __post_init__(self) -> None:
        object.__setattr__(self, "preapproved_tool_names", frozenset(self.preapproved_tool_names))
        object.__setattr__(self, "preapproved_tools", frozenset(self.preapproved_tools))

    def requires_approval(self, tool_name: str, safety: SafetyClass) -> bool:
        return (
            safety is not SafetyClass.READ_ONLY
            and tool_name not in self.preapproved_tool_names
            and tool_name not in self.preapproved_tools
        )


@dataclass(frozen=True, slots=True)
class RuntimeConfig:
    system_prompt: str = "You are a helpful assistant."
    max_iterations: int = 24
    lease_seconds: float = 30.0
    worker_id: str = "agent-runtime"
    approval_policy: ApprovalPolicy = field(default_factory=DefaultApprovalPolicy)
    reservation_tokens: int = 4096
    reservation_calls: int = 1
    reservation_micro_usd: int = 0
    no_progress_threshold: int = 3
    memory_context: str | None = None
    memory_scope: Mapping[str, str] | None = None
    memory_snapshot_hash: str | None = None

    def __post_init__(self) -> None:
        if self.max_iterations <= 0:
            raise ValueError("max_iterations must be positive")
        if self.lease_seconds <= 0:
            raise ValueError("lease_seconds must be positive")
        if not self.worker_id:
            raise ValueError("worker_id must not be empty")
        for name in ("reservation_tokens", "reservation_calls", "reservation_micro_usd"):
            value = getattr(self, name)
            if isinstance(value, bool) or not isinstance(value, int) or value < 0:
                raise ValueError(f"{name} must be a non-negative integer")
        if self.no_progress_threshold <= 0:
            raise ValueError("no_progress_threshold must be positive")
        if self.memory_context is not None and not isinstance(self.memory_context, str):
            raise TypeError("memory_context must be a string or None")
        if self.memory_scope is not None:
            scope = dict(self.memory_scope)
            if set(scope) != {"profile_id", "subject_key"} or not all(
                isinstance(value, str) and value for value in scope.values()
            ):
                raise ValueError("memory_scope requires non-empty profile_id and subject_key")
            object.__setattr__(self, "memory_scope", MappingProxyType(scope))
        if self.memory_snapshot_hash is not None and not self.memory_snapshot_hash:
            raise ValueError("memory_snapshot_hash must not be empty")

    @property
    def token_reservation(self) -> int:
        return self.reservation_tokens

    @property
    def call_reservation(self) -> int:
        return self.reservation_calls

    @property
    def micro_usd_reservation(self) -> int:
        return self.reservation_micro_usd


@dataclass(frozen=True, slots=True)
class AgentResult:
    run_id: str
    status: RunStatus
    final_text: str | None
    actual_models: tuple[str, ...]
    usage: Usage


class AgentRuntime:
    """Execute one durable agent conversation at a time."""

    def __init__(
        self,
        journal: Journal,
        provider: Provider,
        provider_name: str,
        tools: ToolRegistry,
        config: RuntimeConfig | None = None,
        cost_estimator: CostEstimator | None = None,
    ) -> None:
        if not provider_name:
            raise ValueError("provider_name must not be empty")
        self.journal = journal
        self.provider = provider
        self.provider_name = provider_name
        self.tools = tools
        self.config = config or RuntimeConfig()
        self.cost_estimator = cost_estimator

    def create_run(
        self,
        request: str,
        mode: str = "single",
        budget: Budget | Mapping[str, Any] | None = None,
        *,
        external_key: str | None = None,
    ) -> RunRecord:
        requested_model = self.provider.config.model
        preapproved = (
            sorted(
                self.config.approval_policy.preapproved_tool_names
                | self.config.approval_policy.preapproved_tools
            )
            if isinstance(self.config.approval_policy, DefaultApprovalPolicy)
            else []
        )
        persisted_config = {
            "provider": self.provider_name,
            "model": requested_model,
            "system_prompt": self.config.system_prompt,
            "max_iterations": self.config.max_iterations,
            "lease_seconds": self.config.lease_seconds,
            "approval_policy": type(self.config.approval_policy).__name__,
            "preapproved_tool_names": preapproved,
            "reservation_tokens": self.config.reservation_tokens,
            "reservation_calls": self.config.reservation_calls,
            "reservation_micro_usd": self.config.reservation_micro_usd,
            "no_progress_threshold": self.config.no_progress_threshold,
            "memory_context": self.config.memory_context,
            "memory_scope": self.config.memory_scope,
            "memory_snapshot_hash": self.config.memory_snapshot_hash,
        }
        return self.journal.create_run(
            mode,
            {"prompt": request},
            persisted_config,
            budget=budget,
            external_key=external_key,
        )

    async def run(
        self,
        request: str,
        mode: str = "single",
        budget: Budget | Mapping[str, Any] | None = None,
        *,
        external_key: str | None = None,
    ) -> AgentResult:
        return await self.execute(
            self.create_run(request, mode, budget, external_key=external_key).id
        )

    def recover(self) -> tuple[str, ...]:
        self.journal.reclaim_expired_leases()
        return tuple(record.id for record in self.journal.recoverable_runs())

    def replay(self, run_id: str) -> ReplayResult:
        return recorded_replay(self.journal, run_id)

    async def execute(self, run_id: str) -> AgentResult:
        run = self.journal.get_run(run_id)
        if run.status in (RunStatus.COMPLETED, RunStatus.FAILED, RunStatus.CANCELLED):
            return self._result(run_id)

        self.journal.reclaim_expired_leases()
        run = self.journal.get_run(run_id)
        if await self._handle_approvals_and_uncertainty(run_id):
            return self._result(run_id)

        run = self.journal.get_run(run_id)
        if run.status in (RunStatus.PAUSED, RunStatus.CREATED):
            self.journal.mark_run_status(run_id, RunStatus.RUNNING)

        while True:
            messages, iteration, final_text = self._reconstruct(run_id)
            if final_text is not None:
                self.journal.mark_run_status(run_id, RunStatus.COMPLETED)
                return self._result(run_id)
            if iteration >= self.config.max_iterations:
                return self._stop_failed(
                    run_id, "runtime.iteration_limit", "maximum model iterations reached"
                )

            tool_calls = self._outstanding_tool_calls(run_id, iteration)
            if tool_calls:
                outcome = await self._execute_tool_group(run_id, iteration, tool_calls)
                if outcome != "continue":
                    return self._result(run_id)
                if self._has_repeated_tool_calls(run_id):
                    return self._stop_failed(
                        run_id,
                        "runtime.no_progress",
                        "repeated identical tool-call sets",
                    )
                continue

            completed = await self._execute_model(run_id, messages, iteration)
            if not completed:
                if self.journal.get_run(run_id).status is not RunStatus.RUNNING:
                    return self._result(run_id)
                continue

    def _initial_messages(self, run: RunRecord) -> list[Message]:
        request = run.request
        prompt = request.get("prompt") if isinstance(request, Mapping) else request
        content = prompt if isinstance(prompt, str) else canonical_json(prompt)
        memory_context = (
            run.config.get("memory_context")
            if isinstance(run.config, Mapping)
            else self.config.memory_context
        )
        if isinstance(memory_context, str) and memory_context:
            content = f"{content}\n\n{memory_context}"
        messages: list[Message] = []
        system_prompt = (
            run.config.get("system_prompt", self.config.system_prompt)
            if isinstance(run.config, Mapping)
            else self.config.system_prompt
        )
        if system_prompt:
            messages.append(Message("system", str(system_prompt)))
        messages.append(Message("user", content))
        return messages

    def _reconstruct(self, run_id: str) -> tuple[list[Message], int, str | None]:
        run = self.journal.get_run(run_id)
        messages = self._initial_messages(run)
        committed_models: dict[int, CompletionResult] = {}
        steps = self.journal.list_steps(run_id)
        for step in steps:
            if (
                step.kind == "model"
                and step.status is StepStatus.COMMITTED
                and isinstance(step.output, Mapping)
            ):
                iteration = int(step.input["iteration"])
                committed_models[iteration] = deserialize_completion(step.output)

        next_iteration = 0
        for iteration in sorted(committed_models):
            if iteration != next_iteration:
                break
            completion = committed_models[iteration]
            messages.append(completion.message)
            if not completion.tool_calls:
                return messages, iteration + 1, self._message_text(completion.message)
            outputs = self._tool_outputs(run_id, iteration, completion.tool_calls)
            if outputs is None:
                return messages, iteration, None
            for call, output in zip(completion.tool_calls, outputs, strict=True):
                messages.append(
                    Message(
                        "tool",
                        canonical_json(output),
                        name=call.name,
                        tool_call_id=call.id,
                    )
                )
            next_iteration = iteration + 1
        return messages, next_iteration, None

    def _outstanding_tool_calls(self, run_id: str, iteration: int) -> tuple[ToolCall, ...]:
        for step in self.journal.list_steps(run_id):
            if (
                step.kind == "model"
                and step.status is StepStatus.COMMITTED
                and int(step.input["iteration"]) == iteration
                and isinstance(step.output, Mapping)
            ):
                return deserialize_completion(step.output).tool_calls
        return ()

    def _tool_outputs(
        self, run_id: str, iteration: int, calls: Sequence[ToolCall]
    ) -> list[object] | None:
        originals = {
            int(step.input["call_index"]): step
            for step in self.journal.list_steps(run_id)
            if step.kind == "tool"
            and step.input.get("iteration") == iteration
            and "recovery_of" not in step.input
        }
        outputs: list[object] = []
        for index, _call in enumerate(calls):
            step = originals.get(index)
            if step is None:
                return None
            found, output = recorded_tool_output(self.journal, run_id, step)
            if not found:
                return None
            outputs.append(output)
        return outputs

    async def _execute_model(
        self, run_id: str, messages: Sequence[Message], iteration: int
    ) -> bool:
        prior = [
            step
            for step in self.journal.list_steps(run_id)
            if step.kind == "model" and step.input.get("iteration") == iteration
        ]
        committed = next((step for step in prior if step.status is StepStatus.COMMITTED), None)
        if committed is not None:
            return True
        failures = sum(step.status is StepStatus.FAILED for step in prior)
        if failures >= self.config.max_iterations:
            self._stop_failed(run_id, "provider.attempt_limit", "provider retry limit reached")
            return False

        requested_model = self._requested_model(run_id)
        resumable = next(
            (
                step
                for step in reversed(prior)
                if step.status in (StepStatus.READY, StepStatus.LEASED, StepStatus.EXECUTING)
            ),
            None,
        )
        attempt = failures + 1
        if resumable is None:
            request_snapshot = {
                "iteration": iteration,
                "attempt": attempt,
                "provider": self.provider_name,
                "requested_model": requested_model,
                "messages": [serialize_message(message) for message in messages],
                "tools": list(self.tools.get_definitions()),
            }
            step = self.journal.create_step(
                run_id,
                "model",
                f"completion-{iteration}-attempt-{attempt}",
                request_snapshot,
                JournalSafetyClass.READ_ONLY,
                sequence=iteration * 1000,
            )
        else:
            step = resumable
            request_snapshot = cast(dict[str, Any], step.input)
        if step.status is StepStatus.COMMITTED:
            return True
        claimed = self._claim(step)
        if claimed is None:
            return False
        self.journal.mark_executing(step.id, self.config.worker_id)
        abandoned_calls = [
            call
            for call in self.journal.list_provider_calls(run_id)
            if call.step_id == step.id and call.status == "started"
        ]
        for abandoned in abandoned_calls:
            self.journal.complete_provider_call(
                abandoned.id,
                response={"warning": "worker lease expired before the response committed"},
                status="uncertain",
            )
            self.journal.append_event(
                run_id,
                "provider.duplicate_billing_warning",
                {
                    "provider_call_id": abandoned.id,
                    "warning": "retry may cause duplicate billing",
                },
                step_id=step.id,
            )

        reservation_id: str | None = None
        call_id: str | None = None
        try:
            reservation_id = self.journal.reserve_budget(
                run_id,
                calls=self.config.reservation_calls,
                tokens=self.config.reservation_tokens,
                micro_usd=self.config.reservation_micro_usd,
                reservation_id=f"budget_{step.id}",
            )
            call = self.journal.start_provider_call(
                run_id,
                self.provider_name,
                request_snapshot,
                step_id=step.id,
                model=requested_model,
            )
            call_id = call.id
            snapshot_messages = request_snapshot.get("messages", ())
            snapshot_tools = request_snapshot.get("tools", ())
            self._refresh_lease(step.id)
            async with self._lease_heartbeat(step.id):
                completion = await self.provider.complete(
                    [
                        deserialize_message(message)
                        for message in cast(Sequence[object], snapshot_messages)
                        if isinstance(message, Mapping)
                    ],
                    cast(Sequence[Mapping[str, Any]], snapshot_tools),
                )
                micro_usd = self.cost_estimator(completion) if self.cost_estimator else 0
                usage = completion.usage
                if call_id is None or reservation_id is None:
                    raise RuntimeError("provider bookkeeping was not initialized")
                self._refresh_lease(step.id)
                self.journal.complete_provider_call(
                    call_id,
                    response=serialize_completion(completion),
                    model=completion.model,
                    input_tokens=usage.prompt_tokens,
                    output_tokens=usage.completion_tokens,
                    micro_usd=micro_usd,
                )
                self._refresh_lease(step.id)
                self.journal.settle_budget(
                    run_id,
                    reservation_id,
                    actual_calls=1,
                    actual_tokens=usage.total_tokens,
                    actual_micro_usd=micro_usd,
                )
                self._refresh_lease(step.id)
                self.journal.commit_step(
                    step.id,
                    self.config.worker_id,
                    serialize_completion(completion),
                )
        except BudgetExceededError as exc:
            self.journal.fail_step(step.id, self.config.worker_id, self._error(exc))
            self._stop_failed(run_id, "budget.exceeded", str(exc))
            return False
        except (ProviderError, TimeoutError) as exc:
            if call_id is not None:
                self.journal.complete_provider_call(
                    call_id,
                    response=self._error(exc),
                    status="failed",
                    model=requested_model,
                )
            if reservation_id is not None:
                self.journal.settle_budget(
                    run_id,
                    reservation_id,
                    actual_calls=1,
                )
            self.journal.fail_step(step.id, self.config.worker_id, self._error(exc))
            self.journal.append_event(
                run_id,
                "provider.retryable_failure",
                {
                    "error": self._error(exc),
                    "attempt": attempt,
                    "warning": "response was not committed; retry may cause duplicate billing",
                },
                step_id=step.id,
            )
            return False

        return True

    async def _execute_tool_group(
        self, run_id: str, iteration: int, calls: Sequence[ToolCall]
    ) -> str:
        for index, call in enumerate(calls):
            entry = self.tools.get_entry(call.name)
            step_input = {
                "iteration": iteration,
                "call_index": index,
                "tool_call": serialize_tool_call(call),
                "arguments": dict(call.arguments),
            }
            step = self.journal.create_step(
                run_id,
                "tool",
                call.name,
                step_input,
                JournalSafetyClass(entry.safety_class.value),
                sequence=iteration * 1000 + index + 1,
            )
            if step.status is StepStatus.COMMITTED:
                continue
            approval = self._approval_outcome(run_id, step, entry)
            if approval == "pending":
                self._pause(run_id, "tool approval required", step.id)
                return "paused"
            if approval == "rejected":
                self.journal.cancel_step(step.id)
                self.journal.mark_run_status(run_id, RunStatus.CANCELLED)
                return "cancelled"
            claimed = self._claim(step)
            if claimed is None:
                return "paused"
            self.journal.mark_executing(step.id, self.config.worker_id)
            try:
                async with self._lease_heartbeat(step.id):
                    result = await self.tools.execute(call.name, call.arguments)
                    self._refresh_lease(step.id)
            except Exception as exc:
                self.journal.fail_step(step.id, self.config.worker_id, self._error(exc))
                self._stop_failed(run_id, "tool.failed", str(exc), step.id)
                return "failed"
            output = {"tool_call_id": call.id, "name": call.name, "result": result}
            receipt = None
            if entry.safety_class is SafetyClass.RECONCILABLE:
                receipt = {
                    "idempotency_key": f"tool:{step.id}",
                    "payload": output,
                }
            self.journal.commit_step(
                step.id,
                self.config.worker_id,
                output,
                receipt=receipt,
            )
        return "continue"

    async def _handle_approvals_and_uncertainty(self, run_id: str) -> bool:
        for approval in self.journal.list_approvals(run_id=run_id):
            if approval.status == "pending":
                self._pause(run_id, "approval pending", approval.step_id)
                return True
            if approval.status == "rejected":
                if approval.step_id is not None:
                    step = self.journal.get_step(approval.step_id)
                    if step.status in (
                        StepStatus.READY,
                        StepStatus.UNCERTAIN,
                    ):
                        self.journal.cancel_step(step.id)
                run = self.journal.get_run(run_id)
                if run.status not in (RunStatus.CANCELLED, RunStatus.COMPLETED):
                    self.journal.mark_run_status(run_id, RunStatus.CANCELLED)
                return True

        uncertain = [
            step for step in self.journal.list_steps(run_id) if step.status is StepStatus.UNCERTAIN
        ]
        for step in uncertain:
            if step.safety is JournalSafetyClass.RECONCILABLE:
                if await self._reconcile_uncertain(run_id, step):
                    continue
                return True
            decisions = [
                approval
                for approval in self.journal.list_approvals(run_id=run_id, step_id=step.id)
                if approval.kind == "uncertain_outcome"
            ]
            if decisions and decisions[-1].status == "approved":
                if await self._retry_approved_uncertain(run_id, step):
                    continue
                return True
            if not decisions:
                self._request_uncertainty_decision(run_id, step)
            self._pause(run_id, "opaque tool outcome is uncertain", step.id)
            return True
        return False

    async def _retry_approved_uncertain(self, run_id: str, original: StepRecord) -> bool:
        existing = next(
            (
                step
                for step in self.journal.list_steps(run_id)
                if step.kind == "tool" and step.input.get("recovery_of") == original.id
            ),
            None,
        )
        if existing is not None and existing.status in (
            StepStatus.COMMITTED,
            StepStatus.UNCERTAIN,
        ):
            return True
        call = self._call_from_step(original)
        replacement = existing or self.journal.create_step(
            run_id,
            "tool",
            call.name,
            {**cast(dict[str, Any], original.input), "recovery_of": original.id},
            original.safety,
            sequence=(original.sequence or 0) + 600,
        )
        claimed = self._claim(replacement)
        if claimed is None:
            self._pause(run_id, "uncertain tool retry is already leased", replacement.id)
            return False
        self.journal.mark_executing(replacement.id, self.config.worker_id)
        try:
            async with self._lease_heartbeat(replacement.id):
                result = await self.tools.execute(call.name, call.arguments)
                self._refresh_lease(replacement.id)
        except Exception as exc:
            self.journal.fail_step(replacement.id, self.config.worker_id, self._error(exc))
            self._stop_failed(run_id, "tool.failed", str(exc), replacement.id)
            return False
        self.journal.commit_step(
            replacement.id,
            self.config.worker_id,
            {"tool_call_id": call.id, "name": call.name, "result": result},
        )
        return True

    async def _reconcile_uncertain(self, run_id: str, original: StepRecord) -> bool:
        if self.journal.find_receipt(self._reconcile_receipt_key(original)) is not None:
            return True
        call = self._call_from_step(original)
        reconcile_input = {
            "original_step_id": original.id,
            "tool_call": serialize_tool_call(call),
            "arguments": dict(call.arguments),
        }
        reconcile_step = self.journal.create_step(
            run_id,
            "reconcile",
            call.name,
            reconcile_input,
            JournalSafetyClass.READ_ONLY,
            sequence=(original.sequence or 0) + 500,
        )
        if reconcile_step.status is StepStatus.COMMITTED:
            result = reconcile_step.output
        else:
            claimed = self._claim(reconcile_step)
            if claimed is None:
                self._pause(run_id, "reconciliation is already leased", original.id)
                return False
            self.journal.mark_executing(reconcile_step.id, self.config.worker_id)
            try:
                async with self._lease_heartbeat(reconcile_step.id):
                    result = await self.tools.reconcile(call.name, call.arguments)
                    self._refresh_lease(reconcile_step.id)
            except Exception as exc:
                self.journal.fail_step(reconcile_step.id, self.config.worker_id, self._error(exc))
                self._stop_failed(run_id, "tool.reconcile_failed", str(exc), original.id)
                return False
            self.journal.commit_step(
                reconcile_step.id,
                self.config.worker_id,
                result,
                receipt={
                    "idempotency_key": f"reconcile:{reconcile_step.id}",
                    "payload": result,
                },
            )
        if self._reconcile_proves_applied(result):
            output = self._reconciled_output(call, result)
            self.journal.record_receipt(
                original.id,
                self._reconcile_receipt_key(original),
                output,
            )
            self.journal.append_event(
                run_id,
                "tool.reconciled",
                {"original_step_id": original.id, "outcome": "already_applied"},
                step_id=original.id,
            )
            return True

        replacement = self.journal.create_step(
            run_id,
            "tool",
            call.name,
            {
                **cast(dict[str, Any], original.input),
                "recovery_of": original.id,
            },
            original.safety,
            sequence=(original.sequence or 0) + 600,
        )
        entry = self.tools.get_entry(call.name)
        approval = self._approval_outcome(run_id, replacement, entry)
        if approval == "rejected":
            self.journal.cancel_step(replacement.id)
            self.journal.mark_run_status(run_id, RunStatus.CANCELLED)
            return False
        if approval == "pending":
            self._pause(run_id, "retry after reconciliation requires approval", replacement.id)
            return False
        claimed = self._claim(replacement)
        if claimed is None:
            self._pause(run_id, "recovered tool is already leased", replacement.id)
            return False
        self.journal.mark_executing(replacement.id, self.config.worker_id)
        try:
            async with self._lease_heartbeat(replacement.id):
                executed = await self.tools.execute(call.name, call.arguments)
                self._refresh_lease(replacement.id)
        except Exception as exc:
            self.journal.fail_step(replacement.id, self.config.worker_id, self._error(exc))
            self._stop_failed(run_id, "tool.failed", str(exc), replacement.id)
            return False
        self.journal.commit_step(
            replacement.id,
            self.config.worker_id,
            {"tool_call_id": call.id, "name": call.name, "result": executed},
            receipt={
                "idempotency_key": f"tool:{replacement.id}",
                "payload": executed,
            },
        )
        return True

    def _approval_outcome(self, run_id: str, step: StepRecord, entry: ToolEntry) -> str:
        run_config = self.journal.get_run(run_id).config
        durable_preapproved = (
            run_config.get("preapproved_tool_names", ()) if isinstance(run_config, Mapping) else ()
        )
        if entry.name in durable_preapproved or not (
            self.config.approval_policy.requires_approval(entry.name, entry.safety_class)
        ):
            return "approved"
        approvals = self.journal.list_approvals(run_id=run_id, step_id=step.id)
        if not approvals:
            self.journal.request_approval(
                run_id,
                step.id,
                {
                    "tool": entry.name,
                    "safety": entry.safety_class.value,
                    "input": step.input,
                },
                kind="tool_execution",
            )
            return "pending"
        return approvals[-1].status

    def _request_uncertainty_decision(self, run_id: str, step: StepRecord) -> None:
        approvals = self.journal.list_approvals(run_id=run_id, step_id=step.id)
        if any(approval.kind == "uncertain_outcome" for approval in approvals):
            return
        self.journal.request_approval(
            run_id,
            step.id,
            {
                "step": step.id,
                "tool": step.name,
                "reason": step.uncertainty_reason,
            },
            kind="uncertain_outcome",
        )

    def _claim(self, step: StepRecord) -> StepRecord | None:
        current = self.journal.get_step(step.id)
        if current.status is not StepStatus.READY:
            return None
        claimed = self.journal.claim_ready_step(
            self.config.worker_id,
            self.config.lease_seconds,
            step.run_id,
        )
        if claimed is None:
            return None
        if claimed.id != step.id:
            raise RuntimeError(
                f"journal claimed step {claimed.id!r} while {step.id!r} was expected"
            )
        return claimed

    def _refresh_lease(self, step_id: str) -> None:
        self.journal.heartbeat(
            step_id,
            self.config.worker_id,
            self.config.lease_seconds,
        )

    @asynccontextmanager
    async def _lease_heartbeat(self, step_id: str) -> AsyncIterator[None]:
        interval = max(min(self.config.lease_seconds / 3, 5.0), 0.001)

        async def pulse() -> None:
            while True:
                await asyncio.sleep(interval)
                self._refresh_lease(step_id)

        task = asyncio.create_task(pulse(), name=f"lease-heartbeat-{step_id}")
        try:
            yield
            if task.done() and not task.cancelled():
                task.result()
        finally:
            task.cancel()
            with suppress(asyncio.CancelledError):
                await task

    def _has_repeated_tool_calls(self, run_id: str) -> bool:
        signatures: list[str] = []
        for step in self.journal.list_steps(run_id):
            if (
                step.kind == "model"
                and step.status is StepStatus.COMMITTED
                and isinstance(step.output, Mapping)
            ):
                calls = deserialize_completion(step.output).tool_calls
                if calls:
                    signatures.append(
                        canonical_json(
                            [
                                {"name": call.name, "arguments": dict(call.arguments)}
                                for call in calls
                            ]
                        )
                    )
        threshold = self.config.no_progress_threshold
        return len(signatures) >= threshold and len(set(signatures[-threshold:])) == 1

    def _result(self, run_id: str) -> AgentResult:
        run = self.journal.get_run(run_id)
        _, _, final_text = self._reconstruct(run_id)
        calls = [
            call for call in self.journal.list_provider_calls(run_id) if call.status == "completed"
        ]
        models: list[str] = []
        for call in calls:
            if call.model is not None and call.model not in models:
                models.append(call.model)
        prompt = sum(call.input_tokens for call in calls)
        completion = sum(call.output_tokens for call in calls)
        return AgentResult(
            run_id=run_id,
            status=run.status,
            final_text=final_text,
            actual_models=tuple(models),
            usage=Usage(
                prompt_tokens=prompt,
                completion_tokens=completion,
                total_tokens=prompt + completion,
            ),
        )

    def _stop_failed(
        self,
        run_id: str,
        event_type: str,
        message: str,
        step_id: str | None = None,
    ) -> AgentResult:
        self.journal.append_event(run_id, event_type, {"message": message}, step_id=step_id)
        run = self.journal.get_run(run_id)
        if run.status not in (RunStatus.FAILED, RunStatus.CANCELLED):
            self.journal.mark_run_status(run_id, RunStatus.FAILED)
        return self._result(run_id)

    def _pause(self, run_id: str, reason: str, step_id: str | None) -> None:
        self.journal.append_event(run_id, "runtime.paused", {"reason": reason}, step_id=step_id)
        run = self.journal.get_run(run_id)
        if run.status in (RunStatus.CREATED, RunStatus.RUNNING):
            self.journal.mark_run_status(run_id, RunStatus.PAUSED)

    def _requested_model(self, run_id: str) -> str:
        config = self.journal.get_run(run_id).config
        if isinstance(config, Mapping) and isinstance(config.get("model"), str):
            return str(config["model"])
        return self.provider.config.model

    @staticmethod
    def _call_from_step(step: StepRecord) -> ToolCall:
        value = step.input["tool_call"]
        if not isinstance(value, Mapping):
            raise TypeError("tool step does not contain a serialized tool call")
        from .serialization import deserialize_tool_call

        return deserialize_tool_call(value)

    @staticmethod
    def _reconcile_proves_applied(result: object) -> bool:
        if not isinstance(result, Mapping):
            return False
        if result.get("found") or result.get("already_applied"):
            return True
        return result.get("status") in {
            "found",
            "already_applied",
            "applied",
            "completed",
        }

    @staticmethod
    def _reconciled_output(call: ToolCall, result: object) -> dict[str, object]:
        payload = result.get("result", result) if isinstance(result, Mapping) else result
        return {"tool_call_id": call.id, "name": call.name, "result": payload}

    @staticmethod
    def _reconcile_receipt_key(step: StepRecord) -> str:
        return f"runtime:reconciled:{step.id}"

    @staticmethod
    def _message_text(message: Message) -> str:
        if isinstance(message.content, str):
            return message.content
        if message.content is None:
            return ""
        return canonical_json([dict(part) for part in message.content])

    @staticmethod
    def _error(exc: BaseException) -> dict[str, str]:
        return {"type": type(exc).__name__, "message": str(exc)}
