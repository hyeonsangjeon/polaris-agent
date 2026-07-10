"""Strict immutable models for ensemble research."""

from __future__ import annotations

import hashlib
import re
from typing import Annotated, Any, Literal

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    StringConstraints,
    field_validator,
    model_validator,
)

from polaris.journal import Budget

NonEmpty = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1)]
Sha256 = Annotated[str, StringConstraints(pattern=r"^[0-9a-f]{64}$")]
ClaimStatus = Literal["consensus", "disputed", "unsupported"]


class StrictModel(BaseModel):
    """Frozen model with explicit JSON serialization and no unknown fields."""

    model_config = ConfigDict(frozen=True, extra="forbid", strict=True)

    def to_dict(self) -> dict[str, Any]:
        return self.model_dump(mode="json")

    @classmethod
    def from_dict(cls, value: object) -> StrictModel:
        return cls.model_validate(value)

    def to_json(self) -> str:
        return self.model_dump_json()

    @classmethod
    def from_json(cls, value: str | bytes | bytearray) -> StrictModel:
        return cls.model_validate_json(value)


class BudgetSpec(StrictModel):
    """Serializable limits used for deterministic parent/child allocation."""

    call_limit: int | None = Field(default=None, ge=0)
    token_limit: int | None = Field(default=None, ge=0)
    micro_usd_limit: int | None = Field(default=None, ge=0)
    wall_seconds_limit: float | None = Field(default=None, ge=0)


class WorkerSpec(StrictModel):
    id: NonEmpty
    provider_name: NonEmpty
    role: NonEmpty
    instructions: NonEmpty

    @field_validator("id")
    @classmethod
    def valid_id(cls, value: str) -> str:
        if re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,63}", value) is None:
            raise ValueError("worker id must be a safe identifier")
        return value


class ResearchConfig(StrictModel):
    max_workers: int = Field(default=4, ge=1, le=8)
    worker_budget: BudgetSpec = Field(default_factory=BudgetSpec)
    verifier_name: NonEmpty
    synthesizer_name: NonEmpty
    output_language: NonEmpty = "English"

    @field_validator("worker_budget", mode="before")
    @classmethod
    def journal_budget(cls, value: object) -> object:
        if isinstance(value, Budget):
            return {
                "call_limit": value.call_limit,
                "token_limit": value.token_limit,
                "micro_usd_limit": value.micro_usd_limit,
                "wall_seconds_limit": value.wall_seconds_limit,
            }
        return value


class Evidence(StrictModel):
    source_id: NonEmpty
    url: str | None = None
    title: str | None = None
    quote: NonEmpty
    content_hash: Sha256

    @field_validator("url", "title")
    @classmethod
    def optional_nonempty(cls, value: str | None) -> str | None:
        if value is not None and not value.strip():
            raise ValueError("optional evidence strings must not be empty")
        return value


class Claim(StrictModel):
    id: NonEmpty
    statement: NonEmpty
    evidence_ids: tuple[NonEmpty, ...]
    supporters: tuple[NonEmpty, ...]
    opponents: tuple[NonEmpty, ...]
    status: ClaimStatus
    confidence: float = Field(ge=0.0, le=1.0)

    @model_validator(mode="after")
    def coherent_status(self) -> Claim:
        if len(set(self.evidence_ids)) != len(self.evidence_ids):
            raise ValueError("claim evidence_ids must be unique")
        if len(set(self.supporters)) != len(self.supporters):
            raise ValueError("claim supporters must be unique")
        if len(set(self.opponents)) != len(self.opponents):
            raise ValueError("claim opponents must be unique")
        if set(self.supporters) & set(self.opponents):
            raise ValueError("a worker cannot both support and oppose a claim")
        if self.status == "consensus" and (not self.supporters or self.opponents):
            raise ValueError("consensus claims require supporters and no opponents")
        if self.status == "consensus" and not self.evidence_ids:
            raise ValueError("consensus claims require evidence")
        if self.status == "disputed" and (not self.supporters or not self.opponents):
            raise ValueError("disputed claims require supporters and opponents")
        if self.status == "unsupported" and self.supporters:
            raise ValueError("unsupported claims cannot have supporters")
        return self


class WorkerResult(StrictModel):
    worker_id: NonEmpty
    run_id: NonEmpty
    output: NonEmpty
    requested_model: NonEmpty
    actual_models: tuple[NonEmpty, ...]
    prompt_tokens: int = Field(default=0, ge=0)
    completion_tokens: int = Field(default=0, ge=0)
    micro_usd: int = Field(default=0, ge=0)
    artifact_hash: Sha256

    @property
    def total_tokens(self) -> int:
        return self.prompt_tokens + self.completion_tokens


class CostSummary(StrictModel):
    requested_models: dict[str, NonEmpty]
    actual_models: dict[str, tuple[NonEmpty, ...]]
    input_tokens: int = Field(ge=0)
    output_tokens: int = Field(ge=0)
    total_tokens: int = Field(ge=0)
    micro_usd: int = Field(ge=0)
    calls: int = Field(ge=0)
    allocated_budget: BudgetSpec


class ResearchResult(StrictModel):
    run_id: NonEmpty
    question: NonEmpty
    report: NonEmpty
    claims: tuple[Claim, ...]
    evidence: tuple[Evidence, ...]
    disagreements: NonEmpty
    workers: tuple[WorkerResult, ...]
    cost: CostSummary
    artifacts: dict[str, Sha256]


class VerificationResult(StrictModel):
    claims: tuple[Claim, ...]
    evidence: tuple[Evidence, ...]


def validate_evidence_integrity(
    claims: tuple[Claim, ...],
    evidence: tuple[Evidence, ...],
    worker_ids: set[str],
) -> None:
    evidence_ids = [item.source_id for item in evidence]
    if len(set(evidence_ids)) != len(evidence_ids):
        raise ValueError("evidence source_id values must be unique")
    claim_ids = [item.id for item in claims]
    if len(set(claim_ids)) != len(claim_ids):
        raise ValueError("claim ids must be unique")
    known_evidence = set(evidence_ids)
    for item in evidence:
        expected_hash = hashlib.sha256(item.quote.encode("utf-8")).hexdigest()
        if item.content_hash != expected_hash:
            raise ValueError(
                f"evidence {item.source_id!r} content_hash does not match its quote"
            )
    for claim in claims:
        missing = set(claim.evidence_ids) - known_evidence
        if missing:
            raise ValueError(f"claim {claim.id!r} references missing evidence: {sorted(missing)}")
        unknown_workers = (set(claim.supporters) | set(claim.opponents)) - worker_ids
        if unknown_workers:
            raise ValueError(
                f"claim {claim.id!r} references unknown workers: {sorted(unknown_workers)}"
            )


__all__ = [
    "BudgetSpec",
    "Claim",
    "ClaimStatus",
    "CostSummary",
    "Evidence",
    "ResearchConfig",
    "ResearchResult",
    "VerificationResult",
    "WorkerResult",
    "WorkerSpec",
    "validate_evidence_integrity",
]
