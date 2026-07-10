"""Durable fan-out research orchestration."""

from __future__ import annotations

import asyncio
import json
from collections.abc import AsyncIterator, Callable, Mapping, Sequence
from contextlib import asynccontextmanager, suppress
from typing import Any, cast

from polaris.artifacts import ArtifactStore
from polaris.journal import (
    Budget,
    BudgetExceededError,
    Journal,
    RunRecord,
    RunStatus,
    SafetyClass,
    StepStatus,
    canonical_json,
)
from polaris.providers import CompletionResult, Message, Provider
from polaris.runtime import (
    AgentRuntime,
    RuntimeConfig,
    deserialize_completion,
    serialize_completion,
    serialize_message,
)
from polaris.tools import ToolRegistry

from .models import (
    BudgetSpec,
    Claim,
    CostSummary,
    Evidence,
    ResearchConfig,
    ResearchResult,
    VerificationResult,
    WorkerResult,
    WorkerSpec,
    validate_evidence_integrity,
)

ProviderSource = Mapping[str, Provider] | Callable[[str], Provider]
RuntimeConfigFactory = Callable[[WorkerSpec], RuntimeConfig]
CostEstimator = Callable[[CompletionResult], int]

_EVIDENCE_CONTRACT = """Return a non-empty research memo. For every factual assertion,
include explicit evidence records using stable source_id values, source URL/title when
available, an exact supporting quote, and the lowercase SHA-256 content_hash of the
quoted/source content. Clearly identify uncertainty and disagreement. Never invent evidence."""


class EnsembleResearchError(RuntimeError):
    """The ensemble cannot produce a valid successful result."""


