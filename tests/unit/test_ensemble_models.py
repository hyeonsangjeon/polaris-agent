from __future__ import annotations

import hashlib

import pytest
from pydantic import ValidationError

from polaris.ensemble import Claim, Evidence, WorkerSpec, validate_evidence_integrity
from polaris.ensemble.models import WorkerResult


def evidence(source_id: str = "e1") -> Evidence:
    quote = "quoted"
    return Evidence(
        source_id=source_id,
        quote=quote,
        content_hash=hashlib.sha256(quote.encode()).hexdigest(),
    )


def test_models_are_strict_frozen_and_serializable() -> None:
    worker = WorkerSpec(id="worker-1", provider_name="local", role="critic", instructions="Check")
    restored = WorkerSpec.from_json(worker.to_json())

    assert restored == worker
    with pytest.raises(ValidationError):
        WorkerSpec(id="bad/id", provider_name="local", role="critic", instructions="Check")
    with pytest.raises(ValidationError):
        WorkerResult(
            worker_id="w",
            run_id="r",
            output="memo",
            requested_model="m",
            actual_models=["m"],  # type: ignore[arg-type]
            artifact_hash="a" * 64,
        )
    with pytest.raises(ValidationError):
        worker.role = "changed"


def test_claim_status_and_evidence_integrity() -> None:
    disputed = Claim(
        id="c1",
        statement="A disputed statement",
        evidence_ids=("e1",),
        supporters=("w1",),
        opponents=("w2",),
        status="disputed",
        confidence=0.5,
    )
    validate_evidence_integrity((disputed,), (evidence(),), {"w1", "w2"})

    with pytest.raises(ValueError, match="missing evidence"):
        validate_evidence_integrity((disputed,), (), {"w1", "w2"})
    with pytest.raises(ValueError, match="unknown workers"):
        validate_evidence_integrity((disputed,), (evidence(),), {"w1"})
    with pytest.raises(ValidationError):
        Claim(
            id="c2",
            statement="Unsupported",
            evidence_ids=(),
            supporters=("w1",),
            opponents=(),
            status="unsupported",
            confidence=0.2,
        )
