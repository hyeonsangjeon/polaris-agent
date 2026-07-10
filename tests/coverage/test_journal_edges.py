from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from polaris.journal import (
    BudgetExceededError,
    InvalidTransitionError,
    Journal,
    JournalClosedError,
    JournalConflictError,
    JournalNotFoundError,
    JournalValidationError,
    OwnershipError,
    RunStatus,
    SafetyClass,
    StepStatus,
)


def test_journal_open_and_input_validation(tmp_path: Path) -> None:
    with pytest.raises(JournalValidationError, match="busy_timeout"):
        Journal(tmp_path / "bad.sqlite3", busy_timeout_ms=-1)

    journal = Journal(tmp_path / "journal.sqlite3")
    with pytest.raises(JournalValidationError, match="mode"):
        journal.create_run("", {}, {})
    with pytest.raises(JournalValidationError, match="either"):
        journal.create_run("x", {}, {}, {}, budget={})
    with pytest.raises(JournalValidationError, match="unknown budget"):
        journal.create_run("x", {}, {}, {"unknown": 1})
    with pytest.raises(JournalValidationError, match="non-negative"):
        journal.create_run("x", {}, {}, {"max_calls": True})
    with pytest.raises(JournalNotFoundError):
        journal.create_run("child", {}, {}, parent_run_id="missing")
    with pytest.raises(JournalValidationError, match="unknown run status"):
        journal.list_runs("unknown")

    run = journal.create_run("test", {}, {})
    assert journal.list_runs([]) == []
    assert journal.mark_run_status(run.id, RunStatus.CREATED) == run
    journal.close()
    journal.close()
    with pytest.raises(JournalClosedError):
        journal.get_run(run.id)


def test_journal_step_filters_validation_and_cancellation(tmp_path: Path) -> None:
    with Journal(tmp_path / "journal.sqlite3") as journal:
        run = journal.create_run("test", {}, {})
        with pytest.raises(JournalValidationError):
            journal.create_step(run.id, "", "name", {}, SafetyClass.READ_ONLY)
        with pytest.raises(JournalValidationError):
            journal.create_step(run.id, "tool", "name", {}, "invalid")
        with pytest.raises(JournalValidationError):
            journal.create_step(run.id, "tool", "name", {}, SafetyClass.READ_ONLY, -1)

        first = journal.create_step(
            run.id, "tool", "first", {}, SafetyClass.READ_ONLY, sequence=1
        )
        second = journal.create_step(
            run.id, "tool", "second", {}, SafetyClass.READ_ONLY, sequence=2
        )
        assert journal.create_step(
            run.id, "tool", "first", {}, SafetyClass.READ_ONLY, sequence=1
        ).id == first.id
        assert [step.id for step in journal.list_steps(run.id, StepStatus.READY)] == [
            first.id,
            second.id,
        ]
        assert journal.list_steps(run.id, []) == []
        with pytest.raises(JournalValidationError, match="worker_id"):
            journal.claim_ready_step("", 1)
        with pytest.raises(JournalValidationError, match="lease_seconds"):
            journal.claim_ready_step("worker", 0)
        with pytest.raises(JournalNotFoundError):
            journal.claim_ready_step("worker", 1, "missing")

        claimed = journal.claim_ready_step("worker", 10, run.id)
        assert claimed is not None
        with pytest.raises(OwnershipError, match="required"):
            journal.cancel_step(claimed.id)
        assert journal.cancel_step(claimed.id, "worker").status is StepStatus.CANCELLED
        with pytest.raises(InvalidTransitionError):
            journal.cancel_step(claimed.id)
        assert journal.cancel_step(second.id).status is StepStatus.CANCELLED
        assert journal.claim_ready_step("worker", 1, run.id) is None