class EnsembleResearchEngine:
    """Execute a manually configured, durable research ensemble."""

    def __init__(
        self,
        journal: Journal,
        artifact_store: ArtifactStore,
        providers: ProviderSource | None = None,
        tools: ToolRegistry | None = None,
        config: ResearchConfig | None = None,
        runtime_config_factory: RuntimeConfigFactory | None = None,
        cost_estimator: CostEstimator | Mapping[str, CostEstimator] | None = None,
        *,
        max_workers: int | None = None,
        provider_factory: Callable[[str], Provider] | None = None,
        provider_map: Mapping[str, Provider] | None = None,
        tool_registry: ToolRegistry | None = None,
    ) -> None:
        if max_workers is not None and not 1 <= max_workers <= 8:
            raise ValueError("max_workers must be between 1 and 8")
        self.journal = journal
        self.artifact_store = artifact_store
        sources = [
            source for source in (providers, provider_factory, provider_map) if source is not None
        ]
        if len(sources) != 1:
            raise ValueError("configure exactly one provider map or factory")
        if tools is not None and tool_registry is not None:
            raise ValueError("pass either tools or tool_registry, not both")
        self.providers = sources[0]
        self.tools = tools or tool_registry or ToolRegistry()
        self.config = config
        self.runtime_config_factory = runtime_config_factory
        self.cost_estimator = cost_estimator
        self.max_workers = max_workers or (config.max_workers if config else 4)

    def create_run(
        self,
        question: str,
        workers: Sequence[WorkerSpec],
        verifier: str,
        synthesizer: str,
        budget: Budget | Mapping[str, Any] | BudgetSpec,
    ) -> RunRecord:
        if not isinstance(question, str) or not question.strip():
            raise ValueError("question must not be empty")
        if not workers or len(workers) > 8:
            raise ValueError("workers must contain between 1 and 8 WorkerSpec values")
        if any(not isinstance(worker, WorkerSpec) for worker in workers):
            raise TypeError("workers must contain WorkerSpec values")
        worker_ids = [worker.id for worker in workers]
        if len(set(worker_ids)) != len(worker_ids):
            raise ValueError("worker ids must be unique")
        if not verifier or not synthesizer:
            raise ValueError("verifier and synthesizer provider names must not be empty")

        requested_budget = self._budget_spec(budget)
        allocation = self._allocate_budget(requested_budget, len(workers))
        worker_allocations = {
            worker.id: self._apply_worker_cap(allocation["worker"]) for worker in workers
        }
        provider_lease_seconds = {
            provider_name: self._provider(provider_name).config.timeout_seconds
            for provider_name in {verifier, synthesizer}
        }
        persisted = {
            "version": 1,
            "max_workers": min(self.max_workers, len(workers)),
            "workers": [worker.to_dict() for worker in workers],
            "verifier": verifier,
            "synthesizer": synthesizer,
            "provider_lease_seconds": provider_lease_seconds,
            "output_language": self.config.output_language if self.config else "English",
            "requested_budget": requested_budget.to_dict(),
            "allocation": {
                "workers": worker_allocations,
                "verifier": allocation["verifier"],
                "synthesizer": allocation["synthesizer"],
            },
        }
        parent = self.journal.create_run(
            "fan-out",
            {"question": question.strip()},
            persisted,
            budget=self._journal_budget(requested_budget),
        )
        for worker in workers:
            worker_budget = BudgetSpec.model_validate(worker_allocations[worker.id])
            runtime_config = self._runtime_config(worker, worker_budget)
            provider = self._provider(worker.provider_name)
            prompt = self._worker_prompt(question.strip(), worker)
            child_config = {
                "provider": worker.provider_name,
                "model": provider.config.model,
                "system_prompt": runtime_config.system_prompt,
                "max_iterations": runtime_config.max_iterations,
                "lease_seconds": runtime_config.lease_seconds,
                "reservation_tokens": runtime_config.reservation_tokens,
                "reservation_calls": runtime_config.reservation_calls,
                "reservation_micro_usd": runtime_config.reservation_micro_usd,
                "no_progress_threshold": runtime_config.no_progress_threshold,
                "ensemble_worker": worker.to_dict(),
            }
            child = self.journal.create_run(
                "single",
                {"prompt": prompt},
                child_config,
                budget=self._journal_budget(worker_budget),
                parent_run_id=parent.id,
            )
            self.journal.append_event(
                parent.id,
                "ensemble.child_created",
                {"worker_id": worker.id, "child_run_id": child.id},
            )
        self.journal.append_event(
            parent.id,
            "ensemble.budget_allocated",
            {
                "policy": "fixed equal slots at create time; verifier and synthesizer reserve "
                "one slot each and workers share the remaining slots",
                "allocation": persisted["allocation"],
            },
        )
        return parent

    def create_foundry_router_run(
        self,
        question: str,
        provider_name: str,
        budget: Budget | Mapping[str, Any] | BudgetSpec,
    ) -> RunRecord:
        """Create the thin Foundry strategy without duplicating model routing.

        A single research worker, verifier, and synthesizer all call the same
        Model Router deployment. Foundry chooses the underlying model for each
        prompt; Polaris records those choices and adds only durable execution,
        evidence validation, disagreement handling, and replay.
        """

        worker = WorkerSpec(
            id="foundry-router",
            provider_name=provider_name,
            role="evidence-first researcher",
            instructions=(
                "Research broadly, cite exact source evidence, and surface uncertainty. "
                "Do not choose or request an underlying model; the Foundry deployment "
                "owns routing."
            ),
        )
        parent = self.create_run(
            question,
            (worker,),
            provider_name,
            provider_name,
            budget,
        )
        self.journal.append_event(
            parent.id,
            "ensemble.strategy_selected",
            {
                "strategy": "foundry_model_router",
                "provider": provider_name,
                "routing_owner": "microsoft_foundry",
            },
        )
        return parent

    async def execute(self, run_id: str) -> ResearchResult:
        run = self.journal.get_run(run_id)
        if run.mode != "fan-out":
            raise EnsembleResearchError(f"run {run_id!r} is not a fan-out run")
        if run.status is RunStatus.COMPLETED:
            return self.replay(run_id)
        if run.status in (RunStatus.FAILED, RunStatus.CANCELLED):
            raise EnsembleResearchError(f"run {run_id!r} is {run.status.value}")
        self.journal.reclaim_expired_leases()
        if self.journal.get_run(run_id).status in (RunStatus.CREATED, RunStatus.PAUSED):
            self.journal.mark_run_status(run_id, RunStatus.RUNNING)

        try:
            workers = self._workers(run)
            children = self._children(run_id)
            if set(children) != {worker.id for worker in workers}:
                raise EnsembleResearchError("parent run is missing durable worker children")
            semaphore = asyncio.Semaphore(int(cast(Mapping[str, Any], run.config)["max_workers"]))
            worker_results: dict[str, WorkerResult] = {}

            async def execute_worker(worker: WorkerSpec) -> None:
                async with semaphore:
                    worker_results[worker.id] = await self._execute_worker(
                        worker, children[worker.id]
                    )

            try:
                async with asyncio.TaskGroup() as group:
                    for worker in workers:
                        group.create_task(execute_worker(worker), name=f"ensemble-{worker.id}")
            except* EnsembleResearchError as group:
                raise EnsembleResearchError(str(group.exceptions[0])) from group

            ordered_workers = tuple(worker_results[worker.id] for worker in workers)
            verification = await self._verify(run, ordered_workers)
            report = await self._synthesize(run, ordered_workers, verification)
            result = self._write_outputs(run, ordered_workers, verification, report)
            self.journal.mark_run_status(run_id, RunStatus.COMPLETED)
            return result
        except EnsembleResearchError as exc:
            self._fail_parent(run_id, exc)
            raise
        except BudgetExceededError as exc:
            self._fail_parent(run_id, exc)
            raise EnsembleResearchError(str(exc)) from exc

    def replay(self, run_id: str) -> ResearchResult:
        run = self.journal.get_run(run_id)
        if run.status is not RunStatus.COMPLETED:
            raise EnsembleResearchError("only completed ensemble runs can be replayed")
        records = self.journal.list_artifacts(run_id)
        by_name = {record.name: record for record in records}
        expected = {
            "report.md",
            "claims.json",
            "evidence.jsonl",
            "disagreements.md",
            "run-graph.json",
            "cost.json",
            "manifest.json",
        }
        if set(by_name) != expected or len(records) != len(expected):
            raise EnsembleResearchError(
                "ensemble artifact set is incomplete or contains duplicates"
            )
        payloads: dict[str, bytes] = {}
        hashes: dict[str, str] = {}
        for name in sorted(expected):
            record = by_name[name]
            if record.sha256 is None:
                raise EnsembleResearchError(f"artifact {name!r} has no content hash")
            payloads[name] = self.artifact_store.get(record.sha256)
            hashes[name] = record.sha256

        claims_value = json.loads(payloads["claims.json"])
        claims = tuple(
            Claim.model_validate_json(canonical_json(item)) for item in claims_value
        )
        evidence = tuple(
            Evidence.model_validate_json(line)
            for line in payloads["evidence.jsonl"].splitlines()
            if line
        )
        graph = json.loads(payloads["run-graph.json"])
        workers = tuple(
            WorkerResult.model_validate_json(canonical_json(item)) for item in graph["workers"]
        )
        validate_evidence_integrity(claims, evidence, {worker.worker_id for worker in workers})
        cost = CostSummary.model_validate_json(payloads["cost.json"])
        return ResearchResult(
            run_id=run_id,
            question=str(graph["question"]),
            report=payloads["report.md"].decode("utf-8"),
            claims=claims,
            evidence=evidence,
            disagreements=payloads["disagreements.md"].decode("utf-8"),
            workers=workers,
            cost=cost,
            artifacts=hashes,
        )

    async def _execute_worker(self, worker: WorkerSpec, child: RunRecord) -> WorkerResult:
        existing = self._worker_artifact(child.id)
        if child.status is RunStatus.COMPLETED and existing is not None:
            return WorkerResult.model_validate_json(self.artifact_store.get(existing.sha256))
        if child.status in (RunStatus.FAILED, RunStatus.CANCELLED):
            raise EnsembleResearchError(
                f"worker {worker.id!r} child run is {child.status.value}"
            )
        active_steps = [
            step
            for step in self.journal.list_steps(child.id)
            if step.status in (StepStatus.LEASED, StepStatus.EXECUTING)
        ]
        if active_steps:
            raise RuntimeError(f"worker {worker.id!r} is not recoverable until its lease expires")
        allocation = self._worker_budget(
            self.journal.get_run(child.parent_run_id or "").config, worker
        )
        runtime_config = self._runtime_config(worker, allocation)
        runtime = AgentRuntime(
            self.journal,
            self._provider(worker.provider_name),
            worker.provider_name,
            self.tools,
            runtime_config,
            self._estimator(worker.provider_name),
        )
        result = await runtime.execute(child.id)
        if result.status is not RunStatus.COMPLETED:
            raise EnsembleResearchError(
                f"worker {worker.id!r} did not complete: {result.status.value}"
            )
        if result.final_text is None or not result.final_text.strip():
            raise EnsembleResearchError(f"worker {worker.id!r} returned empty output")
        if (
            allocation.token_limit is not None
            and result.usage.total_tokens > allocation.token_limit
        ):
            raise EnsembleResearchError(f"worker {worker.id!r} exhausted its token allocation")
        calls = [
            call
            for call in self.journal.list_provider_calls(child.id)
            if call.status == "completed"
        ]
        all_calls = self.journal.list_provider_calls(child.id)
        if allocation.call_limit is not None and len(all_calls) > allocation.call_limit:
            raise EnsembleResearchError(f"worker {worker.id!r} exhausted its call allocation")
        micro_usd = sum(call.micro_usd for call in calls)
        output_blob = self.artifact_store.put_text(result.final_text)
        worker_result = WorkerResult(
            worker_id=worker.id,
            run_id=child.id,
            output=result.final_text,
            requested_model=self._provider(worker.provider_name).config.model,
            actual_models=result.actual_models,
            prompt_tokens=result.usage.prompt_tokens,
            completion_tokens=result.usage.completion_tokens,
            micro_usd=micro_usd,
            artifact_hash=output_blob.sha256,
        )
        stored = self.artifact_store.put_json(worker_result.to_dict())
        self.journal.record_artifact(
            child.id,
            "worker-result.json",
            stored.uri,
            media_type="application/json",
            sha256=stored.sha256,
            size_bytes=stored.size_bytes,
            metadata={"worker_id": worker.id, "output_hash": output_blob.sha256},
        )
        self.journal.append_event(
            child.id,
            "ensemble.worker_output",
            {
                "worker_id": worker.id,
                "artifact_hash": stored.sha256,
                "usage": {
                    "input_tokens": result.usage.prompt_tokens,
                    "output_tokens": result.usage.completion_tokens,
                    "micro_usd": micro_usd,
                },
            },
        )
        return worker_result

    async def _verify(
        self, run: RunRecord, workers: tuple[WorkerResult, ...]
    ) -> VerificationResult:
        config = cast(Mapping[str, Any], run.config)
        provider_name = str(config["verifier"])
        messages = [
            Message(
                "system",
                "Extract and verify claims. Return only JSON matching the supplied schema. "
                "Preserve disagreements and never create evidence absent from worker memos.",
            ),
            Message(
                "user",
                canonical_json(
                    {
                        "question": cast(Mapping[str, Any], run.request)["question"],
                        "workers": [
                            {"worker_id": item.worker_id, "memo": item.output}
                            for item in workers
                        ],
                    }
                ),
            ),
        ]
        completion = await self._durable_provider_step(
            run,
            key_name="claim-extraction-verification",
            sequence=10_000,
            provider_name=provider_name,
            messages=messages,
            response_schema=VerificationResult.model_json_schema(),
            allocation=BudgetSpec.model_validate(
                dict(cast(Mapping[str, Any], config["allocation"]["verifier"]))
            ),
        )
        text = self._completion_text(completion, "verifier")
        try:
            verification = VerificationResult.model_validate_json(text)
        except Exception as exc:
            raise EnsembleResearchError("verifier returned schema-invalid JSON") from exc
        try:
            validate_evidence_integrity(
                verification.claims,
                verification.evidence,
                {worker.worker_id for worker in workers},
            )
        except ValueError as exc:
            raise EnsembleResearchError(f"verifier evidence integrity failed: {exc}") from exc
        return verification

    async def _synthesize(
        self,
        run: RunRecord,
        workers: tuple[WorkerResult, ...],
        verification: VerificationResult,
    ) -> str:
        config = cast(Mapping[str, Any], run.config)
        provider_name = str(config["synthesizer"])
        messages = [
            Message(
                "system",
                "Write a rigorous Markdown research report in "
                f"{config['output_language']}. Use only normalized claims/evidence. "
                "State uncertainty and preserve every disputed claim and its opposing workers.",
            ),
            Message(
                "user",
                canonical_json(
                    {
                        "question": cast(Mapping[str, Any], run.request)["question"],
                        "claims": [claim.to_dict() for claim in verification.claims],
                        "evidence": [item.to_dict() for item in verification.evidence],
                        "worker_excerpts": [
                            {"worker_id": worker.worker_id, "excerpt": worker.output[:4000]}
                            for worker in workers
                        ],
                    }
                ),
            ),
        ]
        completion = await self._durable_provider_step(
            run,
            key_name="synthesis",
            sequence=20_000,
            provider_name=provider_name,
            messages=messages,
            response_schema=None,
            allocation=BudgetSpec.model_validate(
                dict(cast(Mapping[str, Any], config["allocation"]["synthesizer"]))
            ),
        )
        report = self._completion_text(completion, "synthesizer").strip()
        disagreements = self._disagreements(verification.claims)
        if disagreements != "# Disagreements\n\nNone.\n":
            report = f"{report.rstrip()}\n\n{disagreements}"
        return report + ("\n" if not report.endswith("\n") else "")

    async def _durable_provider_step(
        self,
        run: RunRecord,
        *,
        key_name: str,
        sequence: int,
        provider_name: str,
        messages: Sequence[Message],
        response_schema: Mapping[str, Any] | None,
        allocation: BudgetSpec,
    ) -> CompletionResult:
        provider = self._provider(provider_name)
        request = {
            "provider": provider_name,
            "requested_model": provider.config.model,
            "messages": [serialize_message(message) for message in messages],
            "response_schema": response_schema,
        }
        step = self.journal.create_step(
            run.id,
            "ensemble-provider",
            key_name,
            request,
            SafetyClass.READ_ONLY,
            sequence=sequence,
        )
        if step.status is StepStatus.COMMITTED:
            if not isinstance(step.output, Mapping):
                raise EnsembleResearchError(f"committed {key_name} output is invalid")
            return deserialize_completion(step.output)
        if step.status is not StepStatus.READY:
            raise EnsembleResearchError(
                f"{key_name} step is not recoverable yet ({step.status.value})"
            )
        worker_id = f"ensemble-{key_name}"
        lease_seconds = self._provider_lease_seconds(run, provider_name, provider)
        claimed = self.journal.claim_ready_step(worker_id, lease_seconds, run.id)
        if claimed is None or claimed.id != step.id:
            raise EnsembleResearchError(f"could not claim {key_name} step")
        self.journal.mark_executing(step.id, worker_id)
        async with self._lease_heartbeat(step.id, worker_id, lease_seconds):
            prior_calls = [
                call for call in self.journal.list_provider_calls(run.id) if call.step_id == step.id
            ]
            if allocation.call_limit is not None and len(prior_calls) >= allocation.call_limit:
                self._refresh_lease(step.id, worker_id, lease_seconds)
                self.journal.fail_step(
                    step.id,
                    worker_id,
                    {"error": "BudgetExceededError", "message": "call allocation exhausted"},
                )
                raise BudgetExceededError(f"{key_name} call allocation exhausted")
            for abandoned in prior_calls:
                if abandoned.status == "started":
                    self.journal.complete_provider_call(
                        abandoned.id,
                        response={"warning": "lease expired before response commit"},
                        status="uncertain",
                    )
                    self.journal.append_event(
                        run.id,
                        "provider.duplicate_billing_warning",
                        {"provider_call_id": abandoned.id},
                        step_id=step.id,
                    )
            reservation_id = f"budget_{step.id}"
            call_id: str | None = None
            try:
                self._refresh_lease(step.id, worker_id, lease_seconds)
                self.journal.reserve_budget(
                    run.id,
                    calls=1,
                    tokens=allocation.token_limit or 0,
                    micro_usd=allocation.micro_usd_limit or 0,
                    reservation_id=reservation_id,
                )
                self._refresh_lease(step.id, worker_id, lease_seconds)
                call = self.journal.start_provider_call(
                    run.id,
                    provider_name,
                    request,
                    step_id=step.id,
                    model=provider.config.model,
                )
                call_id = call.id
                completion = await provider.complete(messages, (), response_schema)
                micro_usd = self._estimate(provider_name, completion)
                if (
                    allocation.token_limit is not None
                    and completion.usage.total_tokens > allocation.token_limit
                ):
                    raise BudgetExceededError(f"{key_name} token allocation exhausted")
                if (
                    allocation.micro_usd_limit is not None
                    and micro_usd > allocation.micro_usd_limit
                ):
                    raise BudgetExceededError(f"{key_name} cost allocation exhausted")
            except BaseException as exc:
                if isinstance(exc, (KeyboardInterrupt, SystemExit, asyncio.CancelledError)):
                    raise
                self._refresh_lease(step.id, worker_id, lease_seconds)
                if call_id is not None:
                    self.journal.complete_provider_call(
                        call_id,
                        response={"error": type(exc).__name__, "message": str(exc)},
                        status="failed",
                        model=provider.config.model,
                    )
                with suppress(Exception):
                    self.journal.settle_budget(run.id, reservation_id, actual_calls=1)
                self._refresh_lease(step.id, worker_id, lease_seconds)
                self.journal.fail_step(
                    step.id,
                    worker_id,
                    {"error": type(exc).__name__, "message": str(exc)},
                )
                if isinstance(exc, BudgetExceededError):
                    raise
                raise EnsembleResearchError(f"{key_name} provider failed: {exc}") from exc
            if call_id is None:
                raise RuntimeError("provider call bookkeeping was not initialized")
            self._refresh_lease(step.id, worker_id, lease_seconds)
            self.journal.complete_provider_call(
                call_id,
                response=serialize_completion(completion),
                model=completion.model,
                input_tokens=completion.usage.prompt_tokens,
                output_tokens=completion.usage.completion_tokens,
                micro_usd=micro_usd,
            )
            self._refresh_lease(step.id, worker_id, lease_seconds)
            self.journal.settle_budget(
                run.id,
                reservation_id,
                actual_calls=1,
                actual_tokens=completion.usage.total_tokens,
                actual_micro_usd=micro_usd,
            )
            self._refresh_lease(step.id, worker_id, lease_seconds)
            self.journal.commit_step(step.id, worker_id, serialize_completion(completion))
            return completion

    @staticmethod
    def _provider_lease_seconds(run: RunRecord, provider_name: str, provider: Provider) -> float:
        if isinstance(run.config, Mapping):
            persisted = run.config.get("provider_lease_seconds")
            if isinstance(persisted, Mapping):
                value = persisted.get(provider_name)
                if isinstance(value, (int, float)) and not isinstance(value, bool) and value > 0:
                    return float(value)
        timeout = provider.config.timeout_seconds
        return timeout if timeout > 0 else 30.0

    def _refresh_lease(self, step_id: str, worker_id: str, lease_seconds: float) -> None:
        self.journal.heartbeat(step_id, worker_id, lease_seconds)

    @asynccontextmanager
    async def _lease_heartbeat(
        self, step_id: str, worker_id: str, lease_seconds: float
    ) -> AsyncIterator[None]:
        interval = max(min(lease_seconds / 3, 5.0), 0.001)

        async def pulse() -> None:
            while True:
                await asyncio.sleep(interval)
                self._refresh_lease(step_id, worker_id, lease_seconds)

        task = asyncio.create_task(pulse(), name=f"ensemble-lease-heartbeat-{step_id}")
        try:
            yield
            if task.done() and not task.cancelled():
                task.result()
        finally:
            task.cancel()
            with suppress(asyncio.CancelledError):
                await task

    def _write_outputs(
        self,
        run: RunRecord,
        workers: tuple[WorkerResult, ...],
        verification: VerificationResult,
        report: str,
    ) -> ResearchResult:
        disagreements = self._disagreements(verification.claims)
        cost = self._cost_summary(run, workers)
        graph = {
            "run_id": run.id,
            "question": cast(Mapping[str, Any], run.request)["question"],
            "mode": "fan-out",
            "workers": [worker.to_dict() for worker in workers],
            "verifier": cast(Mapping[str, Any], run.config)["verifier"],
            "synthesizer": cast(Mapping[str, Any], run.config)["synthesizer"],
        }
        bodies: list[tuple[str, bytes, str]] = [
            ("report.md", report.encode(), "text/markdown"),
            (
                "claims.json",
                canonical_json([claim.to_dict() for claim in verification.claims]).encode(),
                "application/json",
            ),
            (
                "evidence.jsonl",
                (
                    "".join(canonical_json(item.to_dict()) + "\n" for item in verification.evidence)
                ).encode(),
                "application/x-ndjson",
            ),
            ("disagreements.md", disagreements.encode(), "text/markdown"),
            ("run-graph.json", canonical_json(graph).encode(), "application/json"),
            ("cost.json", canonical_json(cost.to_dict()).encode(), "application/json"),
        ]
        hashes: dict[str, str] = {}
        for name, body, media_type in bodies:
            hashes[name] = self._record_parent_artifact(run.id, name, body, media_type)
        manifest = {
            "version": 1,
            "run_id": run.id,
            "artifacts": dict(sorted(hashes.items())),
        }
        hashes["manifest.json"] = self._record_parent_artifact(
            run.id,
            "manifest.json",
            canonical_json(manifest).encode(),
            "application/json",
        )
        return ResearchResult(
            run_id=run.id,
            question=str(cast(Mapping[str, Any], run.request)["question"]),
            report=report,
            claims=verification.claims,
            evidence=verification.evidence,
            disagreements=disagreements,
            workers=workers,
            cost=cost,
            artifacts=hashes,
        )

    def _record_parent_artifact(
        self, run_id: str, name: str, body: bytes, media_type: str
    ) -> str:
        stored = self.artifact_store.put(body)
        existing = [item for item in self.journal.list_artifacts(run_id) if item.name == name]
        if existing:
            if len(existing) != 1 or existing[0].sha256 != stored.sha256:
                raise EnsembleResearchError(f"conflicting durable artifact {name!r}")
            return stored.sha256
        self.journal.record_artifact(
            run_id,
            name,
            stored.uri,
            media_type=media_type,
            sha256=stored.sha256,
            size_bytes=stored.size_bytes,
        )
        return stored.sha256

    def _cost_summary(
        self, run: RunRecord, workers: tuple[WorkerResult, ...]
    ) -> CostSummary:
        config = cast(Mapping[str, Any], run.config)
        requested = {worker.worker_id: worker.requested_model for worker in workers}
        actual = {worker.worker_id: worker.actual_models for worker in workers}
        input_tokens = sum(worker.prompt_tokens for worker in workers)
        output_tokens = sum(worker.completion_tokens for worker in workers)
        micro_usd = sum(worker.micro_usd for worker in workers)
        parent_calls = [
            call for call in self.journal.list_provider_calls(run.id) if call.status == "completed"
        ]
        for label, provider_name, call in zip(
            ("verifier", "synthesizer"),
            (str(config["verifier"]), str(config["synthesizer"])),
            parent_calls,
            strict=False,
        ):
            requested[label] = self._provider(provider_name).config.model
            actual[label] = (call.model,) if call.model else ()
        input_tokens += sum(call.input_tokens for call in parent_calls)
        output_tokens += sum(call.output_tokens for call in parent_calls)
        micro_usd += sum(call.micro_usd for call in parent_calls)
        all_calls = [
            call
            for worker in workers
            for call in self.journal.list_provider_calls(worker.run_id)
        ] + self.journal.list_provider_calls(run.id)
        budget = BudgetSpec.model_validate(
            dict(cast(Mapping[str, Any], config["requested_budget"]))
        )
        if budget.call_limit is not None and len(all_calls) > budget.call_limit:
            raise EnsembleResearchError("aggregate call budget exhausted")
        if budget.token_limit is not None and input_tokens + output_tokens > budget.token_limit:
            raise EnsembleResearchError("aggregate token budget exhausted")
        if budget.micro_usd_limit is not None and micro_usd > budget.micro_usd_limit:
            raise EnsembleResearchError("aggregate cost budget exhausted")
        return CostSummary(
            requested_models=requested,
            actual_models=actual,
            input_tokens=input_tokens,
            output_tokens=output_tokens,
            total_tokens=input_tokens + output_tokens,
            micro_usd=micro_usd,
            calls=len(all_calls),
            allocated_budget=budget,
        )

    def _children(self, parent_run_id: str) -> dict[str, RunRecord]:
        children: dict[str, RunRecord] = {}
        for child in self.journal.list_runs():
            if child.parent_run_id != parent_run_id:
                continue
            config = cast(Mapping[str, Any], child.config)
            worker = config.get("ensemble_worker")
            if isinstance(worker, Mapping):
                children[str(worker["id"])] = child
        return children

    @staticmethod
    def _workers(run: RunRecord) -> tuple[WorkerSpec, ...]:
        config = cast(Mapping[str, Any], run.config)
        return tuple(
            WorkerSpec.model_validate(dict(cast(Mapping[str, Any], item)))
            for item in config["workers"]
        )

    def _provider(self, name: str) -> Provider:
        if callable(self.providers):
            provider = self.providers(name)
        else:
            try:
                provider = self.providers[name]
            except KeyError as exc:
                raise EnsembleResearchError(f"provider {name!r} is not configured") from exc
        if not isinstance(provider, Provider):
            raise TypeError(f"provider {name!r} is not a Provider")
        return provider

    def _runtime_config(self, worker: WorkerSpec, budget: BudgetSpec) -> RuntimeConfig:
        if self.runtime_config_factory is not None:
            return self.runtime_config_factory(worker)
        return RuntimeConfig(
            system_prompt=f"You are the {worker.role} research worker. {worker.instructions}",
            worker_id=f"ensemble-worker-{worker.id}",
            reservation_calls=1,
            reservation_tokens=min(4096, budget.token_limit)
            if budget.token_limit is not None
            else 4096,
            reservation_micro_usd=min(0, budget.micro_usd_limit)
            if budget.micro_usd_limit is not None
            else 0,
        )

    def _estimator(self, provider_name: str) -> CostEstimator | None:
        if isinstance(self.cost_estimator, Mapping):
            return self.cost_estimator.get(provider_name)
        return self.cost_estimator

    def _estimate(self, provider_name: str, completion: CompletionResult) -> int:
        estimator = self._estimator(provider_name)
        return estimator(completion) if estimator else 0

    @staticmethod
    def _completion_text(completion: CompletionResult, label: str) -> str:
        content = completion.message.content
        if not isinstance(content, str) or not content.strip():
            raise EnsembleResearchError(f"{label} returned empty output")
        return content

    @staticmethod
    def _worker_prompt(question: str, worker: WorkerSpec) -> str:
        return (
            f"Research question:\n{question}\n\n"
            f"Your distinct role: {worker.role}\n"
            f"Instructions: {worker.instructions}\n\n"
            f"Evidence contract:\n{_EVIDENCE_CONTRACT}"
        )

    @staticmethod
    def _disagreements(claims: tuple[Claim, ...]) -> str:
        disputed = [claim for claim in claims if claim.status == "disputed"]
        if not disputed:
            return "# Disagreements\n\nNone.\n"
        lines = ["# Disagreements", ""]
        for claim in disputed:
            lines.extend(
                [
                    f"## {claim.id}",
                    "",
                    claim.statement,
                    "",
                    f"- Supporters: {', '.join(claim.supporters)}",
                    f"- Opponents: {', '.join(claim.opponents)}",
                    "",
                ]
            )
        return "\n".join(lines).rstrip() + "\n"

    @staticmethod
    def _budget_spec(value: Budget | Mapping[str, Any] | BudgetSpec) -> BudgetSpec:
        if isinstance(value, BudgetSpec):
            return value
        if isinstance(value, Budget):
            return BudgetSpec(
                call_limit=value.call_limit,
                token_limit=value.token_limit,
                micro_usd_limit=value.micro_usd_limit,
                wall_seconds_limit=value.wall_seconds_limit,
            )
        aliases = {
            "max_calls": "call_limit",
            "max_tokens": "token_limit",
            "max_micro_usd": "micro_usd_limit",
            "max_wall_seconds": "wall_seconds_limit",
        }
        normalized = {aliases.get(str(key), str(key)): item for key, item in value.items()}
        return BudgetSpec.model_validate(normalized)

    @staticmethod
    def _journal_budget(value: BudgetSpec) -> Budget:
        return Budget(**value.to_dict())

    @staticmethod
    def _allocate_budget(budget: BudgetSpec, worker_count: int) -> dict[str, dict[str, Any]]:
        slots = worker_count + 2

        if budget.call_limit is not None and budget.call_limit < slots:
            raise BudgetExceededError(
                f"call budget requires at least {slots} calls for workers, verifier, synthesizer"
            )
        slot = BudgetSpec(
            call_limit=budget.call_limit // slots if budget.call_limit is not None else None,
            token_limit=budget.token_limit // slots if budget.token_limit is not None else None,
            micro_usd_limit=budget.micro_usd_limit // slots
            if budget.micro_usd_limit is not None
            else None,
            wall_seconds_limit=budget.wall_seconds_limit / slots
            if budget.wall_seconds_limit is not None
            else None,
        ).to_dict()
        return {"worker": slot, "verifier": slot, "synthesizer": slot}

    def _apply_worker_cap(self, allocated: Mapping[str, Any]) -> dict[str, Any]:
        result = dict(allocated)
        if self.config is None:
            return result
        cap = self.config.worker_budget
        for name in (
            "call_limit",
            "token_limit",
            "micro_usd_limit",
            "wall_seconds_limit",
        ):
            value = result[name]
            maximum = getattr(cap, name)
            if maximum is not None and (value is None or maximum < value):
                result[name] = maximum
        return result

    @staticmethod
    def _worker_budget(config: object, worker: WorkerSpec) -> BudgetSpec:
        mapping = cast(Mapping[str, Any], config)
        value = mapping["allocation"]["workers"][worker.id]
        return BudgetSpec.model_validate(dict(cast(Mapping[str, Any], value)))

    def _worker_artifact(self, run_id: str) -> Any | None:
        records = [
            artifact
            for artifact in self.journal.list_artifacts(run_id)
            if artifact.name == "worker-result.json"
        ]
        if len(records) > 1:
            raise EnsembleResearchError("worker has duplicate result artifacts")
        return records[0] if records else None

    def _fail_parent(self, run_id: str, exc: BaseException) -> None:
        self.journal.append_event(
            run_id,
            "ensemble.failed",
            {"error": type(exc).__name__, "message": str(exc)},
        )
        run = self.journal.get_run(run_id)
        if run.status in (RunStatus.CREATED, RunStatus.RUNNING, RunStatus.PAUSED):
            self.journal.mark_run_status(run_id, RunStatus.FAILED)


__all__ = ["EnsembleResearchEngine", "EnsembleResearchError"]
