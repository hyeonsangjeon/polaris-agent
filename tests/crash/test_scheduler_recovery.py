from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

from polaris.scheduler import (
    DeliveryStatus,
    JobPayload,
    JobRunStatus,
    SchedulerEngine,
    SchedulerStore,
    ScheduleSpec,
)


def dt(value: str) -> datetime:
    return datetime.fromisoformat(value).astimezone(UTC)


def test_crash_after_claim_becomes_interrupted_without_automatic_rerun(tmp_path: Path) -> None:
    path = tmp_path / "journal.sqlite3"
    first = SchedulerStore(path)
    first.create_job(
        ScheduleSpec.once("2026-01-01T00:00:00+00:00"),
        JobPayload("single", {"prompt": "side effect"}),
        job_id="job",
        now=dt("2025-12-31T00:00:00+00:00"),
    )
    claimed = first.claim_due_runs(dt("2026-01-01T00:00:00+00:00"), "dead", 5)[0]
    first.mark_running(claimed.id, "dead", now=dt("2026-01-01T00:00:01+00:00"))
    first.close()

    with SchedulerStore(path) as recovered:
        stale = recovered.recover_stale_runs(dt("2026-01-01T00:00:05+00:00"))
        assert stale[0].status is JobRunStatus.INTERRUPTED
        assert "ambiguous" in (stale[0].execution_error or "")
        assert recovered.claim_due_runs(
            dt("2026-01-02T00:00:00+00:00"),
            "new",
            30,
        ) == ()
        assert len(recovered.list_runs(job_id="job")) == 1


async def test_crash_after_execution_commit_recovers_delivery_once(tmp_path: Path) -> None:
    path = tmp_path / "journal.sqlite3"
    now = dt("2026-01-01T00:00:00+00:00")
    first = SchedulerStore(path)
    first.create_job(
        ScheduleSpec.once(now),
        JobPayload.single(
            {"prompt": "side effect"},
            {"channel": "durable"},
        ),
        job_id="job",
        now=dt("2025-12-31T00:00:00+00:00"),
    )
    claimed = first.claim_due_runs(now, "dead", 30)[0]
    first.mark_running(claimed.id, "dead", now=now)
    first.set_polaris_run_id(claimed.id, "dead", "agent-run", now=now)
    committed = first.complete(claimed.id, "dead", now=now)
    assert committed.delivery_status is DeliveryStatus.PENDING
    first.close()

    delivered: list[str] = []

    async def deliver(_target: object, run_id: str) -> None:
        delivered.append(run_id)

    with SchedulerStore(path) as recovered:
        engine = SchedulerEngine(
            recovered,
            lambda _payload: "must-not-run",
            deliver,
            owner="new",
            clock=lambda: now,
        )
        await engine.tick()
        await engine.tick()
        await engine.drain()

        run = recovered.get_run(claimed.id)
        assert run.delivery_status is DeliveryStatus.SUCCEEDED
        assert delivered == ["agent-run"]
