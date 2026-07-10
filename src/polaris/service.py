"""Application service coordinating durable runtime components."""

from __future__ import annotations

import asyncio
from collections.abc import Mapping, Sequence
from contextlib import suppress
from typing import Any

from polaris.artifacts import ArtifactStore
from polaris.ensemble import BudgetSpec, EnsembleResearchEngine, WorkerSpec
from polaris.journal import (
    ApprovalRecord,
    ArtifactRecord,
    Budget,
    EventRecord,
    InvalidTransitionError,
    Journal,
    RunRecord,
    RunStatus,
    SafetyClass,
    StepStatus,
)
from polaris.providers import Provider, ProviderConfigurationError
from polaris.runtime import AgentRuntime, DefaultApprovalPolicy, RuntimeConfig, recorded_replay
from polaris.tools import ToolRegistry

from .config import AppConfig
from .factory import build_tools, create_providers

_TERMINAL = {RunStatus.COMPLETED, RunStatus.FAILED, RunStatus.CANCELLED}


class AgentService:
    """Own and coordinate all stateful daemon dependencies."""

    def __init__(
        self,
        config: AppConfig,
        *,
        journal: Journal | None = None,
        artifact_store: ArtifactStore | None = None,
        providers: Mapping[str, Provider] | None = None,
        tools: ToolRegistry | None = None,
    ) -> None:
        if not isinstance(config, AppConfig):
            raise TypeError("config must be an AppConfig")
        self.config = config
        self.paths = config.paths
        self.paths.ensure()
        self.journal = journal or Journal(self.paths.journal_file)
        self.artifact_store = artifact_store or ArtifactStore(self.paths.artifact_dir)
        self.providers = dict(providers) if providers is not None else create_providers(config)
        self.tools = tools or build_tools(config)
        self._tasks: dict[str, asyncio.Task[Any]] = {}
        self._closed = False

    def _runtime_config(
        self, persisted: Mapping[str, Any] | None = None
    ) -> RuntimeConfig:
        value = persisted or {}
        preapproved = value.get("preapproved_tool_names", ())
        return RuntimeConfig(
            system_prompt=str(value.get("system_prompt", "You are a helpful assistant.")),
            max_iterations=int(value.get("max_iterations", 24)),
            lease_seconds=float(value.get("lease_seconds", 30.0)),
            approval_policy=DefaultApprovalPolicy(frozenset(map(str, preapproved))),
            reservation_tokens=int(value.get("reservation_tokens", 4096)),
            reservation_calls=int(value.get("reservation_calls", 1)),
            reservation_micro_usd=int(value.get("reservation_micro_usd", 0)),
            no_progress_threshold=int(value.get("no_progress_threshold", 3)),
        )

    def _runtime(
        self, provider_name: str, persisted: Mapping[str, Any] | None = None
    ) -> AgentRuntime:
        try:
            provider = self.providers[provider_name]
        except KeyError as exc:
            raise ProviderConfigurationError(
                f"provider {provider_name!r} is not configured"
            ) from exc
        return AgentRuntime(
            self.journal,
            provider,
            provider_name,
            self.tools,
            self._runtime_config(persisted),
        )

    def _ensemble(self, *, max_workers: int | None = None) -> EnsembleResearchEngine:
        return EnsembleResearchEngine(
            self.journal,
            self.artifact_store,
            provider_map=self.providers,
            tool_registry=self.tools,
            max_workers=max_workers,
        )

    def _default_provider(self) -> str:
        for name in self.config.providers:
            if name in self.providers:
                return name
        raise ProviderConfigurationError("no providers are configured")

    async def submit_single(
        self,
        prompt: str,
        *,
        provider: str | None = None,
        budget: Budget | Mapping[str, Any] | None = None,
        schedule: bool = True,
    ) -> RunRecord:
        provider_name = provider or self._default_provider()
        run = self._runtime(provider_name).create_run(prompt, budget=budget)
        if schedule:
            self._schedule(run.id)
        return run

    async def submit_fanout(
        self,
        question: str,
        workers: Sequence[WorkerSpec],
        *,
        verifier: str,
        synthesizer: str,
        budget: Budget | Mapping[str, Any] | BudgetSpec,
        max_workers: int | None = None,
        schedule: bool = True,
    ) -> RunRecord:
        run = self._ensemble(max_workers=max_workers).create_run(
            question, workers, verifier, synthesizer, budget
        )
        if schedule:
            self._schedule(run.id)
        return run

    async def submit_foundry_router(
        self,
        question: str,
        *,
        provider: str,
        budget: Budget | Mapping[str, Any] | BudgetSpec,
        schedule: bool = True,
    ) -> RunRecord:
        try:
            configured = self.config.providers[provider]
        except KeyError as exc:
            raise ProviderConfigurationError(
                f"provider {provider!r} is not configured"
            ) from exc
        if configured.kind != "foundry_router":
            raise ProviderConfigurationError(
                f"provider {provider!r} is not a Foundry Model Router deployment"
            )
        run = self._ensemble(max_workers=1).create_foundry_router_run(
            question,
            provider,
            budget,
        )
        if schedule:
            self._schedule(run.id)
        return run

    def _schedule(self, run_id: str) -> asyncio.Task[Any]:
        if self._closed:
            raise RuntimeError("service is closed")
        existing = self._tasks.get(run_id)
        if existing is not None and not existing.done():
            return existing
        task = asyncio.create_task(self._execute(run_id), name=f"polaris-{run_id}")
        self._tasks[run_id] = task
        task.add_done_callback(lambda completed: self._task_done(run_id, completed))
        return task

    async def _execute(self, run_id: str) -> object:
        run = self.journal.get_run(run_id)
        try:
            if run.mode == "single":
                config = run.config if isinstance(run.config, Mapping) else {}
                provider = config.get("provider")
                if not isinstance(provider, str):
                    raise ProviderConfigurationError("run does not record a provider")
                return await self._runtime(provider, config).execute(run_id)
            if run.mode == "fan-out":
                max_workers = (
                    int(run.config.get("max_workers", 4))
                    if isinstance(run.config, Mapping)
                    else None
                )
                return await self._ensemble(max_workers=max_workers).execute(run_id)
            raise ValueError(f"unsupported run mode: {run.mode!r}")
        except asyncio.CancelledError:
            current = self.journal.get_run(run_id)
            if current.status not in _TERMINAL:
                with suppress(InvalidTransitionError):
                    self.journal.mark_run_status(run_id, RunStatus.CANCELLED)
            self.journal.append_event(run_id, "service.task_cancelled", {})
            raise
        except BaseException as exc:
            current = self.journal.get_run(run_id)
            if current.status not in _TERMINAL:
                with suppress(InvalidTransitionError):
                    self.journal.mark_run_status(run_id, RunStatus.FAILED)
            self.journal.append_event(
                run_id,
                "service.task_failed",
                {"error_type": type(exc).__name__, "message": str(exc)},
            )
            raise

    def _task_done(self, run_id: str, task: asyncio.Task[Any]) -> None:
        self._tasks.pop(run_id, None)
        if not task.cancelled():
            task.exception()

    async def startup(self) -> tuple[str, ...]:
        self.journal.reclaim_expired_leases()
        resumed: list[str] = []
        runs = self.journal.list_runs((RunStatus.CREATED, RunStatus.RUNNING))
        parent_ids = {run.parent_run_id for run in runs if run.parent_run_id}
        for run in runs:
            if run.parent_run_id is not None and run.parent_run_id in parent_ids:
                continue
            if self._has_active_lease(run.id):
                self.journal.append_event(
                    run.id,
                    "service.recovery_skipped",
                    {"reason": "run still has an active step lease"},
                )
                continue
            if self._has_uncertain_opaque_step(run.id):
                self.journal.append_event(
                    run.id,
                    "service.recovery_skipped",
                    {"reason": "uncertain opaque side effect requires operator decision"},
                )
                continue
            if not self._providers_available(run):
                self.journal.append_event(
                    run.id,
                    "service.recovery_skipped",
                    {"reason": "provider configuration unavailable"},
                )
                continue
            self._schedule(run.id)
            resumed.append(run.id)
        return tuple(resumed)

    def _has_active_lease(self, run_id: str) -> bool:
        return any(
            step.status in {StepStatus.LEASED, StepStatus.EXECUTING}
            for step in self.journal.list_steps(run_id)
        )

    def _has_uncertain_opaque_step(self, run_id: str) -> bool:
        return any(
            step.status is StepStatus.UNCERTAIN
            and step.safety is SafetyClass.OPAQUE_SIDE_EFFECT
            for step in self.journal.list_steps(run_id)
        )

    def _providers_available(self, run: RunRecord) -> bool:
        if not isinstance(run.config, Mapping):
            return False
        if run.mode == "single":
            provider = run.config.get("provider")
            return isinstance(provider, str) and provider in self.providers
        if run.mode == "fan-out":
            names = {run.config.get("verifier"), run.config.get("synthesizer")}
            workers = run.config.get("workers", ())
            if isinstance(workers, list):
                names.update(
                    worker.get("provider_name")
                    for worker in workers
                    if isinstance(worker, Mapping)
                )
            return all(isinstance(name, str) and name in self.providers for name in names)
        return False

    def get(self, run_id: str) -> RunRecord:
        return self.journal.get_run(run_id)

    status = get

    def list_runs(self, status: RunStatus | str | None = None) -> list[RunRecord]:
        return self.journal.list_runs(status)

    def timeline(
        self, run_id: str, *, after_id: int | None = None, limit: int | None = None
    ) -> list[EventRecord]:
        return self.journal.list_events(run_id, after_id=after_id, limit=limit)

    def artifacts(self, run_id: str) -> list[ArtifactRecord]:
        self.journal.get_run(run_id)
        return self.journal.list_artifacts(run_id)

    def approvals(
        self, run_id: str, *, pending_only: bool = False
    ) -> list[ApprovalRecord]:
        self.journal.get_run(run_id)
        return (
            self.journal.list_pending_approvals(run_id)
            if pending_only
            else self.journal.list_approvals(run_id=run_id)
        )

    def replay(self, run_id: str) -> object:
        run = self.journal.get_run(run_id)
        if run.mode == "fan-out":
            return self._ensemble().replay(run_id)
        return recorded_replay(self.journal, run_id)

    async def decide_approval(
        self,
        approval_id: str,
        approved: bool,
        *,
        decided_by: str = "local-user",
        reason: str | None = None,
        schedule: bool = True,
    ) -> ApprovalRecord:
        approval = self.journal.decide_approval(
            approval_id, approved, decided_by=decided_by, reason=reason
        )
        if schedule:
            run = self.journal.get_run(approval.run_id)
            if run.status not in _TERMINAL:
                self._schedule(run.id)
        return approval

    async def approve(
        self,
        approval_id: str,
        *,
        decided_by: str = "local-user",
        reason: str | None = None,
    ) -> ApprovalRecord:
        return await self.decide_approval(
            approval_id, True, decided_by=decided_by, reason=reason
        )

    async def reject(
        self, approval_id: str, *, decided_by: str = "local-user", reason: str | None = None
    ) -> ApprovalRecord:
        return await self.decide_approval(
            approval_id, False, decided_by=decided_by, reason=reason
        )

    async def resume(self, run_id: str) -> RunRecord:
        run = self.journal.get_run(run_id)
        if run.status in _TERMINAL:
            raise InvalidTransitionError(f"run {run_id!r} is already {run.status.value}")
        if self._has_uncertain_opaque_step(run_id):
            raise InvalidTransitionError("uncertain opaque steps require explicit reconciliation")
        if not self._providers_available(run):
            raise ProviderConfigurationError("run provider configuration is unavailable")
        self._schedule(run_id)
        return run

    async def cancel(self, run_id: str) -> RunRecord:
        run = self.journal.get_run(run_id)
        if run.status in _TERMINAL:
            return run
        task = self._tasks.get(run_id)
        if task is not None:
            task.cancel()
            with suppress(asyncio.CancelledError):
                await task
        current = self.journal.get_run(run_id)
        if current.status not in _TERMINAL:
            current = self.journal.mark_run_status(run_id, RunStatus.CANCELLED)
        return current

    async def provider_doctor(self) -> dict[str, object]:
        results: dict[str, object] = {}
        seen: set[int] = set()
        for name, provider in self.providers.items():
            if id(provider) in seen:
                continue
            seen.add(id(provider))
            try:
                results[name] = dict(await provider.doctor())
            except Exception as exc:
                results[name] = {
                    "ok": False,
                    "error_type": type(exc).__name__,
                    "message": str(exc),
                }
        return results

    async def models(self) -> dict[str, object]:
        results: dict[str, object] = {}
        seen: set[int] = set()
        for name, provider in self.providers.items():
            if id(provider) in seen:
                continue
            seen.add(id(provider))
            try:
                results[name] = list(await provider.list_models())
            except Exception as exc:
                results[name] = {
                    "error_type": type(exc).__name__,
                    "message": str(exc),
                }
        return results

    def tool_names(self) -> tuple[str, ...]:
        return self.tools.names()

    async def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        tasks = tuple(self._tasks.values())
        for task in tasks:
            task.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)
        seen: set[int] = set()
        for provider in self.providers.values():
            if id(provider) not in seen:
                seen.add(id(provider))
                await provider.aclose()
        self.journal.close()

    aclose = close
    list = list_runs
