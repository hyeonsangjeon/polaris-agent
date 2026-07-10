"""Immutable journal domain records."""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
from typing import Any


class RunStatus(StrEnum):
    CREATED = "created"
    RUNNING = "running"
    PAUSED = "paused"
    COMPLETED = "completed"
    FAILED = "failed"
    CANCELLED = "cancelled"

    created = CREATED
    running = RUNNING
    paused = PAUSED
    completed = COMPLETED
    failed = FAILED
    cancelled = CANCELLED


class StepStatus(StrEnum):
    CREATED = "created"
    READY = "ready"
    LEASED = "leased"
    EXECUTING = "executing"
    COMMITTED = "committed"
    UNCERTAIN = "uncertain"
    FAILED = "failed"
    CANCELLED = "cancelled"

    created = CREATED
    ready = READY
    leased = LEASED
    executing = EXECUTING
    committed = COMMITTED
    uncertain = UNCERTAIN
    failed = FAILED
    cancelled = CANCELLED


class SafetyClass(StrEnum):
    READ_ONLY = "read_only"
    IDEMPOTENT = "idempotent"
    RECONCILABLE = "reconcilable"
    OPAQUE_SIDE_EFFECT = "opaque_side_effect"

    read_only = READ_ONLY
    idempotent = IDEMPOTENT
    reconcilable = RECONCILABLE
    opaque_side_effect = OPAQUE_SIDE_EFFECT


@dataclass(frozen=True, slots=True)
class Budget:
    call_limit: int | None = None
    token_limit: int | None = None
    micro_usd_limit: int | None = None
    wall_seconds_limit: float | None = None
    reserved_calls: int = 0
    reserved_tokens: int = 0
    reserved_micro_usd: int = 0
    reserved_wall_seconds: float = 0.0
    used_calls: int = 0
    used_tokens: int = 0
    used_micro_usd: int = 0
    used_wall_seconds: float = 0.0

    @property
    def max_calls(self) -> int | None:
        return self.call_limit

    @property
    def max_tokens(self) -> int | None:
        return self.token_limit

    @property
    def max_micro_usd(self) -> int | None:
        return self.micro_usd_limit

    @property
    def max_wall_seconds(self) -> float | None:
        return self.wall_seconds_limit


@dataclass(frozen=True, slots=True)
class RunRecord:
    id: str
    mode: str
    request: Any
    config: Any
    status: RunStatus
    budget: Budget
    parent_run_id: str | None
    created_at: str
    updated_at: str


@dataclass(frozen=True, slots=True)
class StepRecord:
    id: str
    run_id: str
    key: str
    kind: str
    name: str
    input: Any
    input_hash: str
    safety: SafetyClass
    sequence: int | None
    status: StepStatus
    output: Any
    error: Any
    uncertainty_reason: str | None
    lease_owner: str | None
    lease_expires_at: str | None
    attempt_count: int
    created_at: str
    updated_at: str


@dataclass(frozen=True, slots=True)
class EventRecord:
    id: int
    run_id: str
    step_id: str | None
    type: str
    payload: Any
    created_at: str


@dataclass(frozen=True, slots=True)
class ReceiptRecord:
    id: str
    run_id: str
    step_id: str | None
    idempotency_key: str
    payload: Any
    created_at: str


@dataclass(frozen=True, slots=True)
class ApprovalRecord:
    id: str
    run_id: str
    step_id: str | None
    kind: str
    request: Any
    status: str
    decision: str | None
    decided_by: str | None
    decision_reason: str | None
    created_at: str
    decided_at: str | None


@dataclass(frozen=True, slots=True)
class ArtifactRecord:
    id: str
    run_id: str
    step_id: str | None
    name: str
    media_type: str | None
    uri: str
    sha256: str | None
    size_bytes: int | None
    metadata: Any
    created_at: str


@dataclass(frozen=True, slots=True)
class ProviderCallRecord:
    id: str
    run_id: str
    step_id: str | None
    provider: str
    model: str | None
    request: Any
    response: Any
    status: str
    input_tokens: int
    output_tokens: int
    micro_usd: int
    started_at: str
    completed_at: str | None
