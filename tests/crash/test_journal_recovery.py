from __future__ import annotations

from pathlib import Path

from polaris.journal import Journal, SafetyClass, StepStatus


def test_receipt_idempotent_step_and_approval_survive_restart(tmp_path: Path) -> None:
    path = tmp_path / "nested" / "journal.sqlite3"
    with Journal(path) as journal:
        run = journal.create_run("test", {"prompt": "go"}, {}, {"max_calls": 2})
        first = journal.create_step(
            run.id,
            "tool",
            "write",
            {"value": 1},
            SafetyClass.OPAQUE_SIDE_EFFECT,
            sequence=1,
        )
        second = journal.create_step(
            run.id,
            "tool",
            "write",
            {"value": 1},
            SafetyClass.OPAQUE_SIDE_EFFECT,
            sequence=1,
        )
        assert first.id == second.id
        receipt = journal.record_receipt(first.id, "operation-1", {"remote_id": "x"})
        approval = journal.request_approval(
            run.id, first.id, {"reason": "side effect"}, kind="tool"
        )
        journal.decide_approval(approval.id, True, "operator")

    with Journal(path) as journal:
        assert journal.find_receipt("operation-1") == receipt
        restored = journal.get_approval(approval.id)
        assert restored.status == "approved"
        assert restored.decided_by == "operator"
        assert journal.list_pending_approvals(run.id) == []
        assert journal.get_step(first.id).status is StepStatus.READY


def test_wal_database_can_reopen_while_reader_exists(tmp_path: Path) -> None:
    path = tmp_path / "journal.sqlite3"
    first = Journal(path)
    try:
        run = first.create_run("test", {}, {})
        with Journal(path) as second:
            assert second.get_run(run.id).id == run.id
            second.append_event(run.id, "reopened", {})
        assert first.list_events(run.id)[-1].type == "reopened"
    finally:
        first.close()
