from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta
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


def due_job(store: SchedulerStore, *, delivery: bool = False) -> None:
    target = {"channel": "test"} if delivery else None
    store.create_job(
        ScheduleSpec.once("2026-01-01T00:00:00+00:00"),
        JobPayload("single", {"prompt": "hello"}, target),
        now=dt("2025-12-31T00:00:00+00:00"),
    )


async def test_execution_success_and_delivery_failure_are_distinct(tmp_path: Path) -> None:
    with SchedulerStore(tmp_path / "journal.sqlite3") as store:
        due_job(store, delivery=True)

        async def deliver(_target: object, _run_id: str) -> None:
            raise RuntimeError("channel unavailable")

        engine = SchedulerEngine(
            store,
            lambda payload: {"run_id": f"run-{payload.mode}"},
            deliver,
            owner="engine",
            clock=lambda: dt("2026-01-01T00:00:00+00:00"),
        )
        await engine.tick()
        await engine.drain()
        run = store.list_runs()[0]
        assert run.status is JobRunStatus.SUCCEEDED
        assert run.polaris_run_id == "run-single"
        assert run.delivery_status is DeliveryStatus.FAILED
        assert "channel unavailable" in (run.delivery_error or "")


async def test_cancel_during_execution_suppresses_delivery(tmp_path: Path) -> None:
    delivered = False
    with SchedulerStore(tmp_path / "journal.sqlite3") as store:
        due_job(store, delivery=True)

        async def submit(_payload: JobPayload) -> str:
            running = store.list_runs(status=JobRunStatus.RUNNING)[0]
            store.request_cancel(running.id, now=dt("2026-01-01T00:00:00+00:00"))
            return "polaris-run"

        async def deliver(_target: object, _run_id: str) -> None:
            nonlocal delivered
            delivered = True

        engine = SchedulerEngine(
            store,
            submit,
            deliver,
            owner="engine",
            clock=lambda: dt("2026-01-01T00:00:00+00:00"),
        )
        await engine.tick()
        await engine.drain()
        run = store.list_runs()[0]
        assert run.status is JobRunStatus.CANCELLED
        assert run.cancel_requested
        assert run.delivery_status is DeliveryStatus.SUPPRESSED
        assert not delivered


async def test_overlapping_ticks_do_not_overlap_submission(tmp_path: Path) -> None:
    entered = asyncio.Event()
    release = asyncio.Event()
    active = 0
    maximum = 0
    with SchedulerStore(tmp_path / "journal.sqlite3") as store:
        due_job(store)

        async def submit(_payload: JobPayload) -> str:
            nonlocal active, maximum
            active += 1
            maximum = max(maximum, active)
            entered.set()
            await release.wait()
            active -= 1
            return "run"

        engine = SchedulerEngine(
            store,
            submit,
            owner="engine",
            clock=lambda: dt("2026-01-01T00:00:00+00:00"),
        )
        first = await engine.tick()
        await entered.wait()
        second = await engine.tick()
        assert len(first) == 1
        assert second == ()
        release.set()
        await engine.drain()
        assert maximum == 1
        assert len(store.list_runs()) == 1


async def test_ticker_starts_stops_and_drains(tmp_path: Path) -> None:
    entered = asyncio.Event()
    release = asyncio.Event()
    sleep_started = asyncio.Event()

    async def controlled_sleep(_delay: float) -> None:
        sleep_started.set()
        await asyncio.Event().wait()

    with SchedulerStore(tmp_path / "journal.sqlite3") as store:
        due_job(store)

        async def submit(_payload: JobPayload) -> str:
            entered.set()
            await release.wait()
            return "run"

        engine = SchedulerEngine(
            store,
            submit,
            owner="engine",
            clock=lambda: dt("2026-01-01T00:00:00+00:00"),
            sleep=controlled_sleep,
        )
        await engine.start()
        assert engine.running
        await entered.wait()
        closing = asyncio.create_task(engine.close())
        await asyncio.sleep(0)
        assert not closing.done()
        release.set()
        await closing
        assert not engine.running
        assert store.list_runs()[0].status is JobRunStatus.SUCCEEDED