def test_journal_receipt_duplicate_conflict_and_commit_validation(tmp_path: Path) -> None:
    with Journal(tmp_path / "journal.sqlite3") as journal:
        run = journal.create_run("test", {}, {})
        step = journal.create_step(run.id, "tool", "write", {}, SafetyClass.IDEMPOTENT)
        assert journal.find_receipt("missing") is None
        with pytest.raises(JournalValidationError, match="idempotency"):
            journal.record_receipt(step.id, "", {})
        receipt = journal.record_receipt(step.id, "key", {"value": 1})
        assert journal.record_receipt(step.id, "key", {"value": 1}) == receipt
        assert journal.find_receipt("key") == receipt
        with pytest.raises(JournalConflictError, match="different"):
            journal.record_receipt(step.id, "key", {"value": 2})

        journal.claim_ready_step("worker", 10)
        journal.mark_executing(step.id, "worker")
        with pytest.raises(JournalValidationError, match="idempotency_key"):
            journal.commit_step(step.id, "worker", {}, receipt={})
        committed = journal.commit_step(
            step.id,
            "worker",
            {"ok": True},
            receipt={"idempotency_key": "commit-key", "receipt": {"id": 1}},
        )
        assert committed.status is StepStatus.COMMITTED


def test_journal_approval_decisions_filters_and_cross_run_guards(tmp_path: Path) -> None:
    with Journal(tmp_path / "journal.sqlite3") as journal:
        run = journal.create_run("test", {}, {})
        other = journal.create_run("test", {}, {})
        step = journal.create_step(run.id, "tool", "name", {}, SafetyClass.READ_ONLY)
        other_step = journal.create_step(
            other.id, "tool", "name", {}, SafetyClass.READ_ONLY
        )
        with pytest.raises(JournalValidationError, match="kind"):
            journal.request_approval(run.id, kind="")
        with pytest.raises(JournalValidationError, match="belong"):
            journal.request_approval(run.id, other_step.id)

        approval = journal.request_approval(run.id, step.id, {"why": "needed"}, kind="tool")
        assert journal.get_approval(approval.id) == approval
        assert journal.list_pending_approvals(run.id) == [approval]
        assert journal.list_approvals(step_id=step.id, status="pending") == [approval]
        with pytest.raises(JournalValidationError, match="decision"):
            journal.decide_approval(approval.id, "maybe", "tester")
        with pytest.raises(JournalValidationError, match="decided_by"):
            journal.decide_approval(approval.id, True, "")
        decided = journal.decide_approval(approval.id, True, "tester", "safe")
        assert decided.status == "approved"
        assert journal.decide_approval(approval.id, "approved", "tester") == decided
        with pytest.raises(InvalidTransitionError):
            journal.decide_approval(approval.id, False, "other")
        assert journal.list_pending_approvals(run.id) == []


def test_journal_artifact_provider_and_event_boundaries(tmp_path: Path) -> None:
    with Journal(tmp_path / "journal.sqlite3") as journal:
        run = journal.create_run("test", {}, {})
        other = journal.create_run("test", {}, {})
        step = journal.create_step(run.id, "tool", "name", {}, SafetyClass.READ_ONLY)
        other_step = journal.create_step(
            other.id, "tool", "name", {}, SafetyClass.READ_ONLY
        )
        with pytest.raises(JournalValidationError):
            journal.record_artifact(run.id, "", "u")
        with pytest.raises(JournalValidationError):
            journal.record_artifact(run.id, "n", "")
        with pytest.raises(JournalValidationError, match="size_bytes"):
            journal.record_artifact(run.id, "n", "u", size_bytes=-1)
        with pytest.raises(JournalValidationError, match="belong"):
            journal.record_artifact(run.id, "n", "u", step_id=other_step.id)
        artifact = journal.record_artifact(
            run.id,
            "output",
            "file:///output",
            step_id=step.id,
            size_bytes=0,
            metadata={"kind": "test"},
        )
        assert journal.list_artifacts(run.id) == [artifact]

        with pytest.raises(JournalValidationError, match="provider"):
            journal.start_provider_call(run.id, "", {})
        call = journal.start_provider_call(run.id, "fake", {"prompt": "x"}, model="asked")
        with pytest.raises(JournalValidationError, match="status"):
            journal.complete_provider_call(call.id, response={}, status="bad")
        with pytest.raises(JournalValidationError, match="input_tokens"):
            journal.complete_provider_call(call.id, response={}, input_tokens=-1)
        completed = journal.complete_provider_call(
            call.id,
            response={"answer": "ok"},
            status="uncertain",
            model="actual",
            input_tokens=1,
            output_tokens=2,
            micro_usd=3,
        )
        assert completed.status == "uncertain"
        with pytest.raises(InvalidTransitionError):
            journal.complete_provider_call(call.id, response={})

        with pytest.raises(JournalValidationError, match="event_type"):
            journal.append_event(run.id, "", {})
        with pytest.raises(JournalValidationError, match="belong"):
            journal.append_event(run.id, "bad", {}, step_id=other_step.id)
        custom = journal.append_event(run.id, "custom", {}, step_id=step.id)
        assert journal.get_event(custom.id) == custom
        assert journal.list_events(run.id, step_id=step.id, after_id=0, limit=1)
        with pytest.raises(JournalValidationError, match="limit"):
            journal.list_events(run.id, limit=0)
        with pytest.raises(JournalNotFoundError):
            journal.get_event(999999)


