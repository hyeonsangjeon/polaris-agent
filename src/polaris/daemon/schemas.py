"""Public daemon API schemas."""

from __future__ import annotations

from typing import Annotated, Any, Literal

from pydantic import BaseModel, ConfigDict, Field, StringConstraints

NonEmpty = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1)]


class StrictSchema(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)


class BudgetSchema(StrictSchema):
    call_limit: int | None = Field(default=None, ge=0)
    token_limit: int | None = Field(default=None, ge=0)
    micro_usd_limit: int | None = Field(default=None, ge=0)
    wall_seconds_limit: float | None = Field(default=None, ge=0)


class SingleRunRequest(StrictSchema):
    prompt: NonEmpty
    provider: NonEmpty | None = None
    budget: BudgetSchema = Field(default_factory=BudgetSchema)
    schedule: bool = True
    profile_id: NonEmpty | None = None
    subject_key: NonEmpty | None = None


class WorkerRequest(StrictSchema):
    id: NonEmpty
    provider: NonEmpty
    role: NonEmpty
    instructions: NonEmpty = "Research the question and cite evidence."


class FanoutRunRequest(StrictSchema):
    question: NonEmpty
    workers: list[WorkerRequest] = Field(min_length=1, max_length=8)
    verifier: NonEmpty
    synthesizer: NonEmpty
    budget: BudgetSchema
    max_workers: int | None = Field(default=None, ge=1, le=8)
    schedule: bool = True


class FoundryRouterRunRequest(StrictSchema):
    question: NonEmpty
    provider: NonEmpty
    budget: BudgetSchema
    schedule: bool = True


class ApprovalDecisionRequest(StrictSchema):
    decision: Literal["approved", "rejected"]
    decided_by: NonEmpty = "local-user"
    reason: str | None = None


class RunResponse(StrictSchema):
    id: str
    mode: str
    request: Any
    config: Any
    status: str
    budget: dict[str, Any]
    parent_run_id: str | None
    created_at: str
    updated_at: str


class ErrorResponse(StrictSchema):
    error: str
    detail: str


class MemoryScopeSchema(StrictSchema):
    profile_id: NonEmpty = "default"
    subject_key: NonEmpty = "local"


class MemoryAddRequest(MemoryScopeSchema):
    content: NonEmpty
    kind: Literal["user", "agent", "fact", "preference"] = "fact"
    trust_level: Literal["user_asserted", "model_inferred", "verified"] = "user_asserted"
    provenance_run_id: NonEmpty | None = None
    provenance_session_id: NonEmpty | None = None
    provenance_message_id: NonEmpty | None = None
    idempotency_key: NonEmpty | None = None


class MemoryReviseRequest(MemoryScopeSchema):
    content: NonEmpty
    kind: Literal["user", "agent", "fact", "preference"] | None = None
    trust_level: Literal["user_asserted", "model_inferred", "verified"] | None = None
    provenance_run_id: NonEmpty | None = None
    provenance_session_id: NonEmpty | None = None
    provenance_message_id: NonEmpty | None = None
    expected_revision: int = Field(ge=1)
    expected_hash: NonEmpty | None = None


class MemoryRemoveRequest(MemoryScopeSchema):
    expected_revision: int = Field(ge=1)
    expected_hash: NonEmpty | None = None


class ScheduleSchema(StrictSchema):
    kind: Literal["once", "interval", "cron"]
    once_at: NonEmpty | None = None
    interval_seconds: float | None = Field(default=None, gt=0)
    cron: NonEmpty | None = None
    timezone: NonEmpty
    start_at: NonEmpty | None = None


class JobPayloadSchema(StrictSchema):
    mode: Literal["single", "fanout", "foundry-router"]
    request: dict[str, Any]
    delivery: dict[str, Any] | None = None


class JobCreateRequest(StrictSchema):
    name: str = ""
    schedule: ScheduleSchema
    payload: JobPayloadSchema
    catchup_policy: Literal["skip", "fire_once", "bounded"] = "fire_once"
    max_catchup: int = Field(default=1, ge=1, le=10)
    grace_seconds: float = Field(default=0, ge=0)


class SchedulePreviewRequest(StrictSchema):
    schedule: ScheduleSchema
    after: NonEmpty
    count: int = Field(default=5, ge=1, le=100)


class OutboxResolutionRequest(StrictSchema):
    note: NonEmpty