async def test_submit_failure_and_invalid_result_are_recorded(tmp_path: Path) -> None:
    for index, submit in enumerate((lambda _payload: None, lambda _payload: 1 / 0)):
        with SchedulerStore(tmp_path / f"journal-{index}.sqlite3") as store:
            due_job(store)
            engine = SchedulerEngine(
                store,
                submit,
                owner="engine",
                clock=lambda: dt("2026-01-01T00:00:00+00:00"),
            )
            await engine.tick()
            await engine.drain()
            run = store.list_runs()[0]
            assert run.status is JobRunStatus.FAILED
            assert run.execution_error


async def test_delivery_success_and_missing_callback(tmp_path: Path) -> None:
    delivered: list[str] = []

    async def delivery(_target: object, run_id: str) -> None:
        delivered.append(run_id)

    for index, callback in enumerate((delivery, None)):
        with SchedulerStore(tmp_path / f"delivery-{index}.sqlite3") as store:
            due_job(store, delivery=True)
            engine = SchedulerEngine(
                store,
                lambda _payload: "run",
                callback,
                owner="engine",
                clock=lambda: dt("2026-01-01T00:00:00+00:00"),
            )
            await engine.tick()
            await engine.drain()
            run = store.list_runs()[0]
            expected = DeliveryStatus.SUCCEEDED if callback else DeliveryStatus.FAILED
            assert run.delivery_status is expected
    assert delivered == ["run"]


async def test_slow_inline_delivery_is_not_dispatched_again_by_tick(tmp_path: Path) -> None:
    entered = asyncio.Event()
    release = asyncio.Event()
    deliveries = 0

    async def delivery(_target: object, _run_id: str) -> None:
        nonlocal deliveries
        deliveries += 1
        entered.set()
        await release.wait()

    with SchedulerStore(tmp_path / "journal.sqlite3") as store:
        due_job(store, delivery=True)
        engine = SchedulerEngine(
            store,
            lambda _payload: "run",
            delivery,
            owner="engine",
            clock=lambda: dt("2026-01-01T00:00:00+00:00"),
        )

        await engine.tick()
        await entered.wait()
        assert store.list_runs()[0].delivery_status is DeliveryStatus.PENDING

        await engine.tick()
        await asyncio.sleep(0)
        assert deliveries == 1

        release.set()
        await engine.drain()
        assert store.list_runs()[0].delivery_status is DeliveryStatus.SUCCEEDED


def test_engine_constructor_validation(tmp_path: Path) -> None:
    with SchedulerStore(tmp_path / "journal.sqlite3") as store:
        for options in (
            {"lease": 0},
            {"batch": 0},
            {"tick_seconds": 0},
            {"jitter_seconds": -1},
        ):
            try:
                SchedulerEngine(store, lambda _payload: "run", **options)  # type: ignore[arg-type]
            except ValueError:
                pass
            else:
                raise AssertionError("invalid engine options must fail")


async def test_engine_heartbeats_long_async_submission(tmp_path: Path) -> None:
    current = [dt("2026-01-01T00:00:00+00:00")]
    sleeps = 0

    async def advancing_sleep(delay: float) -> None:
        nonlocal sleeps
        sleeps += 1
        current[0] += timedelta(seconds=delay)
        await asyncio.sleep(0)

    async def submit(_payload: JobPayload) -> str:
        for _ in range(4):
            await asyncio.sleep(0)
        return "run"

    with SchedulerStore(tmp_path / "journal.sqlite3") as store:
        due_job(store)
        engine = SchedulerEngine(
            store,
            submit,
            owner="engine",
            lease=0.03,
            clock=lambda: current[0],
            sleep=advancing_sleep,
        )
        await engine.tick()
        await engine.drain()
        assert sleeps > 0
        assert store.list_runs()[0].status is JobRunStatus.SUCCEEDED


async def test_tick_sweeps_lease_that_expires_after_early_restart(tmp_path: Path) -> None:
    current = [dt("2026-01-01T00:00:05+00:00")]
    with SchedulerStore(tmp_path / "journal.sqlite3") as store:
        due_job(store)
        claimed = store.claim_due_runs(
            dt("2026-01-01T00:00:00+00:00"),
            "old-engine",
            10,
        )[0]
        store.mark_running(
            claimed.id,
            "old-engine",
            now=dt("2026-01-01T00:00:00+00:00"),
        )
        engine = SchedulerEngine(
            store,
            lambda _payload: "must-not-run",
            owner="new-engine",
            clock=lambda: current[0],
        )

        await engine.tick()
        assert store.get_run(claimed.id).status is JobRunStatus.RUNNING

        current[0] = dt("2026-01-01T00:00:10+00:00")
        await engine.tick()
        stale = store.get_run(claimed.id)
        assert stale.status is JobRunStatus.INTERRUPTED
        assert stale.polaris_run_id is None


