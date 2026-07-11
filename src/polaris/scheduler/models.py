"""Domain models for durable scheduled jobs."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import UTC, datetime
from enum import StrEnum
from math import isfinite
from types import MappingProxyType
from typing import Any
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError


class ScheduleKind(StrEnum):
    ONCE = "once"
    INTERVAL = "interval"
    CRON = "cron"

    once = ONCE
    interval = INTERVAL
    cron = CRON


class CatchupPolicy(StrEnum):
    SKIP = "skip"
    FIRE_ONCE = "fire_once"
    BOUNDED = "bounded"

    skip = SKIP
    fire_once = FIRE_ONCE
    bounded = BOUNDED


class JobState(StrEnum):
    SCHEDULED = "scheduled"
    PAUSED = "paused"
    COMPLETED = "completed"
    CANCELLED = "cancelled"

    scheduled = SCHEDULED
    paused = PAUSED
    completed = COMPLETED
    cancelled = CANCELLED


class JobRunStatus(StrEnum):
    CLAIMED = "claimed"
    RUNNING = "running"
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    INTERRUPTED = "interrupted"
    CANCELLED = "cancelled"

    claimed = CLAIMED
    running = RUNNING
    succeeded = SUCCEEDED
    failed = FAILED
    interrupted = INTERRUPTED
    cancelled = CANCELLED


class DeliveryStatus(StrEnum):
    NOT_REQUESTED = "not_requested"
    PENDING = "pending"
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    SUPPRESSED = "suppressed"

    not_requested = NOT_REQUESTED
    pending = PENDING
    succeeded = SUCCEEDED
    failed = FAILED
    suppressed = SUPPRESSED


def utc_now() -> datetime:
    return datetime.now(UTC)


def ensure_aware(value: datetime, name: str = "timestamp") -> datetime:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError(f"{name} must include a timezone")
    return value.astimezone(UTC)


def parse_timestamp(value: str | datetime, timezone: str = "UTC") -> datetime:
    if isinstance(value, datetime):
        if value.tzinfo is not None and value.utcoffset() is not None:
            return value.astimezone(UTC)
        local = value
    else:
        text = value.strip()
        if not text:
            raise ValueError("timestamp must not be empty")
        if text.endswith("Z"):
            text = f"{text[:-1]}+00:00"
        try:
            parsed = datetime.fromisoformat(text)
        except ValueError as exc:
            raise ValueError(f"invalid ISO timestamp: {value!r}") from exc
        if parsed.tzinfo is not None and parsed.utcoffset() is not None:
            return parsed.astimezone(UTC)
        local = parsed
    zone = get_timezone(timezone)
    candidates = valid_local_instants(local, zone)
    if not candidates:
        raise ValueError(f"timestamp {local.isoformat()} does not exist in {timezone}")
    # A local once time that occurs twice is pinned to the first occurrence.
    return candidates[0]


def get_timezone(name: str) -> ZoneInfo:
    try:
        return ZoneInfo(name)
    except (ZoneInfoNotFoundError, ValueError) as exc:
        raise ValueError(f"unknown IANA timezone: {name!r}") from exc


def valid_local_instants(local: datetime, zone: ZoneInfo) -> tuple[datetime, ...]:
    if local.tzinfo is not None:
        local = local.replace(tzinfo=None)
    found: list[datetime] = []
    for fold in (0, 1):
        candidate = local.replace(tzinfo=zone, fold=fold).astimezone(UTC)
        round_trip = candidate.astimezone(zone)
        if (
            round_trip.replace(tzinfo=None) == local
            and round_trip.fold == fold
            and candidate not in found
        ):
            found.append(candidate)
    return tuple(sorted(found))


@dataclass(frozen=True, slots=True)
class ScheduleSpec:
    kind: ScheduleKind | str
    once_at: str | datetime | None = None
    interval_seconds: float | None = None
    cron: str | None = None
    timezone: str = "UTC"
    start_at: str | datetime | None = None

    def __post_init__(self) -> None:
        kind = ScheduleKind(self.kind)
        object.__setattr__(self, "kind", kind)
        get_timezone(self.timezone)
        if kind is ScheduleKind.ONCE:
            if self.once_at is None:
                raise ValueError("once schedule requires once_at")
            parse_timestamp(self.once_at, self.timezone)
            if self.interval_seconds is not None or self.cron is not None:
                raise ValueError("once schedule cannot include interval or cron")
        elif kind is ScheduleKind.INTERVAL:
            if (
                self.interval_seconds is None
                or not isfinite(self.interval_seconds)
                or self.interval_seconds <= 0
            ):
                raise ValueError("interval_seconds must be positive")
            if self.once_at is not None or self.cron is not None:
                raise ValueError("interval schedule cannot include once_at or cron")
            if self.start_at is not None:
                parse_timestamp(self.start_at, self.timezone)
        else:
            if self.cron is None:
                raise ValueError("cron schedule requires cron")
            if self.once_at is not None or self.interval_seconds is not None:
                raise ValueError("cron schedule cannot include once_at or interval")
            from .cron import CronExpression

            CronExpression(self.cron)

    @classmethod
    def once(cls, at: str | datetime, *, timezone: str = "UTC") -> ScheduleSpec:
        return cls(ScheduleKind.ONCE, once_at=at, timezone=timezone)

    @classmethod
    def interval(
        cls,
        seconds: float,
        *,
        start_at: str | datetime | None = None,
        timezone: str = "UTC",
    ) -> ScheduleSpec:
        return cls(
            ScheduleKind.INTERVAL,
            interval_seconds=seconds,
            start_at=start_at,
            timezone=timezone,
        )

    @classmethod
    def cron_schedule(cls, expression: str, *, timezone: str = "UTC") -> ScheduleSpec:
        return cls(ScheduleKind.CRON, cron=expression, timezone=timezone)

    @property
    def expression(self) -> str | float | datetime | None:
        if self.kind is ScheduleKind.ONCE:
            return self.once_at
        if self.kind is ScheduleKind.INTERVAL:
            return self.interval_seconds
        return self.cron


def _freeze_mapping(value: Mapping[str, Any] | None) -> Mapping[str, Any] | None:
    if value is None:
        return None
    return MappingProxyType(dict(value))


@dataclass(frozen=True, slots=True)
class JobPayload:
    mode: str
    request: Mapping[str, Any]
    delivery: Mapping[str, Any] | None = None

    def __post_init__(self) -> None:
        mode = self.mode.replace("_", "-")
        if mode == "fan-out":
            mode = "fanout"
        if mode not in {"single", "fanout", "foundry-router"}:
            raise ValueError("payload mode must be single, fanout, or foundry-router")
        if not isinstance(self.request, Mapping):
            raise ValueError("payload request must be a mapping")
        object.__setattr__(self, "mode", mode)
        object.__setattr__(self, "request", _freeze_mapping(self.request))
        object.__setattr__(self, "delivery", _freeze_mapping(self.delivery))

    @property
    def kind(self) -> str:
        return self.mode

    @property
    def delivery_target(self) -> Mapping[str, Any] | None:
        return self.delivery

    @classmethod
    def single(
        cls,
        request: Mapping[str, Any],
        delivery: Mapping[str, Any] | None = None,
    ) -> JobPayload:
        return cls("single", request, delivery)

    @classmethod
    def fanout(
        cls,
        request: Mapping[str, Any],
        delivery: Mapping[str, Any] | None = None,
    ) -> JobPayload:
        return cls("fanout", request, delivery)

    @classmethod
    def foundry_router(
        cls,
        request: Mapping[str, Any],
        delivery: Mapping[str, Any] | None = None,
    ) -> JobPayload:
        return cls("foundry-router", request, delivery)


@dataclass(frozen=True, slots=True)
class Job:
    id: str
    name: str
    schedule: ScheduleSpec
    payload: JobPayload
    catchup_policy: CatchupPolicy
    max_catchup: int
    state: JobState
    next_run_at: datetime | None
    grace_seconds: float
    created_at: datetime
    updated_at: datetime
    version: int = 1


@dataclass(frozen=True, slots=True)
class JobRun:
    id: str
    job_id: str
    scheduled_for: datetime
    status: JobRunStatus
    attempt: int
    owner: str | None
    lease_expires_at: datetime | None
    polaris_run_id: str | None
    cancel_requested: bool
    execution_error: str | None
    delivery_status: DeliveryStatus
    delivery_error: str | None
    claimed_at: datetime
    started_at: datetime | None
    completed_at: datetime | None
    updated_at: datetime
    payload: JobPayload | None = field(default=None, compare=False)
