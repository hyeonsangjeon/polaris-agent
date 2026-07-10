from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime, timedelta
from pathlib import Path
from time import sleep

import pytest

from polaris.journal import (
    BudgetExceededError,
    InvalidTransitionError,
    Journal,
    LeaseExpiredError,
    OwnershipError,
    RunStatus,
    SafetyClass,
    StepStatus,
)


def make_run(journal: Journal, **limits: int) -> str:
    return journal.create_run("test", {"task": "journal"}, {}, limits).id


def test_schema_persistence_events_and_transitions(tmp_path: Path) -> None:
    path = tmp_path / "state" / "journal.sqlite3"
    with Journal(path) as journal:
        run = journal.create_run("interactive", {"prompt": "hello"}, {"model": "test"})
        assert journal.schema_version == 1
        journal.mark_run_status(run.id, RunStatus.RUNNING)
        event = journal.append_event(run.id, "custom.event", {"ok": True})
        assert event.type == "custom.event"

    with Journal(path) as journal:
        assert journal.get_run(run.id).status is RunStatus.RUNNING
        assert [event.type for event in journal.materialized_timeline(run.id)] == [
            "run.created",
            "run.status_changed",
            "custom.event",
        ]
        journal.mark_run_status(run.id, RunStatus.COMPLETED)
        with pytest.raises(InvalidTransitionError):
            journal.mark_run_status(run.id, RunStatus.RUNNING)


def test_two_claimers_only_claim_once(tmp_path: Path) -> None:
    path = tmp_path / "journal.sqlite3"
    with Journal(path) as setup:
        run_id = make_run(setup)
        step_id = setup.create_step(
            run_id, "tool", "read", {"path": "a"}, SafetyClass.READ_ONLY
        ).id

    def claim(worker: str) -> str | None:
        with Journal(path) as journal:
            step = journal.claim_ready_step(worker, 30, run_id)
            return None if step is None else step.id

    with ThreadPoolExecutor(max_workers=2) as pool:
        claimed = list(pool.map(claim, ("one", "two")))
    assert sorted(item for item in claimed if item is not None) == [step_id]


def test_ownership_heartbeat_and_transitions(tmp_path: Path) -> None:
    with Journal(tmp_path / "journal.sqlite3") as journal:
        run_id = make_run(journal)
        step = journal.create_step(
            run_id, "tool", "read", {}, SafetyClass.READ_ONLY
        )
        claimed = journal.claim_ready_step("owner", 30)
        assert claimed is not None and claimed.id == step.id
        with pytest.raises(OwnershipError):
            journal.heartbeat(step.id, "other", 30)
        heartbeat = journal.heartbeat(step.id, "owner", 30)
        assert heartbeat.lease_expires_at is not None
        executing = journal.mark_executing(step.id, "owner")
        assert executing.status is StepStatus.EXECUTING
        with pytest.raises(InvalidTransitionError):
            journal.mark_executing(step.id, "owner")
        committed = journal.commit_step(step.id, "owner", {"result": 1})
        assert committed.status is StepStatus.COMMITTED
        with pytest.raises(InvalidTransitionError):
            journal.fail_step(step.id, "owner", {"message": "late"})


@pytest.mark.parametrize(
    ("safety", "expected"),
    [
        (SafetyClass.READ_ONLY, StepStatus.READY),
        (SafetyClass.IDEMPOTENT, StepStatus.READY),
        (SafetyClass.RECONCILABLE, StepStatus.UNCERTAIN),
        (SafetyClass.OPAQUE_SIDE_EFFECT, StepStatus.UNCERTAIN),
    ],
)
def test_expired_lease_recovery(
    tmp_path: Path, safety: SafetyClass, expected: StepStatus
) -> None:
    with Journal(tmp_path / f"{safety}.sqlite3") as journal:
        run_id = make_run(journal)
        step = journal.create_step(run_id, "tool", safety.value, {}, safety)
        journal.claim_ready_step("worker", 0.01)
        future = datetime.now(UTC) + timedelta(seconds=1)
        reclaimed = journal.reclaim_expired_leases(future)
        assert [item.id for item in reclaimed] == [step.id]
        assert reclaimed[0].status is expected


def test_expired_owner_cannot_act(tmp_path: Path) -> None:
    with Journal(tmp_path / "journal.sqlite3") as journal:
        run_id = make_run(journal)
        step = journal.create_step(run_id, "tool", "read", {}, SafetyClass.READ_ONLY)
        journal.claim_ready_step("worker", 0.01)
        sleep(0.02)
        with pytest.raises(LeaseExpiredError):
            journal.heartbeat(step.id, "worker", 30)


def test_atomic_budget_reservations(tmp_path: Path) -> None:
    path = tmp_path / "journal.sqlite3"
    with Journal(path) as setup:
        run_id = make_run(setup, max_calls=1, max_tokens=10, max_micro_usd=100)

    def reserve() -> str | None:
        with Journal(path) as journal:
            try:
                return journal.reserve_budget(
                    run_id, calls=1, tokens=10, micro_usd=100
                )
            except BudgetExceededError:
                return None

    with ThreadPoolExecutor(max_workers=2) as pool:
        reservations = list(pool.map(lambda _: reserve(), range(2)))
    assert len([item for item in reservations if item]) == 1

    reservation = next(item for item in reservations if item)
    with Journal(path) as journal:
        budget = journal.settle_budget(
            run_id,
            reservation,
            actual_calls=1,
            actual_tokens=7,
            actual_micro_usd=90,
        )
        assert budget.reserved_calls == 0
        assert budget.used_calls == 1
        with pytest.raises(BudgetExceededError):
            journal.reserve_budget(run_id, calls=1)
