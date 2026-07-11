"""Application service coordinating durable runtime components."""

from __future__ import annotations

import asyncio
from collections.abc import Mapping, Sequence
from contextlib import suppress
from dataclasses import replace
from datetime import datetime
from typing import Any, TypedDict

from polaris.artifacts import ArtifactStore
from polaris.channels import (
    ChannelAdapter,
    ChannelStore,
    OutboxRecord,
    Platform,
)
from polaris.ensemble import BudgetSpec, EnsembleResearchEngine, WorkerSpec
from polaris.harness import ChannelHarness
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
from polaris.memory import (
    MemoryEntry,
    MemoryHit,
    MemoryKind,
    MemoryScope,
    MemoryStore,
    MemoryTools,
    MemoryWrite,
    TrustLevel,
)
from polaris.providers import Provider, ProviderConfigurationError
from polaris.runtime import AgentRuntime, DefaultApprovalPolicy, RuntimeConfig, recorded_replay
from polaris.scheduler import (
    CatchupPolicy,
    Job,
    JobPayload,
    JobRun,
    JobRunStatus,
    JobState,
    SchedulerEngine,
    SchedulerStore,
    ScheduleSpec,
)
from polaris.tools import ToolRegistry

from .config import AppConfig, secret_from_env
from .factory import build_tools, create_providers

_TERMINAL = {RunStatus.COMPLETED, RunStatus.FAILED, RunStatus.CANCELLED}
_UNSET = object()


