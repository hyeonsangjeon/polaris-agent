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
