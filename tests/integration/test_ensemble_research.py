from __future__ import annotations

import asyncio
import hashlib
from collections.abc import Mapping, Sequence
from pathlib import Path

import pytest

from polaris.artifacts import ArtifactStore
from polaris.ensemble import EnsembleResearchEngine, EnsembleResearchError, WorkerSpec
from polaris.journal import Budget, BudgetExceededError, Journal, RunStatus
from polaris.providers import CompletionResult, Message, Provider, ProviderConfig, Usage
from polaris.tools import ToolRegistry
from polaris.tools.registry import JsonValue


def completion(text: str, model: str, tokens: int = 2) -> CompletionResult:
    return CompletionResult(
        Message("assistant", text),
        model,
        Usage(prompt_tokens=1, completion_tokens=tokens - 1, total_tokens=tokens),
    )


class ResearchProvider(Provider):
    def __init__(self, kind: str, model: str, verification: str = "") -> None:
        self.kind = kind
        self.config = ProviderConfig(model=f"requested-{model}", base_url="http://localhost")
        self.actual_model = f"actual-{model}"
        self.verification = verification
        self.calls = 0
        self.active = 0
        self.max_active = 0
        self.started = asyncio.Event()

    async def complete(
        self,
        messages: Sequence[Message],
        tools: Sequence[Mapping[str, JsonValue]] | None = None,
        response_schema: Mapping[str, JsonValue] | None = None,
    ) -> CompletionResult:
        self.calls += 1
        if self.kind == "worker":
            self.active += 1
            self.max_active = max(self.max_active, self.active)
            if self.active >= 2:
                self.started.set()
            await asyncio.wait_for(self.started.wait(), 1)
            await asyncio.sleep(0.01)
            self.active -= 1
            prompt = str(messages[-1].content)
            role = "optimist" if "optimist" in prompt else "skeptic"
            return completion(f"{role} memo with source e1 and an exact quote.", self.actual_model)
        if self.kind == "verifier":
            assert response_schema is not None
            return completion(self.verification, self.actual_model)
        return completion("# Report\n\nThe evidence is disputed.", self.actual_model)

    async def list_models(self) -> tuple[str, ...]:
        return (self.config.model,)

    async def doctor(self) -> Mapping[str, JsonValue]:
        return {"ok": True}

    async def aclose(self) -> None:
        return None


def verification_json() -> str:
    quote = "an exact quote"
    return (
        "{"
        '"claims":[{"id":"c1","statement":"The conclusion is contested.",'
        '"evidence_ids":["e1"],"supporters":["w1"],"opponents":["w2"],'
        '"status":"disputed","confidence":0.6}],'
        '"evidence":[{"source_id":"e1","url":"https://example.test/source",'
        f'"title":"Source","quote":"{quote}","content_hash":"'
        f'{hashlib.sha256(quote.encode()).hexdigest()}"'
        "}]}"
    )


def workers(count: int = 2) -> tuple[WorkerSpec, ...]:
    values = []
    for index in range(count):
        values.append(
            WorkerSpec(
                id=f"w{index + 1}",
                provider_name="workers",
                role="optimist" if index % 2 == 0 else "skeptic",
                instructions="Investigate independently.",
            )
        )
    return tuple(values)


@pytest.mark.asyncio
async def test_local_pipeline_overlap_cap_artifacts_cost_and_replay(tmp_path: Path) -> None:
    worker_provider = ResearchProvider("worker", "worker")
    verifier = ResearchProvider("verifier", "verify", verification_json())
    synthesizer = ResearchProvider("synth", "synth")
    providers = {"workers": worker_provider, "verify": verifier, "synth": synthesizer}
    journal = Journal(tmp_path / "journal.sqlite3")
    store = ArtifactStore(tmp_path / "artifacts")
    engine = EnsembleResearchEngine(
        journal,
        store,
        providers,
        ToolRegistry(),
        max_workers=2,
        cost_estimator=lambda result: result.usage.total_tokens * 3,
    )
    run = engine.create_run(
        "What is the conclusion?",
        workers(),
        "verify",
        "synth",
        Budget(call_limit=8, token_limit=400, micro_usd_limit=100),
    )
    result = await engine.execute(run.id)

    assert journal.get_run(run.id).status is RunStatus.COMPLETED
    assert worker_provider.max_active == 2
    assert worker_provider.calls == 2
    assert verifier.calls == synthesizer.calls == 1
    assert "Opponents: w2" in result.report
    assert "Opponents: w2" in result.disagreements
    assert set(result.artifacts) == {
        "report.md",
        "claims.json",
        "evidence.jsonl",
        "disagreements.md",
        "run-graph.json",
        "cost.json",
        "manifest.json",
    }
    assert {item.name for item in journal.list_artifacts(run.id)} == set(result.artifacts)
    assert result.cost.actual_models["synthesizer"] == ("actual-synth",)
    assert result.cost.requested_models["synthesizer"] == "requested-synth"
    assert result.cost.micro_usd == 24

    def forbidden(_name: str) -> Provider:
        pytest.fail("replay must not resolve or call providers")

    replay = EnsembleResearchEngine(journal, store, forbidden, ToolRegistry()).replay(run.id)
    assert replay == result
    journal.close()