class _ProvenanceUpdates(TypedDict, total=False):
    provenance_run_id: str | None
    provenance_session_id: str | None
    provenance_message_id: str | None


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
        memory_store: MemoryStore | None = None,
        scheduler_store: SchedulerStore | None = None,
        channel_harness: ChannelHarness | None = None,
        channel_store: ChannelStore | None = None,
        telegram_adapter: ChannelAdapter | None = None,
        slack_adapter: ChannelAdapter | None = None,
        env: dict[str, str] | None = None,
        api_token: str | None = None,
    ) -> None:
        if not isinstance(config, AppConfig):
            raise TypeError("config must be an AppConfig")
        self.config = config
        self.paths = config.paths
        self.paths.ensure()
        self.journal = journal or Journal(self.paths.journal_file)
        self.artifact_store = artifact_store or ArtifactStore(self.paths.artifact_dir)
        self.providers = (
            dict(providers) if providers is not None else create_providers(config, env=env)
        )
        self.tools = tools or build_tools(config)
        secret_names = [
            spec.api_key_env
            for spec in config.providers.values()
            if spec.api_key_env is not None
        ]
        channel_names = (
            config.channels.telegram.token_env,
            config.channels.slack.bot_token_env,
            config.channels.slack.app_token_env,
            config.daemon.api_token_env,
        )
        secret_names.extend(name for name in channel_names if name is not None)
        scanner_secrets = [
            value
            for name in secret_names
            if (value := secret_from_env(name, env)) is not None
        ]
        if api_token:
            scanner_secrets.append(api_token)
        self.memory_store = memory_store or MemoryStore(
            self.paths.journal_file,
            configured_secrets=scanner_secrets,
        )
        if memory_store is not None:
            self.memory_store.add_configured_secrets(scanner_secrets)
        self.scheduler_store = scheduler_store or SchedulerStore(self.paths.journal_file)
        self._tasks: dict[str, asyncio.Task[Any]] = {}
        self._task_failures: dict[str, str] = {}
        self._closed = False
        self.scheduler = SchedulerEngine(
            self.scheduler_store,
            self._submit_scheduled,
            self._deliver_scheduled,
            lease=config.scheduler.lease_seconds,
            batch=config.scheduler.batch,
            tick_seconds=config.scheduler.tick_seconds,
            startup_cap=config.scheduler.startup_cap,
            wait=self._wait_scheduled,
        )
        self.scheduler_engine = self.scheduler
        self.channels = channel_harness or ChannelHarness(
            self,
            config,
            store=channel_store,
            telegram_adapter=telegram_adapter,
            slack_adapter=slack_adapter,
            env=env,
        )
        self.channel_harness = self.channels
        self.channel_store = self.channels.store

    def add_runtime_secrets(self, secrets: Sequence[str]) -> None:
        self.memory_store.add_configured_secrets(secrets)

    def _runtime_config(self, persisted: Mapping[str, Any] | None = None) -> RuntimeConfig:
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
            memory_context=(
                str(value["memory_context"])
                if isinstance(value.get("memory_context"), str)
                else None
            ),
            memory_scope=(
                {
                    "profile_id": str(value["memory_scope"]["profile_id"]),
                    "subject_key": str(value["memory_scope"]["subject_key"]),
                }
                if isinstance(value.get("memory_scope"), Mapping)
                and isinstance(value["memory_scope"].get("profile_id"), str)
                and isinstance(value["memory_scope"].get("subject_key"), str)
                else None
            ),
            memory_snapshot_hash=(
                str(value["memory_snapshot_hash"])
                if isinstance(value.get("memory_snapshot_hash"), str)
                else None
            ),
        )

    def _run_tools(self, persisted: Mapping[str, Any] | None) -> ToolRegistry:
        registry = self.tools.clone()
        if not self.config.memory.enabled or not self.config.memory.tool_enabled:
            return registry
        value = persisted or {}
        scope_value = value.get("memory_scope")
        if not isinstance(scope_value, Mapping):
            return registry
        profile_id = scope_value.get("profile_id")
        subject_key = scope_value.get("subject_key")
        if not isinstance(profile_id, str) or not isinstance(subject_key, str):
            return registry
        memory_tools = MemoryTools(
            self.memory_store,
            MemoryScope(profile_id, subject_key),
        )
        for entry in memory_tools.entries():
            registry.register(entry)
        return registry

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
            self._run_tools(persisted),
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
        memory_scope: MemoryScope | None = None,
        profile_id: str | None = None,
        subject_key: str | None = None,
        external_key: str | None = None,
    ) -> RunRecord:
        provider_name = provider or self._default_provider()
        scope = self._resolve_memory_scope(memory_scope, profile_id, subject_key)
        persisted: dict[str, Any] = {
            "memory_scope": {
                "profile_id": scope.profile_id,
                "subject_key": scope.subject_key,
            }
        }
        if self.config.memory.enabled:
            snapshot = self.memory_store.session_snapshot(
                scope,
                char_budget=self.config.memory.char_budget,
            )
            persisted["memory_snapshot_hash"] = snapshot.hash
            persisted["memory_context"] = self.memory_store.render_prompt_context(
                snapshot,
                char_budget=self.config.memory.char_budget,
            )
        run = self._runtime(provider_name, persisted).create_run(
            prompt,
            budget=budget,
            external_key=external_key,
        )
        if schedule:
            self._schedule(run.id)
        return run

    def _resolve_memory_scope(
        self,
        scope: MemoryScope | None,
        profile_id: str | None,
        subject_key: str | None,
    ) -> MemoryScope:
        if scope is not None and (profile_id is not None or subject_key is not None):
            raise ValueError("pass memory_scope or profile_id/subject_key, not both")
        if scope is not None:
            return scope
        return MemoryScope(
            profile_id or self.config.memory.profile_id,
            subject_key or "local",
        )

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
            exception = task.exception()
            if exception is not None:
                self._task_failures[run_id] = (
                    f"{type(exception).__name__}: {exception}"
                )

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
        self.scheduler_store.recover_stale_runs()
        if self.config.scheduler.enabled:
            await self.scheduler.start()
        try:
            await self.channels.startup()
        except BaseException:
            if self.config.scheduler.enabled:
                await self.scheduler.close(drain=True)
            raise
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

    def background_failures(self) -> Mapping[str, str]:
        failures = dict(self._task_failures)
        scheduler_task = self.scheduler._ticker
        if (
            scheduler_task is not None
            and scheduler_task.done()
            and not scheduler_task.cancelled()
            and scheduler_task.exception() is not None
        ):
            exception = scheduler_task.exception()
            assert exception is not None
            failures["scheduler"] = f"{type(exception).__name__}: {exception}"
        return failures

    @staticmethod
    def _safe_memory_entry(entry: MemoryEntry) -> MemoryEntry:
        if entry.blocked_reason is None:
            return entry
        return replace(entry, content="[BLOCKED: unsafe memory content]")

    def memory_list(
        self,
        scope: MemoryScope | None = None,
        *,
        profile_id: str | None = None,
        subject_key: str | None = None,
        include_tombstones: bool = False,
        limit: int | None = None,
    ) -> tuple[MemoryEntry, ...]:
        selected = self._resolve_memory_scope(scope, profile_id, subject_key)
        return tuple(
            self._safe_memory_entry(entry)
            for entry in self.memory_store.list(
                selected,
                include_tombstones=include_tombstones,
                limit=limit,
            )
        )

    def memory_search(
        self,
        scope: MemoryScope | None,
        query: str,
        *,
        profile_id: str | None = None,
        subject_key: str | None = None,
        limit: int = 10,
    ) -> tuple[MemoryHit, ...]:
        selected = self._resolve_memory_scope(scope, profile_id, subject_key)
        return tuple(
            replace(hit, entry=self._safe_memory_entry(hit.entry))
            for hit in self.memory_store.recall(query, selected, limit)
        )

    def memory_add(
        self,
        scope: MemoryScope | None,
        content: str,
        *,
        profile_id: str | None = None,
        subject_key: str | None = None,
        kind: MemoryKind | str = MemoryKind.FACT,
        trust_level: TrustLevel | str = TrustLevel.USER_ASSERTED,
        provenance_run_id: str | None = None,
        provenance_session_id: str | None = None,
        provenance_message_id: str | None = None,
        idempotency_key: str | None = None,
    ) -> MemoryEntry:
        selected = self._resolve_memory_scope(scope, profile_id, subject_key)
        write = MemoryWrite(
            content,
            kind=MemoryKind(kind),
            trust_level=TrustLevel(trust_level),
            provenance_run_id=provenance_run_id,
            provenance_session_id=provenance_session_id,
            provenance_message_id=provenance_message_id,
        )
        entry = (
            self.memory_store.append_idempotent(selected, write, idempotency_key)
            if idempotency_key is not None
            else self.memory_store.append(selected, write)
        )
        return self._safe_memory_entry(
            entry
        )

    def memory_revise(
        self,
        scope: MemoryScope | None,
        entry_id: str,
        content: str,
        *,
        expected_revision: int,
        expected_hash: str | None = None,
        profile_id: str | None = None,
        subject_key: str | None = None,
        kind: MemoryKind | str | None = None,
        trust_level: TrustLevel | str | None = None,
        provenance_run_id: object = _UNSET,
        provenance_session_id: object = _UNSET,
        provenance_message_id: object = _UNSET,
    ) -> MemoryEntry:
        selected = self._resolve_memory_scope(scope, profile_id, subject_key)
        provenance: _ProvenanceUpdates = {}
        def checked_provenance(value: object) -> str | None:
            if value is not None and not isinstance(value, str):
                raise ValueError("provenance values must be strings or null")
            return value

        if provenance_run_id is not _UNSET:
            provenance["provenance_run_id"] = checked_provenance(provenance_run_id)
        if provenance_session_id is not _UNSET:
            provenance["provenance_session_id"] = checked_provenance(
                provenance_session_id
            )
        if provenance_message_id is not _UNSET:
            provenance["provenance_message_id"] = checked_provenance(
                provenance_message_id
            )
        return self._safe_memory_entry(
            self.memory_store.replace(
                selected,
                entry_id,
                content,
                expected_revision=expected_revision,
                expected_hash=expected_hash,
                kind=None if kind is None else MemoryKind(kind),
                trust_level=None if trust_level is None else TrustLevel(trust_level),
                **provenance,
            )
        )

    def memory_remove(
        self,
        scope: MemoryScope | None,
        entry_id: str,
        *,
        expected_revision: int,
        expected_hash: str | None = None,
        profile_id: str | None = None,
        subject_key: str | None = None,
    ) -> MemoryEntry:
        selected = self._resolve_memory_scope(scope, profile_id, subject_key)
        return self._safe_memory_entry(
            self.memory_store.remove(
                selected,
                entry_id,
                expected_revision=expected_revision,
                expected_hash=expected_hash,
            )
        )

    async def _submit_scheduled(self, payload: JobPayload) -> str:
        request = payload.request
        if payload.mode == "single":
            prompt = request.get("prompt")
            if not isinstance(prompt, str):
                raise ValueError("scheduled single request requires prompt")
            run = await self.submit_single(
                prompt,
                provider=(
                    str(request["provider"])
                    if isinstance(request.get("provider"), str)
                    else None
                ),
                budget=(
                    request["budget"]
                    if isinstance(request.get("budget"), Mapping)
                    else None
                ),
                schedule=False,
                profile_id=(
                    str(request["profile_id"])
                    if isinstance(request.get("profile_id"), str)
                    else None
                ),
                subject_key=(
                    str(request["subject_key"])
                    if isinstance(request.get("subject_key"), str)
                    else None
                ),
            )
        elif payload.mode == "fanout":
            question = request.get("question")
            workers_value = request.get("workers")
            verifier = request.get("verifier")
            synthesizer = request.get("synthesizer")
            if (
                not isinstance(question, str)
                or not isinstance(workers_value, Sequence)
                or isinstance(workers_value, (str, bytes))
                or not isinstance(verifier, str)
                or not isinstance(synthesizer, str)
            ):
                raise ValueError("scheduled fanout request is invalid")
            workers = tuple(
                WorkerSpec(
                    id=str(worker["id"]),
                    provider_name=str(worker.get("provider", worker.get("provider_name"))),
                    role=str(worker["role"]),
                    instructions=str(
                        worker.get("instructions", "Research the question and cite evidence.")
                    ),
                )
                for worker in workers_value
                if isinstance(worker, Mapping)
            )
            run = await self.submit_fanout(
                question,
                workers,
                verifier=verifier,
                synthesizer=synthesizer,
                budget=request.get("budget", {}),
                max_workers=(
                    int(request["max_workers"])
                    if isinstance(request.get("max_workers"), int)
                    else None
                ),
                schedule=False,
            )
        else:
            question = request.get("question")
            provider = request.get("provider")
            if not isinstance(question, str) or not isinstance(provider, str):
                raise ValueError("scheduled foundry-router request is invalid")
            run = await self.submit_foundry_router(
                question,
                provider=provider,
                budget=request.get("budget", {}),
                schedule=False,
            )
        self._schedule(run.id)
        return run.id

    async def _wait_scheduled(self, run_id: str, job_run_id: str) -> None:
        while True:
            occurrence = self.scheduler_store.get_run(job_run_id)
            if occurrence.cancel_requested:
                await self.cancel(run_id)
                raise RuntimeError(f"scheduled agent run {run_id} was cancelled")
            run = self.get(run_id)
            if run.status is RunStatus.COMPLETED:
                return
            if run.status in {RunStatus.FAILED, RunStatus.CANCELLED}:
                raise RuntimeError(f"scheduled agent run {run.id} ended as {run.status.value}")
            await asyncio.sleep(min(self.config.scheduler.tick_seconds, 0.1))

    async def _deliver_scheduled(
        self, target: Mapping[str, Any], run_id: str
    ) -> None:
        self.channels.enqueue_delivery(target, run_id)

    def create_job(
        self,
        schedule: ScheduleSpec,
        payload: JobPayload,
        *,
        name: str = "",
        catchup_policy: CatchupPolicy | str = CatchupPolicy.FIRE_ONCE,
        max_catchup: int = 1,
        grace_seconds: float = 0,
    ) -> Job:
        job = self.scheduler_store.create_job(
            schedule,
            payload,
            name=name,
            catchup_policy=catchup_policy,
            max_catchup=max_catchup,
            grace_seconds=grace_seconds,
        )
        self.scheduler.wake()
        return job

    def list_jobs(self, state: JobState | str | None = None) -> tuple[Job, ...]:
        return self.scheduler_store.list_jobs(state=state)

    def get_job(self, job_id: str) -> Job:
        return self.scheduler_store.get_job(job_id)

    def pause_job(self, job_id: str) -> Job:
        return self.scheduler_store.pause_job(job_id)

    def resume_job(self, job_id: str) -> Job:
        job = self.scheduler_store.resume_job(job_id)
        self.scheduler.wake()
        return job

    def cancel_job(self, job_id: str) -> Job:
        job = self.scheduler_store.cancel_job(job_id)
        for occurrence in self.scheduler_store.list_runs(job_id=job_id):
            if (
                occurrence.status in {JobRunStatus.CLAIMED, JobRunStatus.RUNNING}
                and occurrence.cancel_requested
                and occurrence.polaris_run_id is not None
            ):
                task = self._tasks.get(occurrence.polaris_run_id)
                if task is not None:
                    task.cancel()
                else:
                    run = self.get(occurrence.polaris_run_id)
                    if run.status not in _TERMINAL:
                        with suppress(InvalidTransitionError):
                            self.journal.mark_run_status(
                                occurrence.polaris_run_id,
                                RunStatus.CANCELLED,
                            )
        return job

    def preview_schedule(
        self,
        schedule: ScheduleSpec,
        *,
        after: datetime,
        count: int = 5,
    ) -> tuple[datetime, ...]:
        return self.scheduler_store.preview(schedule, after=after, count=count)

    def list_job_runs(
        self,
        *,
        job_id: str | None = None,
        status: str | None = None,
    ) -> tuple[JobRun, ...]:
        return self.scheduler_store.list_runs(job_id=job_id, status=status)

    def retry_job_run(self, run_id: str) -> JobRun:
        return self.scheduler.retry(run_id)

    def channels_status(self) -> dict[str, object]:
        status = self.channels.status()
        status["background_failures"] = dict(self.background_failures())
        return status

    def unknown_outbox(
        self,
        *,
        platform: Platform | str | None = None,
        limit: int = 500,
    ) -> tuple[OutboxRecord, ...]:
        return self.channel_store.list_unknown_outbox(platform=platform, limit=limit)

    def mark_outbox_sent(self, idempotency_key: str, *, note: str) -> OutboxRecord:
        return self.channel_store.resolve_outbox(
            idempotency_key,
            sent=True,
            note=note,
        )

    def retry_outbox(self, idempotency_key: str, *, note: str) -> OutboxRecord:
        return self.channel_store.retry_outbox(idempotency_key, note=note)

    async def close(self) -> None:
        if self._closed:
            return
        await self.scheduler.close(drain=True)
        await self.channels.close()
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
        self.scheduler_store.close()
        self.memory_store.close()
        self.journal.close()

    aclose = close
    list = list_runs
