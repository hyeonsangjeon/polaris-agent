"""Async single-node scheduler daemon."""

from __future__ import annotations

import asyncio
import inspect
import random
import uuid
from collections.abc import Awaitable, Callable, Coroutine, Mapping
from datetime import datetime, timedelta
from typing import Any

from .models import DeliveryStatus, JobPayload, JobRun, JobRunStatus, ensure_aware, utc_now
from .store import SchedulerStore

SubmitCallback = Callable[[JobPayload], Any | Awaitable[Any]]
WaitCallback = Callable[[str, str], Any | Awaitable[Any]]
DeliveryCallback = Callable[[Mapping[str, Any], str], Any | Awaitable[Any]]
Clock = Callable[[], datetime]
Sleep = Callable[[float], Awaitable[None]]


async def _resolve(value: Any | Awaitable[Any]) -> Any:
    if inspect.isawaitable(value):
        return await value
    return value


def _run_id(result: Any) -> str:
    if isinstance(result, str):
        return result
    if isinstance(result, Mapping):
        value = result.get("run_id") or result.get("id")
    else:
        value = getattr(result, "run_id", None) or getattr(result, "id", None)
    if not isinstance(value, str) or not value:
        raise ValueError("submit callback must return a run id")
    return value


class SchedulerEngine:
    """One ticker that claims durable runs and dispatches callbacks."""

    def __init__(
        self,
        store: SchedulerStore,
        submit: SubmitCallback,
        delivery: DeliveryCallback | None = None,
        *,
        owner: str | None = None,
        lease: timedelta | float = 60,
        batch: int = 32,
        tick_seconds: float = 1,
        jitter_seconds: float = 0,
        jitter_seed: int | None = None,
        startup_cap: int | None = None,
        wait: WaitCallback | None = None,
        clock: Clock = utc_now,
        sleep: Sleep = asyncio.sleep,
    ) -> None:
        lease_delta = lease if isinstance(lease, timedelta) else timedelta(seconds=lease)
        if lease_delta <= timedelta(0):
            raise ValueError("lease must be positive")
        if batch < 1 or tick_seconds <= 0 or jitter_seconds < 0:
            raise ValueError("invalid scheduler engine timing or batch")
        self.store = store
        self.submit = submit
        self.wait = wait
        self.delivery = delivery
        self.owner = owner or f"scheduler-{uuid.uuid4()}"
        self.lease = lease_delta
        self.batch = batch
        self.tick_seconds = tick_seconds
        self.jitter_seconds = jitter_seconds
        self.startup_cap = startup_cap
        self.clock = clock
        self.sleep = sleep
        self._random = random.Random(jitter_seed)
        self._tick_lock = asyncio.Lock()
        self._wake = asyncio.Event()
        self._closing = False
        self._ticker: asyncio.Task[None] | None = None
        self._active: set[asyncio.Task[None]] = set()
        self._active_deliveries: set[str] = set()
        self._first_tick = True

    @property
    def running(self) -> bool:
        return self._ticker is not None and not self._ticker.done()

    async def start(self) -> None:
        if self.running:
            return
        self._closing = False
        self._ticker = asyncio.create_task(self._run(), name=f"scheduler:{self.owner}")
        await asyncio.sleep(0)

    async def close(self, *, drain: bool = True) -> None:
        self._closing = True
        self._wake.set()
        ticker = self._ticker
        if ticker is not None:
            await ticker
        self._ticker = None
        if drain:
            await self.drain()

    async def drain(self) -> None:
        while self._active:
            await asyncio.gather(*tuple(self._active), return_exceptions=True)

    def wake(self) -> None:
        self._wake.set()

    def retry(self, run_id: str) -> JobRun:
        """Explicitly re-claim one unsuccessful run and dispatch it once."""
        claimed = self.store.create_retry(
            run_id,
            self.owner,
            self.lease,
            approved=True,
            now=self.clock(),
        )
        self._spawn(
            self._execute(claimed),
            name=f"scheduler-retry:{claimed.id}",
        )
        return claimed

    def _spawn(
        self,
        operation: Coroutine[Any, Any, None],
        *,
        name: str,
        delivery_run_id: str | None = None,
    ) -> None:
        task = asyncio.create_task(operation, name=name)
        self._active.add(task)
        if delivery_run_id is not None:
            self._active_deliveries.add(delivery_run_id)

        def completed(done: asyncio.Task[None]) -> None:
            self._active.discard(done)
            if delivery_run_id is not None:
                self._active_deliveries.discard(delivery_run_id)
            if done.cancelled():
                return
            error = done.exception()
            if error is not None:
                done.get_loop().call_exception_handler(
                    {
                        "message": "scheduler background task failed",
                        "exception": error,
                        "task": done,
                    }
                )

        task.add_done_callback(completed)

    async def _run(self) -> None:
        while not self._closing:
            await self.tick()
            if self._closing:
                break
            delay = self.tick_seconds
            if self.jitter_seconds:
                delay += self._random.uniform(0, self.jitter_seconds)
            await self._wait_or_wake(delay)

    async def _wait_or_wake(self, delay: float) -> None:
        async def wait_for_wake() -> None:
            await self._wake.wait()

        self._wake.clear()
        sleeper: asyncio.Future[None] = asyncio.ensure_future(self.sleep(delay))
        wake = asyncio.create_task(wait_for_wake())
        done, pending = await asyncio.wait(
            {sleeper, wake},
            return_when=asyncio.FIRST_COMPLETED,
        )
        for task in pending:
            task.cancel()
        await asyncio.gather(*done, *pending, return_exceptions=True)

    async def tick(self) -> tuple[JobRun, ...]:
        async with self._tick_lock:
            now = ensure_aware(self.clock(), "clock")
            self.store.recover_stale_runs(now)
            for pending in self.store.list_pending_deliveries(limit=self.batch):
                if pending.id not in self._active_deliveries:
                    self._spawn(
                        self._deliver_run(pending.id),
                        name=f"scheduler-delivery:{pending.id}",
                        delivery_run_id=pending.id,
                    )
            cap = self.startup_cap if self._first_tick else None
            self._first_tick = False
            claimed = self.store.claim_due_runs(
                now,
                self.owner,
                self.lease,
                self.batch,
                startup_cap=cap,
            )
            for run in claimed:
                self._spawn(
                    self._execute(run),
                    name=f"scheduler-execution:{run.id}",
                )
            return claimed

    async def _execute(self, claimed: JobRun) -> None:
        current = self.store.get_run(claimed.id)
        if current.cancel_requested:
            self.store.cancel(current.id, owner=self.owner, now=self.clock())
            return
        current = self.store.mark_running(current.id, self.owner, now=self.clock())
        if current.status is JobRunStatus.CANCELLED:
            return
        assert current.payload is not None
        heartbeat = asyncio.create_task(
            self._heartbeat(current.id),
            name=f"scheduler-heartbeat:{current.id}",
        )
        polaris_run_id: str | None = None
        try:
            result = await _resolve(self.submit(current.payload))
            polaris_run_id = _run_id(result)
            self.store.set_polaris_run_id(
                current.id,
                self.owner,
                polaris_run_id,
                now=self.clock(),
            )
            if self.wait is not None:
                await _resolve(self.wait(polaris_run_id, current.id))
        except BaseException as exc:
            if isinstance(exc, asyncio.CancelledError):
                raise
            current = self.store.get_run(current.id)
            status = (
                JobRunStatus.CANCELLED
                if current.cancel_requested
                else JobRunStatus.FAILED
            )
            self.store.complete(
                current.id,
                self.owner,
                status,
                error=None if status is JobRunStatus.CANCELLED else f"{type(exc).__name__}: {exc}",
                polaris_run_id=polaris_run_id,
                now=self.clock(),
            )
            return
        finally:
            heartbeat.cancel()
            await asyncio.gather(heartbeat, return_exceptions=True)
        current = self.store.get_run(current.id)
        completed = self.store.complete(
            current.id,
            self.owner,
            (
                JobRunStatus.CANCELLED
                if current.cancel_requested
                else JobRunStatus.SUCCEEDED
            ),
            polaris_run_id=polaris_run_id,
            now=self.clock(),
        )
        if completed.status is not JobRunStatus.SUCCEEDED:
            return
        await asyncio.sleep(0)
        await self._deliver_inline(current.id)

    async def _deliver_inline(self, run_id: str) -> None:
        if run_id in self._active_deliveries:
            return
        self._active_deliveries.add(run_id)
        try:
            await self._deliver_run(run_id)
        finally:
            self._active_deliveries.discard(run_id)

    async def _deliver_run(self, run_id: str) -> None:
        current = self.store.get_run(run_id)
        if (
            current.status is not JobRunStatus.SUCCEEDED
            or current.delivery_status is not DeliveryStatus.PENDING
        ):
            return
        if current.payload is None or current.payload.delivery is None:
            return
        if current.cancel_requested:
            self.store.record_delivery(
                current.id,
                DeliveryStatus.SUPPRESSED,
                now=self.clock(),
            )
            return
        if self.delivery is None:
            self.store.record_delivery(
                current.id,
                DeliveryStatus.FAILED,
                error="no delivery callback configured",
                now=self.clock(),
            )
            return
        try:
            if current.polaris_run_id is None:
                raise ValueError("successful scheduled run has no Polaris run id")
            await _resolve(self.delivery(current.payload.delivery, current.polaris_run_id))
        except BaseException as exc:
            if isinstance(exc, asyncio.CancelledError):
                raise
            self.store.record_delivery(
                current.id,
                DeliveryStatus.FAILED,
                error=f"{type(exc).__name__}: {exc}",
                now=self.clock(),
            )
        else:
            self.store.record_delivery(
                current.id,
                DeliveryStatus.SUCCEEDED,
                now=self.clock(),
            )

    async def _heartbeat(self, run_id: str) -> None:
        delay = max(0.01, self.lease.total_seconds() / 3)
        while True:
            await self.sleep(delay)
            self.store.heartbeat(
                run_id,
                self.owner,
                self.lease,
                now=self.clock(),
            )
