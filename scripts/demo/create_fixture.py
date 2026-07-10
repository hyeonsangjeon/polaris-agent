#!/usr/bin/env python3
"""Create a deterministic, explicitly synthetic Polaris demo fixture."""

from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path
from typing import Any

FIXED_TIME = "2026-01-01T00:00:00+00:00"
RUN_ID = "run_demo_offline_0001"


def canonical(value: Any) -> bytes:
    text = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return (text + "\n").encode()


def write(path: Path, payload: bytes) -> dict[str, Any]:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(payload)
    return {
        "path": f"artifacts/{path.name}",
        "sha256": hashlib.sha256(payload).hexdigest(),
        "size_bytes": len(payload),
    }


def create(output: Path) -> None:
    output = output.resolve()
    artifacts = output / "artifacts"
    output.mkdir(parents=True, exist_ok=True)

    run = {
        "fixture": True,
        "fixture_notice": (
            "Synthetic offline storyboard; no daemon, model, tool, or network call occurred."
        ),
        "id": RUN_ID,
        "mode": "fan-out",
        "status": "completed",
        "created_at": FIXED_TIME,
        "updated_at": "2026-01-01T00:00:08+00:00",
        "requested_models": ["fixture-ollama"],
        "actual_models": ["fixture-ollama"],
        "budget": {
            "call_limit": 8,
            "used_calls": 5,
            "token_limit": 4000,
            "used_tokens": 640,
        },
    }
    write(output / "run.json", canonical(run))

    events = [
        {"id": 1, "at": FIXED_TIME, "type": "run.created", "run_id": RUN_ID},
        {
            "id": 2,
            "at": "2026-01-01T00:00:02+00:00",
            "type": "fixture.daemon_killed",
            "run_id": RUN_ID,
            "note": "Storyboard marker only; no process was killed.",
        },
        {
            "id": 3,
            "at": "2026-01-01T00:00:05+00:00",
            "type": "step.lease_expired",
            "run_id": RUN_ID,
            "step_id": "step_worker_operations",
        },
        {
            "id": 4,
            "at": "2026-01-01T00:00:06+00:00",
            "type": "service.recovered",
            "run_id": RUN_ID,
            "resumed_steps": ["step_worker_operations"],
            "reused_committed_steps": ["step_worker_recovery", "step_worker_security"],
        },
        {
            "id": 5,
            "at": "2026-01-01T00:00:07+00:00",
            "type": "ensemble.disagreement_recorded",
            "run_id": RUN_ID,
            "claim": "automatic retry is always safe",
        },
        {
            "id": 6,
            "at": "2026-01-01T00:00:08+00:00",
            "type": "run.completed",
            "run_id": RUN_ID,
        },
    ]
    timeline = b"".join(canonical(event) for event in events)
    write(output / "timeline.jsonl", timeline)

    report = (
        b"# Synthetic recovery report\n\n"
        b"> Fixture only: generated without a daemon, model, tool, or network call.\n\n"
        b"Two committed worker records were reused. One expired read-only worker "
        b"record was resumed. The verifier rejected the claim that every effect "
        b"is safe to retry; opaque ambiguous effects require operator approval.\n"
    )
    evidence = [
        {
            "claim_id": "claim-1",
            "source_id": "fixture-durability-contract",
            "quote": "Opaque ambiguous effects stop for operator approval.",
            "content_hash": hashlib.sha256(
                b"Opaque ambiguous effects stop for operator approval."
            ).hexdigest(),
            "synthetic": True,
        }
    ]
    disagreements = (
        b"# Synthetic disagreements\n\n"
        b"- Rejected: \"automatic retry is always safe.\"\n"
        b"- Retained: only verified read-only, idempotent, or reconciled work is eligible.\n"
    )
    records = [
        write(artifacts / "report.md", report),
        write(
            artifacts / "evidence.jsonl",
            b"".join(canonical(item) for item in evidence),
        ),
        write(artifacts / "disagreements.md", disagreements),
    ]

    manifest = {
        "fixture": True,
        "schema": "polaris.demo.fixture.v1",
        "run_id": RUN_ID,
        "generated_at": FIXED_TIME,
        "network_calls": 0,
        "provider_calls": 0,
        "tool_calls": 0,
        "files": sorted(records, key=lambda item: item["path"]),
    }
    write(output / "manifest.json", canonical(manifest))
    print(f"Created deterministic offline fixture at {output}")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--output",
        type=Path,
        default=Path("demo-output"),
        help="Output directory (default: ./demo-output)",
    )
    args = parser.parse_args()
    create(args.output)


if __name__ == "__main__":
    main()
