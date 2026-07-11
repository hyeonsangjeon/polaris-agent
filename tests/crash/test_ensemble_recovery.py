from __future__ import annotations

import asyncio
import hashlib
from collections.abc import Mapping, Sequence
from pathlib import Path

import pytest

from polaris.artifacts import ArtifactStore
from polaris.ensemble import EnsembleResearchEngine, WorkerSpec
from polaris.journal import Budget, Journal, RunStatus, StepStatus
from polaris.providers import CompletionResult, Message, Provider, ProviderConfig, Usage
from polaris.runtime import RuntimeConfig
from polaris.tools import ToolRegistry
from polaris.tools.registry import JsonValue

LEASE_SECONDS = 2.0
LEASE_EXPIRY_WAIT = 2.5
SLOW_PROVIDER_DELAY = 5.0


def verification_json() -> str:
    quote = "an exact quote"
    return (
        "{"
        '"claims":[{"id":"c1","statement":"The conclusion is contested.",'
        '"evidence_ids":["e1"],"supporters":["w1"],"opponents":["w2"],'
        '"status":"disputed","confidence":0.6}],'
        '"evidence":[{"source_id":"e1","quote":"an exact quote","content_hash":"'
        f'{hashlib.sha256(quote.encode()).hexdigest()}"'
        "}]}"
    )


class CrashProvider(Provider):
    def __init__(self, kind: str) -> None:
        self.kind = kind
        self.config = ProviderConfig(
            model=f"requested-{kind}",
            base_url="http://localhost",
            timeout_seconds=LEASE_SECONDS,
        )
        self.calls: dict[str, int] = {}
        self.crashed = False

    async def complete(
        self,
        messages: Sequence[Message],
        tools: Sequence[Mapping[str, JsonValue]] | None = None,
        response_schema: Mapping[str, JsonValue] | None = None,
    ) -> CompletionResult:
        prompt = str(messages[-1].content)
        system = str(messages[0].content)
        label = "stable" if system == "stable" else self.kind
        self.calls[label] = self.calls.get(label, 0) + 1
        if self.kind == "worker" and "crasher" in prompt and not self.crashed:
            self.crashed = True
            await asyncio.sleep(0.01)
            raise RuntimeError("simulated daemon death")
        if self.kind == "verifier":
            text = verification_json()
        elif self.kind == "synth" and not self.crashed:
            self.crashed = True
            raise asyncio.CancelledError
        elif self.kind == "synth":
            text = "# Recovered report"
        else:
            text = f"{label} memo with evidence e1"
        return CompletionResult(
            Message("assistant", text),
            f"actual-{self.kind}",
            Usage(prompt_tokens=1, completion_tokens=1, total_tokens=2),
        )

    async def list_models(self) -> tuple[str, ...]:
        return (self.config.model,)

    async def doctor(self) -> Mapping[str, JsonValue]:
        return {"ok": True}

    async def aclose(self) -> None:
        return None


def runtime_config(worker: WorkerSpec) -> RuntimeConfig:
    return RuntimeConfig(
        system_prompt=worker.role,
        worker_id=f"worker-{worker.id}",
        lease_seconds=LEASE_SECONDS,
        reservation_tokens=50,
    )


def worker_specs() -> tuple[WorkerSpec, ...]:
    return (
        WorkerSpec(
            id="w1",
            provider_name="workers",
            role="stable",
            instructions="stable",
        ),
        WorkerSpec(
            id="w2",
            provider_name="workers",
            role="crasher",
            instructions="crasher",
        ),
    )


