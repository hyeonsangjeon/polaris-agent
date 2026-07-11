"""Authenticated FastAPI surface for the local service."""

from __future__ import annotations

import asyncio
import hmac
import json
from collections.abc import AsyncIterator, Mapping
from contextlib import asynccontextmanager
from dataclasses import fields, is_dataclass
from enum import Enum
from typing import Any, cast

from fastapi import Depends, FastAPI, Header, HTTPException, Request, status
from fastapi.encoders import jsonable_encoder
from fastapi.responses import JSONResponse, StreamingResponse

from polaris.channels import ChannelTransitionError
from polaris.ensemble import WorkerSpec
from polaris.journal import (
    InvalidTransitionError,
    JournalConflictError,
    JournalNotFoundError,
    JournalValidationError,
)
from polaris.memory import MemoryConflictError, MemoryNotFoundError, MemoryScope
from polaris.providers import ProviderConfigurationError, ProviderError
from polaris.scheduler import (
    JobPayload,
    SchedulerConflictError,
    SchedulerNotFoundError,
    ScheduleSpec,
)

from ..service import AgentService
from .schemas import (
    ApprovalDecisionRequest,
    FanoutRunRequest,
    FoundryRouterRunRequest,
    JobCreateRequest,
    JobPayloadSchema,
    MemoryAddRequest,
    MemoryRemoveRequest,
    MemoryReviseRequest,
    OutboxResolutionRequest,
    RunResponse,
    SchedulePreviewRequest,
    ScheduleSchema,
    SingleRunRequest,
)


def _plain(value: object) -> Any:
    if is_dataclass(value):
        instance = cast(Any, value)
        return {field.name: _plain(getattr(instance, field.name)) for field in fields(instance)}
    if isinstance(value, Enum):
        return value.value
    if isinstance(value, Mapping):
        return {str(key): _plain(item) for key, item in value.items()}
    if isinstance(value, (tuple, list)):
        return [_plain(item) for item in value]
    return value


def _run_response(run: object) -> RunResponse:
    return RunResponse.model_validate(_plain(run))


def _schedule(value: ScheduleSchema) -> ScheduleSpec:
    if value.kind == "once":
        if value.once_at is None:
            raise ValueError("once schedule requires once_at")
        return ScheduleSpec.once(value.once_at, timezone=value.timezone)
    if value.kind == "interval":
        if value.interval_seconds is None:
            raise ValueError("interval schedule requires interval_seconds")
        return ScheduleSpec.interval(
            value.interval_seconds,
            start_at=value.start_at,
            timezone=value.timezone,
        )
    if value.cron is None:
        raise ValueError("cron schedule requires cron")
    return ScheduleSpec.cron_schedule(value.cron, timezone=value.timezone)


def _payload(value: JobPayloadSchema) -> JobPayload:
    return JobPayload(value.mode, value.request, value.delivery)