@pytest.mark.asyncio
async def test_semaphore_caps_four_workers_at_two(tmp_path: Path) -> None:
    worker_provider = ResearchProvider("worker", "worker")
    providers = {
        "workers": worker_provider,
        "verify": ResearchProvider("verifier", "verify", verification_json()),
        "synth": ResearchProvider("synth", "synth"),
    }
    journal = Journal(tmp_path / "journal.sqlite3")
    engine = EnsembleResearchEngine(
        journal, ArtifactStore(tmp_path / "artifacts"), providers, ToolRegistry(), max_workers=2
    )
    run = engine.create_run(
        "Question",
        workers(4),
        "verify",
        "synth",
        Budget(call_limit=12, token_limit=600),
    )
    await engine.execute(run.id)
    assert worker_provider.max_active == 2
    journal.close()


def test_fixed_budget_allocation_rejects_insufficient_calls(tmp_path: Path) -> None:
    provider = ResearchProvider("worker", "worker")
    engine = EnsembleResearchEngine(
        Journal(tmp_path / "journal.sqlite3"),
        ArtifactStore(tmp_path / "artifacts"),
        {"workers": provider, "verify": provider, "synth": provider},
        ToolRegistry(),
    )
    with pytest.raises(BudgetExceededError, match="at least 3"):
        engine.create_run(
            "Question", workers(1), "verify", "synth", Budget(call_limit=2)
        )


@pytest.mark.asyncio
async def test_invalid_verifier_schema_fails_explicitly(tmp_path: Path) -> None:
    worker = ResearchProvider("worker", "worker")
    providers = {
        "workers": worker,
        "verify": ResearchProvider("verifier", "verify", "{}"),
        "synth": ResearchProvider("synth", "synth"),
    }
    journal = Journal(tmp_path / "journal.sqlite3")
    engine = EnsembleResearchEngine(
        journal, ArtifactStore(tmp_path / "artifacts"), providers, ToolRegistry()
    )
    run = engine.create_run(
        "Question", workers(), "verify", "synth", Budget(call_limit=8, token_limit=400)
    )
    with pytest.raises(EnsembleResearchError, match="schema-invalid"):
        await engine.execute(run.id)
    assert journal.get_run(run.id).status is RunStatus.FAILED
    journal.close()


@pytest.mark.asyncio
async def test_worker_budget_exhaustion_fails_parent_precisely(tmp_path: Path) -> None:
    worker = ResearchProvider("worker", "worker")
    providers = {
        "workers": worker,
        "verify": ResearchProvider("verifier", "verify", verification_json()),
        "synth": ResearchProvider("synth", "synth"),
    }
    journal = Journal(tmp_path / "journal.sqlite3")
    engine = EnsembleResearchEngine(
        journal, ArtifactStore(tmp_path / "artifacts"), providers, ToolRegistry()
    )
    run = engine.create_run(
        "Question", workers(), "verify", "synth", Budget(call_limit=8, token_limit=4)
    )

    with pytest.raises(EnsembleResearchError, match="exhausted its token allocation"):
        await engine.execute(run.id)
    assert journal.get_run(run.id).status is RunStatus.FAILED
    assert not journal.list_provider_calls(run.id)
    journal.close()


@pytest.mark.asyncio
async def test_foundry_router_strategy_uses_one_routed_worker(tmp_path: Path) -> None:
    router = ResearchProvider("worker", "router", verification_json())
    quote = "an exact quote"
    routed_verification = (
        "{"
        '"claims":[{"id":"c1","statement":"The routed conclusion is supported.",'
        '"evidence_ids":["e1"],"supporters":["foundry-router"],"opponents":[],'
        '"status":"consensus","confidence":0.7}],'
        '"evidence":[{"source_id":"e1","url":"https://example.test/source",'
        f'"title":"Source","quote":"{quote}","content_hash":"'
        f'{hashlib.sha256(quote.encode()).hexdigest()}"'
        "}]}"
    )
    responses = iter(
        [
            completion("router memo with source e1 and an exact quote.", "actual-worker"),
            completion(routed_verification, "actual-verifier"),
            completion("# Routed report", "actual-synthesizer"),
        ]
    )

    async def complete_in_order(
        messages: Sequence[Message],
        tools: Sequence[Mapping[str, JsonValue]] | None = None,
        response_schema: Mapping[str, JsonValue] | None = None,
    ) -> CompletionResult:
        router.calls += 1
        return next(responses)

    router.complete = complete_in_order  # type: ignore[method-assign]
    journal = Journal(tmp_path / "journal.sqlite3")
    engine = EnsembleResearchEngine(
        journal,
        ArtifactStore(tmp_path / "artifacts"),
        {"router": router},
        ToolRegistry(),
    )
    run = engine.create_foundry_router_run(
        "Question",
        "router",
        Budget(call_limit=3, token_limit=300),
    )
    result = await engine.execute(run.id)

    children = [item for item in journal.list_runs() if item.parent_run_id == run.id]
    assert len(children) == 1
    assert router.calls == 3
    assert result.cost.actual_models["foundry-router"] == ("actual-worker",)
    assert result.cost.actual_models["verifier"] == ("actual-verifier",)
    assert result.cost.actual_models["synthesizer"] == ("actual-synthesizer",)
    strategy = [
        event
        for event in journal.list_events(run.id)
        if event.type == "ensemble.strategy_selected"
    ]
    assert strategy[0].payload["routing_owner"] == "microsoft_foundry"
    journal.close()
