from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from polaris.scheduler import (
    CronExpression,
    JobPayload,
    SchedulerValidationError,
    ScheduleSpec,
    compute_next_run,
    preview_next_times,
)
from polaris.scheduler.models import get_timezone, parse_timestamp


def dt(value: str) -> datetime:
    return datetime.fromisoformat(value).astimezone(UTC)


def test_cron_ranges_steps_and_dom_dow_or_semantics() -> None:
    cron = CronExpression("*/15 9-10 13 * 1")
    assert cron.matches(datetime(2026, 7, 13, 9, 30))  # Monday and the 13th
    assert cron.matches(datetime(2026, 8, 13, 10, 45))  # Thursday, but the 13th
    assert cron.matches(datetime(2026, 8, 17, 9, 0))  # Monday, but not the 13th
    assert not cron.matches(datetime(2026, 8, 18, 9, 0))
    assert CronExpression("0 0 * * 7").matches(datetime(2026, 7, 12, 0, 0))


@pytest.mark.parametrize(
    "expression",
    ["", "* * * *", "60 * * * *", "* 24 * * *", "* * 0 * *", "*/0 * * * *", "x * * * *"],
)
def test_invalid_cron_rejected(expression: str) -> None:
    with pytest.raises(ValueError):
        CronExpression(expression)


def test_cron_dst_nonexistent_is_skipped_and_ambiguous_runs_twice() -> None:
    zone = get_timezone("America/New_York")
    spring = CronExpression("30 2 * * *")
    assert spring.next_after(dt("2025-03-08T08:00:00+00:00"), zone) == dt(
        "2025-03-10T06:30:00+00:00"
    )

    fall = CronExpression("30 1 * * *")
    first = fall.next_after(dt("2025-11-02T04:00:00+00:00"), zone)
    second = fall.next_after(first, zone)
    assert first == dt("2025-11-02T05:30:00+00:00")
    assert second == dt("2025-11-02T06:30:00+00:00")


def test_once_local_dst_rules_are_explicit() -> None:
    with pytest.raises(ValueError, match="does not exist"):
        ScheduleSpec.once("2025-03-09T02:30:00", timezone="America/New_York")
    ambiguous = parse_timestamp("2025-11-02T01:30:00", "America/New_York")
    assert ambiguous == dt("2025-11-02T05:30:00+00:00")


def test_once_interval_and_preview() -> None:
    once = ScheduleSpec.once("2026-07-11T10:00:00+09:00")
    assert compute_next_run(once, dt("2026-07-11T00:59:00+00:00")) == dt(
        "2026-07-11T01:00:00+00:00"
    )
    assert compute_next_run(once, dt("2026-07-11T01:00:00+00:00")) is None

    interval = ScheduleSpec.interval(30, start_at=dt("2026-01-01T00:00:00+00:00"))
    assert preview_next_times(
        interval,
        dt("2026-01-01T00:00:05+00:00"),
        3,
    ) == (
        dt("2026-01-01T00:00:30+00:00"),
        dt("2026-01-01T00:01:00+00:00"),
        dt("2026-01-01T00:01:30+00:00"),
    )


def test_invalid_schedule_timezone_and_payload() -> None:
    with pytest.raises(ValueError):
        ScheduleSpec.interval(0)
    with pytest.raises(ValueError):
        ScheduleSpec.interval(float("nan"))
    with pytest.raises(ValueError):
        ScheduleSpec.cron_schedule("* * * * *", timezone="Mars/Olympus")
    with pytest.raises(ValueError):
        JobPayload("unknown", {})
    with pytest.raises(SchedulerValidationError):
        preview_next_times(ScheduleSpec.interval(1), datetime.now(UTC), 1001)


def test_interval_arithmetic_does_not_drift() -> None:
    anchor = dt("2026-01-01T00:00:00+00:00")
    schedule = ScheduleSpec.interval(0.25, start_at=anchor)
    assert compute_next_run(schedule, anchor + timedelta(seconds=10.1)) == anchor + timedelta(
        seconds=10.25
    )


def test_cron_iteration_and_additional_parser_validation() -> None:
    cron = CronExpression("5/20 * * * *")
    assert cron.next_times(dt("2026-01-01T00:00:00+00:00"), get_timezone("UTC"), 3) == (
        dt("2026-01-01T00:05:00+00:00"),
        dt("2026-01-01T00:25:00+00:00"),
        dt("2026-01-01T00:45:00+00:00"),
    )
    with pytest.raises(ValueError):
        cron.next_times(datetime.now(UTC), get_timezone("UTC"), -1)
    with pytest.raises(ValueError):
        cron.next_after(datetime(2026, 1, 1), get_timezone("UTC"))
    for expression in ("x-y * * * *", "10-5 * * * *", "*/x * * * *", "x/2 * * * *"):
        with pytest.raises(ValueError):
            CronExpression(expression)


def test_payload_helpers_and_schedule_expression() -> None:
    delivery = {"channel": "console"}
    single = JobPayload.single({"prompt": "one"}, delivery)
    assert single.kind == "single"
    assert single.delivery_target == delivery
    assert JobPayload.fanout({"prompts": []}).mode == "fanout"
    assert JobPayload("fan-out", {}).mode == "fanout"
    assert JobPayload.foundry_router({"prompt": "route"}).mode == "foundry-router"
    with pytest.raises(TypeError):
        single.request["changed"] = True  # type: ignore[index]
    assert ScheduleSpec.once(dt("2026-01-01T00:00:00+00:00")).expression == dt(
        "2026-01-01T00:00:00+00:00"
    )
    assert ScheduleSpec.interval(5).expression == 5
    assert ScheduleSpec.cron_schedule("* * * * *").expression == "* * * * *"