def create_app(service: AgentService, api_token: str) -> FastAPI:
    if not isinstance(api_token, str) or not api_token:
        raise ValueError("api_token must be a non-empty string")
    service.add_runtime_secrets((api_token,))

    @asynccontextmanager
    async def lifespan(_app: FastAPI) -> AsyncIterator[None]:
        await service.startup()
        try:
            yield
        finally:
            await service.close()

    app = FastAPI(
        title="Polaris local daemon",
        version="1",
        lifespan=lifespan,
        docs_url=None,
        redoc_url=None,
        openapi_url=None,
    )
    app.state.service = service

    async def authenticate(authorization: str | None = Header(default=None)) -> None:
        scheme, _, supplied = (authorization or "").partition(" ")
        if scheme.lower() != "bearer" or not supplied or not hmac.compare_digest(
            supplied.encode(), api_token.encode()
        ):
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED,
                detail="invalid bearer token",
                headers={"WWW-Authenticate": "Bearer"},
            )

    auth = [Depends(authenticate)]

    @app.exception_handler(JournalNotFoundError)
    @app.exception_handler(MemoryNotFoundError)
    @app.exception_handler(SchedulerNotFoundError)
    async def not_found(_request: Request, exc: Exception) -> JSONResponse:
        return _error(404, "not_found", str(exc))

    @app.exception_handler(InvalidTransitionError)
    @app.exception_handler(JournalConflictError)
    @app.exception_handler(MemoryConflictError)
    @app.exception_handler(SchedulerConflictError)
    @app.exception_handler(ChannelTransitionError)
    async def conflict(_request: Request, exc: Exception) -> JSONResponse:
        return _error(409, "conflict", str(exc))

    @app.exception_handler(JournalValidationError)
    @app.exception_handler(ValueError)
    async def invalid(_request: Request, exc: Exception) -> JSONResponse:
        return _error(400, "invalid_request", str(exc))

    @app.exception_handler(ProviderConfigurationError)
    async def provider_config(
        _request: Request, exc: ProviderConfigurationError
    ) -> JSONResponse:
        return _error(409, "provider_configuration", str(exc))

    @app.exception_handler(ProviderError)
    async def provider_failure(_request: Request, exc: ProviderError) -> JSONResponse:
        return _error(503, "provider_failure", str(exc))

    @app.get("/health")
    async def health() -> dict[str, str]:
        return {"status": "ok"}

    @app.post("/v1/runs/single", dependencies=auth, response_model=RunResponse, status_code=202)
    async def submit_single(body: SingleRunRequest) -> RunResponse:
        run = await service.submit_single(
            body.prompt,
            provider=body.provider,
            budget=body.budget.model_dump(exclude_none=True),
            schedule=body.schedule,
            profile_id=body.profile_id,
            subject_key=body.subject_key,
        )
        return _run_response(run)

    @app.post("/v1/runs/fanout", dependencies=auth, response_model=RunResponse, status_code=202)
    async def submit_fanout(body: FanoutRunRequest) -> RunResponse:
        workers = tuple(
            WorkerSpec(
                id=item.id,
                provider_name=item.provider,
                role=item.role,
                instructions=item.instructions,
            )
            for item in body.workers
        )
        run = await service.submit_fanout(
            body.question,
            workers,
            verifier=body.verifier,
            synthesizer=body.synthesizer,
            budget=body.budget.model_dump(exclude_none=True),
            max_workers=body.max_workers,
            schedule=body.schedule,
        )
        return _run_response(run)

    @app.post(
        "/v1/runs/foundry-router",
        dependencies=auth,
        response_model=RunResponse,
        status_code=202,
    )
    async def submit_foundry_router(body: FoundryRouterRunRequest) -> RunResponse:
        run = await service.submit_foundry_router(
            body.question,
            provider=body.provider,
            budget=body.budget.model_dump(exclude_none=True),
            schedule=body.schedule,
        )
        return _run_response(run)

    @app.get("/v1/runs", dependencies=auth, response_model=list[RunResponse])
    async def list_runs(run_status: str | None = None) -> list[RunResponse]:
        return [_run_response(run) for run in service.list(run_status)]

    @app.get("/v1/runs/{run_id}", dependencies=auth, response_model=RunResponse)
    async def get_run(run_id: str) -> RunResponse:
        return _run_response(service.get(run_id))

    @app.get("/v1/runs/{run_id}/timeline", dependencies=auth)
    async def timeline(
        run_id: str, after_id: int | None = None, limit: int | None = None
    ) -> Any:
        return jsonable_encoder(_plain(service.timeline(run_id, after_id=after_id, limit=limit)))

    @app.get("/v1/runs/{run_id}/artifacts", dependencies=auth)
    async def artifacts(run_id: str) -> Any:
        return jsonable_encoder(_plain(service.artifacts(run_id)))

    @app.get("/v1/runs/{run_id}/replay", dependencies=auth)
    async def replay(run_id: str) -> Any:
        return jsonable_encoder(_plain(service.replay(run_id)))

    @app.post("/v1/runs/{run_id}/resume", dependencies=auth, response_model=RunResponse)
    async def resume(run_id: str) -> RunResponse:
        return _run_response(await service.resume(run_id))

    @app.post("/v1/runs/{run_id}/cancel", dependencies=auth, response_model=RunResponse)
    async def cancel(run_id: str) -> RunResponse:
        return _run_response(await service.cancel(run_id))

    @app.get("/v1/runs/{run_id}/approvals", dependencies=auth)
    async def approvals(run_id: str, pending: bool = False) -> Any:
        return jsonable_encoder(_plain(service.approvals(run_id, pending_only=pending)))

    @app.post("/v1/approvals/{approval_id}", dependencies=auth)
    @app.post("/v1/approvals/{approval_id}/decision", dependencies=auth)
    async def decide(approval_id: str, body: ApprovalDecisionRequest) -> Any:
        record = await service.decide_approval(
            approval_id,
            body.decision == "approved",
            decided_by=body.decided_by,
            reason=body.reason,
        )
        return jsonable_encoder(_plain(record))

    @app.get("/v1/providers/doctor", dependencies=auth)
    async def providers_doctor() -> dict[str, object]:
        return await service.provider_doctor()

    @app.get("/v1/models", dependencies=auth)
    async def models() -> dict[str, object]:
        return await service.models()

    @app.get("/v1/tools", dependencies=auth)
    async def tools() -> dict[str, list[str]]:
        return {"tools": list(service.tool_names())}

    @app.get("/v1/memory", dependencies=auth)
    async def memory_list(
        profile_id: str = "default",
        subject_key: str = "local",
        include_tombstones: bool = False,
        limit: int | None = None,
    ) -> Any:
        return jsonable_encoder(
            _plain(
                service.memory_list(
                    profile_id=profile_id,
                    subject_key=subject_key,
                    include_tombstones=include_tombstones,
                    limit=limit,
                )
            )
        )

    @app.get("/v1/memory/search", dependencies=auth)
    async def memory_search(
        query: str,
        profile_id: str = "default",
        subject_key: str = "local",
        limit: int = 10,
    ) -> Any:
        return jsonable_encoder(
            _plain(
                service.memory_search(
                    None,
                    query,
                    profile_id=profile_id,
                    subject_key=subject_key,
                    limit=limit,
                )
            )
        )

    @app.post("/v1/memory", dependencies=auth, status_code=201)
    async def memory_add(body: MemoryAddRequest) -> Any:
        return jsonable_encoder(
            _plain(
                service.memory_add(
                    MemoryScope(body.profile_id, body.subject_key),
                    body.content,
                    kind=body.kind,
                    trust_level=body.trust_level,
                    provenance_run_id=body.provenance_run_id,
                    provenance_session_id=body.provenance_session_id,
                    provenance_message_id=body.provenance_message_id,
                    idempotency_key=body.idempotency_key,
                )
            )
        )

    @app.put("/v1/memory/{entry_id}", dependencies=auth)
    async def memory_revise(entry_id: str, body: MemoryReviseRequest) -> Any:
        provenance = {
            field: getattr(body, field)
            for field in (
                "provenance_run_id",
                "provenance_session_id",
                "provenance_message_id",
            )
            if field in body.model_fields_set
        }
        return jsonable_encoder(
            _plain(
                service.memory_revise(
                    MemoryScope(body.profile_id, body.subject_key),
                    entry_id,
                    body.content,
                    expected_revision=body.expected_revision,
                    expected_hash=body.expected_hash,
                    kind=body.kind,
                    trust_level=body.trust_level,
                    **provenance,
                )
            )
        )

    @app.delete("/v1/memory/{entry_id}", dependencies=auth)
    async def memory_remove(entry_id: str, body: MemoryRemoveRequest) -> Any:
        return jsonable_encoder(
            _plain(
                service.memory_remove(
                    MemoryScope(body.profile_id, body.subject_key),
                    entry_id,
                    expected_revision=body.expected_revision,
                    expected_hash=body.expected_hash,
                )
            )
        )

    @app.post("/v1/jobs/preview", dependencies=auth)
    async def preview_job(body: SchedulePreviewRequest) -> Any:
        from polaris.scheduler.models import parse_timestamp

        after = parse_timestamp(body.after, body.schedule.timezone)
        return jsonable_encoder(
            service.preview_schedule(
                _schedule(body.schedule),
                after=after,
                count=body.count,
            )
        )

    @app.post("/v1/jobs", dependencies=auth, status_code=201)
    async def create_job(body: JobCreateRequest) -> Any:
        return jsonable_encoder(
            _plain(
                service.create_job(
                    _schedule(body.schedule),
                    _payload(body.payload),
                    name=body.name,
                    catchup_policy=body.catchup_policy,
                    max_catchup=body.max_catchup,
                    grace_seconds=body.grace_seconds,
                )
            )
        )

    @app.get("/v1/jobs", dependencies=auth)
    async def list_jobs(job_state: str | None = None) -> Any:
        return jsonable_encoder(_plain(service.list_jobs(job_state)))

    @app.get("/v1/jobs/runs", dependencies=auth)
    async def all_job_runs(
        job_id: str | None = None, run_status: str | None = None
    ) -> Any:
        return jsonable_encoder(
            _plain(service.list_job_runs(job_id=job_id, status=run_status))
        )

    @app.post("/v1/jobs/runs/{run_id}/retry", dependencies=auth, status_code=202)
    async def retry_job_run(run_id: str) -> Any:
        return jsonable_encoder(_plain(service.retry_job_run(run_id)))

    @app.get("/v1/jobs/{job_id}", dependencies=auth)
    async def get_job(job_id: str) -> Any:
        return jsonable_encoder(_plain(service.get_job(job_id)))

    @app.post("/v1/jobs/{job_id}/pause", dependencies=auth)
    async def pause_job(job_id: str) -> Any:
        return jsonable_encoder(_plain(service.pause_job(job_id)))

    @app.post("/v1/jobs/{job_id}/resume", dependencies=auth)
    async def resume_job(job_id: str) -> Any:
        return jsonable_encoder(_plain(service.resume_job(job_id)))

    @app.post("/v1/jobs/{job_id}/cancel", dependencies=auth)
    @app.delete("/v1/jobs/{job_id}", dependencies=auth)
    async def cancel_job(job_id: str) -> Any:
        return jsonable_encoder(_plain(service.cancel_job(job_id)))

    @app.get("/v1/jobs/{job_id}/runs", dependencies=auth)
    async def job_runs(job_id: str, run_status: str | None = None) -> Any:
        service.get_job(job_id)
        return jsonable_encoder(
            _plain(service.list_job_runs(job_id=job_id, status=run_status))
        )

    @app.get("/v1/channels/status", dependencies=auth)
    async def channels_status() -> Any:
        return jsonable_encoder(service.channels_status())

    @app.get("/v1/channels/outbox/unknown", dependencies=auth)
    async def unknown_outbox(
        platform: str | None = None,
        limit: int = 500,
    ) -> Any:
        return jsonable_encoder(
            _plain(service.unknown_outbox(platform=platform, limit=limit))
        )

    @app.post(
        "/v1/channels/outbox/{idempotency_key}/mark-sent",
        dependencies=auth,
    )
    async def mark_outbox_sent(
        idempotency_key: str, body: OutboxResolutionRequest
    ) -> Any:
        return jsonable_encoder(
            _plain(service.mark_outbox_sent(idempotency_key, note=body.note))
        )

    @app.post(
        "/v1/channels/outbox/{idempotency_key}/retry",
        dependencies=auth,
    )
    async def retry_outbox(
        idempotency_key: str, body: OutboxResolutionRequest
    ) -> Any:
        return jsonable_encoder(
            _plain(service.retry_outbox(idempotency_key, note=body.note))
        )

    @app.get("/v1/runs/{run_id}/events", dependencies=auth)
    async def events(
        request: Request,
        run_id: str,
        last_event_id: str | None = Header(default=None, alias="Last-Event-ID"),
    ) -> StreamingResponse:
        service.get(run_id)
        try:
            cursor = int(last_event_id) if last_event_id else 0
        except ValueError as exc:
            raise HTTPException(400, "Last-Event-ID must be an integer") from exc

        async def stream() -> AsyncIterator[str]:
            nonlocal cursor
            idle = 0
            while True:
                if await request.is_disconnected():
                    return
                records = service.timeline(run_id, after_id=cursor, limit=100)
                if records:
                    idle = 0
                    for record in records:
                        cursor = record.id
                        payload = json.dumps(_plain(record), separators=(",", ":"))
                        yield f"id: {record.id}\nevent: {record.type}\ndata: {payload}\n\n"
                else:
                    idle += 1
                    if idle >= 15:
                        yield ": heartbeat\n\n"
                        idle = 0
                    run = service.get(run_id)
                    if run.status.value in {"completed", "failed", "cancelled"}:
                        return
                await asyncio.sleep(1)

        return StreamingResponse(
            stream(),
            media_type="text/event-stream",
            headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
        )

    return app


def _error(status_code: int, error: str, detail: str) -> JSONResponse:
    return JSONResponse(status_code=status_code, content={"error": error, "detail": detail})