def test_journal_budget_idempotency_conflict_limits_and_settlement(tmp_path: Path) -> None:
    with Journal(tmp_path / "journal.sqlite3") as journal:
        run = journal.create_run(
            "test",
            {},
            {},
            {
                "calls": 2,
                "tokens": 5,
                "micro_usd": 5,
                "wall_seconds": 2,
            },
        )
        for kwargs in (
            {"calls": True},
            {"tokens": -1},
            {"wall_seconds": -1},
        ):
            with pytest.raises(JournalValidationError):
                journal.reserve_budget(run.id, **kwargs)

        reservation = journal.reserve_budget(
            run.id,
            calls=1,
            tokens=2,
            micro_usd=3,
            wall_seconds=1,
            reservation_id="fixed",
        )
        assert journal.reserve_budget(
            run.id,
            calls=1,
            tokens=2,
            micro_usd=3,
            wall_seconds=1,
            reservation_id="fixed",
        ) == reservation
        with pytest.raises(JournalConflictError):
            journal.reserve_budget(run.id, calls=2, reservation_id="fixed")
        with pytest.raises(BudgetExceededError, match="tokens"):
            journal.reserve_budget(run.id, tokens=4)

        other = journal.create_run("test", {}, {})
        with pytest.raises(JournalValidationError, match="does not belong"):
            journal.settle_budget(other.id, reservation, actual_calls=1)
        with pytest.raises(JournalValidationError, match="either"):
            journal.settle_budget(
                run.id, reservation, actual_calls=1, calls=1
            )
        budget = journal.settle_budget(
            run.id,
            reservation,
            calls=1,
            tokens=2,
            micro_usd=2,
            wall_seconds=0.5,
        )
        assert budget.used_tokens == 2
        assert journal.settle_budget(
            run.id,
            reservation,
            calls=1,
            tokens=2,
            micro_usd=2,
            wall_seconds=0.5,
        ) == budget
        with pytest.raises(JournalConflictError, match="settled differently"):
            journal.settle_budget(run.id, reservation, calls=2)


def test_journal_uncertainty_failure_and_recovery_paths(tmp_path: Path) -> None:
    with Journal(tmp_path / "journal.sqlite3") as journal:
        run = journal.create_run("test", {}, {})
        uncertain = journal.create_step(
            run.id, "tool", "opaque", {}, SafetyClass.OPAQUE_SIDE_EFFECT
        )
        journal.claim_ready_step("worker", 10)
        with pytest.raises(JournalValidationError, match="reason"):
            journal.mark_uncertain(uncertain.id, "worker", "")
        marked = journal.mark_uncertain(uncertain.id, "worker", "unknown outcome")
        assert marked.status is StepStatus.UNCERTAIN

        failed = journal.create_step(
            run.id, "tool", "read", {}, SafetyClass.READ_ONLY
        )
        journal.claim_ready_step("worker", 10)
        result = journal.fail_step(failed.id, "worker", {"message": "failed"})
        assert result.status is StepStatus.FAILED

        expiring = journal.create_step(
            run.id, "tool", "retry", {}, SafetyClass.READ_ONLY
        )
        journal.claim_ready_step("worker", 0.01)
        reclaimed = journal.reclaim_expired_leases(
            datetime.now(UTC) + timedelta(seconds=1)
        )
        assert [item.id for item in reclaimed] == [expiring.id]