async def test_run_id_is_persisted_before_wait_and_pause_can_resume(tmp_path: Path) -> None:
    waiting = asyncio.Event()
    resume = asyncio.Event()
    delivered: list[str] = []

    async def wait_for_terminal(run_id: str, job_run_id: str) -> None:
        linked = store.get_run(job_run_id)
        assert linked.polaris_run_id == run_id
        waiting.set()
        await resume.wait()

    async def deliver(_target: object, run_id: str) -> None:
        delivered.append(run_id)

    with SchedulerStore(tmp_path / "journal.sqlite3") as store:
        due_job(store, delivery=True)
        engine = SchedulerEngine(
            store,
            lambda _payload: "durable-agent-run",
            deliver,
            owner="engine",
            wait=wait_for_terminal,
            clock=lambda: dt("2026-01-01T00:00:00+00:00"),
        )
        ticking = asyncio.create_task(engine.tick())
        await waiting.wait()

        blocked = store.list_runs()[0]
        assert blocked.status is JobRunStatus.RUNNING
        assert blocked.polaris_run_id == "durable-agent-run"
        assert ticking.done()

        resume.set()
        await ticking
        await engine.drain()
        completed = store.list_runs()[0]
        assert completed.status is JobRunStatus.SUCCEEDED
        assert completed.delivery_status is DeliveryStatus.SUCCEEDED
        assert delivered == ["durable-agent-run"]


async def test_cancel_running_job_requests_cancel_and_prevents_delivery(
    tmp_path: Path,
) -> None:
    waiting = asyncio.Event()
    release = asyncio.Event()
    delivered = False

    async def wait_for_terminal(_run_id: str, _job_run_id: str) -> None:
        waiting.set()
        await release.wait()

    async def deliver(_target: object, _run_id: str) -> None:
        nonlocal delivered
        delivered = True

    with SchedulerStore(tmp_path / "journal.sqlite3") as store:
        due_job(store, delivery=True)
        job = store.list_jobs()[0]
        engine = SchedulerEngine(
            store,
            lambda _payload: "agent-run",
            deliver,
            owner="engine",
            wait=wait_for_terminal,
            clock=lambda: dt("2026-01-01T00:00:00+00:00"),
        )
        ticking = asyncio.create_task(engine.tick())
        await waiting.wait()

        store.cancel_job(job.id, now=dt("2026-01-01T00:00:00+00:00"))
        active = store.list_runs()[0]
        assert active.cancel_requested
        release.set()
        await ticking
        await engine.drain()

        cancelled = store.list_runs()[0]
        assert cancelled.status is JobRunStatus.CANCELLED
        assert cancelled.delivery_status is DeliveryStatus.SUPPRESSED
        assert not delivered


async def test_paused_execution_does_not_block_future_ticks(tmp_path: Path) -> None:
    first_waiting = asyncio.Event()
    release = asyncio.Event()
    submitted: list[str] = []

    async def submit(payload: JobPayload) -> str:
        prompt = str(payload.request["prompt"])
        submitted.append(prompt)
        if prompt == "first":
            first_waiting.set()
            await release.wait()
        return f"run-{prompt}"

    with SchedulerStore(tmp_path / "journal.sqlite3") as store:
        for job_id in ("first", "second"):
            store.create_job(
                ScheduleSpec.once("2026-01-01T00:00:00+00:00"),
                JobPayload.single({"prompt": job_id}),
                job_id=job_id,
                now=dt("2025-12-31T00:00:00+00:00"),
            )
        engine = SchedulerEngine(
            store,
            submit,
            owner="engine",
            batch=1,
            clock=lambda: dt("2026-01-01T00:00:00+00:00"),
        )

        first_tick = await engine.tick()
        await first_waiting.wait()
        second_tick = await engine.tick()
        await asyncio.sleep(0)

        assert [run.job_id for run in first_tick] == ["first"]
        assert [run.job_id for run in second_tick] == ["second"]
        assert submitted == ["first", "second"]
        release.set()
        await engine.drain()
        assert all(run.status is JobRunStatus.SUCCEEDED for run in store.list_runs())