@pytest.mark.asyncio
async def test_resume_only_recoverable_worker_and_skip_committed_worker(tmp_path: Path) -> None:
    worker = CrashProvider("worker")
    verifier = CrashProvider("verifier")
    synth = CrashProvider("plain-synth")
    journal = Journal(tmp_path / "journal.sqlite3")
    engine = EnsembleResearchEngine(
        journal,
        ArtifactStore(tmp_path / "artifacts"),
        {"workers": worker, "verify": verifier, "synth": synth},
        ToolRegistry(),
        runtime_config_factory=runtime_config,
    )
    run = engine.create_run(
        "Question",
        worker_specs(),
        "verify",
        "synth",
        Budget(call_limit=8, token_limit=400),
    )
    with pytest.raises(ExceptionGroup):
        await engine.execute(run.id)
    assert journal.get_run(run.id).status is RunStatus.RUNNING
    await asyncio.sleep(LEASE_EXPIRY_WAIT)

    result = await engine.execute(run.id)
    assert result.report
    assert worker.calls["stable"] == 1
    assert worker.calls["worker"] == 2
    journal.close()


@pytest.mark.asyncio
async def test_crash_after_verifier_commit_does_not_rerun_verifier(tmp_path: Path) -> None:
    worker = CrashProvider("worker")
    worker.crashed = True
    verifier = CrashProvider("verifier")
    synth = CrashProvider("synth")
    journal = Journal(tmp_path / "journal.sqlite3")
    engine = EnsembleResearchEngine(
        journal,
        ArtifactStore(tmp_path / "artifacts"),
        {"workers": worker, "verify": verifier, "synth": synth},
        ToolRegistry(),
        runtime_config_factory=runtime_config,
    )
    run = engine.create_run(
        "Question",
        worker_specs(),
        "verify",
        "synth",
        Budget(call_limit=8, token_limit=400),
    )
    with pytest.raises(asyncio.CancelledError):
        await engine.execute(run.id)
    assert sum(verifier.calls.values()) == 1
    synthesis = next(
        step
        for step in journal.list_steps(run.id)
        if step.kind == "ensemble-provider" and step.name == "synthesis"
    )
    assert synthesis.status is StepStatus.EXECUTING
    assert synthesis.lease_expires_at is not None
    assert run.config["provider_lease_seconds"] == {
        "verify": LEASE_SECONDS,
        "synth": LEASE_SECONDS,
    }
    await asyncio.sleep(LEASE_EXPIRY_WAIT)
    journal.reclaim_expired_leases()

    result = await engine.execute(run.id)
    assert result.report
    assert sum(verifier.calls.values()) == 1
    assert synth.calls["synth"] == 2
    journal.close()


@pytest.mark.asyncio
async def test_slow_ensemble_provider_call_keeps_lease_alive(tmp_path: Path) -> None:
    worker = CrashProvider("worker")
    worker.crashed = True
    verifier = CrashProvider("verifier")
    synth = CrashProvider("plain-synth")
    original_complete = verifier.complete

    async def slow_complete(
        messages: Sequence[Message],
        tools: Sequence[Mapping[str, JsonValue]] | None = None,
        response_schema: Mapping[str, JsonValue] | None = None,
    ) -> CompletionResult:
        await asyncio.sleep(SLOW_PROVIDER_DELAY)
        return await original_complete(messages, tools, response_schema)

    verifier.complete = slow_complete  # type: ignore[method-assign]
    journal = Journal(tmp_path / "slow.sqlite3")
    engine = EnsembleResearchEngine(
        journal,
        ArtifactStore(tmp_path / "artifacts"),
        {"workers": worker, "verify": verifier, "synth": synth},
        ToolRegistry(),
        runtime_config_factory=runtime_config,
    )
    run = engine.create_run(
        "Question",
        worker_specs(),
        "verify",
        "synth",
        Budget(call_limit=8, token_limit=400),
    )

    result = await engine.execute(run.id)
    verifier_step = next(
        step
        for step in journal.list_steps(run.id)
        if step.kind == "ensemble-provider" and step.name == "claim-extraction-verification"
    )
    assert result.report
    assert verifier_step.status is StepStatus.COMMITTED
    assert verifier_step.attempt_count == 1
    journal.close()
