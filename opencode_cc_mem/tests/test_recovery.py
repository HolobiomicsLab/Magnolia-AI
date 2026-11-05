"""Crash-window tests for the always-on agent system.

Simulates the two failure modes found in the pre-recovery prototype:
  A. crash AFTER effect stored but BEFORE the side effect (attempt must be
     recovered and re-executed without wedging in RUNNING);
  B. crash AFTER the external run finished but BEFORE the receipt write
     (attempt must be RECONCILED from existing records, not duplicated).
Plus the attempt lease: a live owner blocks any second executor.

Run from opencode_cc_mem/:
    python3 -m pytest tests/test_recovery.py -q
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from router_runner import agentd
from router_runner.models import AssignmentState, RouterError, WorkItemState


def make_engine(tmp_path):
    return agentd.ControlEngine(
        agentd.ControlStore(str(tmp_path / "agentd.db")),
        agentd.default_policy(),
        agentd.CompositeAdapter(out_root=tmp_path / "runs"),
    )


def dead_pid() -> int:
    """A PID that is guaranteed not to be alive (spawn + reap a child)."""
    import subprocess
    proc = subprocess.Popen(["true"])
    proc.wait()
    return proc.pid


def approved_nk2r(engine):
    req = agentd.ingest_letter(engine, agentd.NK2R_LETTER)
    engine.approve(req["work_item_id"], req["nonce"], agentd.operator())
    return req["work_item_id"]


def simulate_crash_before_side_effect(engine, work_item_id, attempt=1):
    """Reproduce the wedge: RUNNING + running assignment(dead pid) + effect stored."""
    engine.store.update(
        "work_items", "work_item_id", work_item_id, {"state": WorkItemState.RUNNING}
    )
    asg_id = f"asg_crash_{attempt}"
    engine.store.insert(
        "assignments",
        {
            "assignment_id": asg_id,
            "work_item_id": work_item_id,
            "executor_kind": "composite-adapter",
            "attempt": attempt,
            "state": AssignmentState.RUNNING,
            "owner_pid": dead_pid(),
            "created_at": "2026-11-03T00:00:00+00:00",
            "ended_at": None,
        },
    )
    engine.store.insert(
        "effects",
        {
            "effect_id": f"eff_crash_{attempt}",
            "work_item_id": work_item_id,
            "kind": "campaign_run",
            "idempotency_key": f"run:{work_item_id}:{attempt}",
            "payload": "{}",
            "state": "stored",
            "receipt": None,
            "created_at": "2026-11-03T00:00:00+00:00",
        },
    )
    return asg_id


def test_crash_before_side_effect_recovers_and_completes(tmp_path):
    engine = make_engine(tmp_path)
    wi = approved_nk2r(engine)
    asg_id = simulate_crash_before_side_effect(engine, wi)

    recovered = engine.recover()
    assert recovered == [wi]
    assert engine.store.get("work_items", "work_item_id", wi)["state"] == "queued"
    assert engine.store.get("assignments", "assignment_id", asg_id)["state"] == "interrupted"

    outcome = engine.execute(wi)
    assert outcome["result"]["outcome"] == "completed"
    assert outcome["reconciled"] is False  # no external run existed to reconcile
    wi_row = engine.store.get("work_items", "work_item_id", wi)
    assert wi_row["state"] == "completed"
    # Exactly one NEW attempt beyond the interrupted one.
    attempts = engine.store.all("assignments", "work_item_id = ?", (wi,))
    assert len(attempts) == 2
    # The new attempt used its own idempotency key and finished.
    effect = engine.store.get("effects", "idempotency_key", f"run:{wi}:2")
    assert effect["state"] == "done"


def test_crash_after_side_effect_reconciles_without_duplicate(tmp_path):
    engine = make_engine(tmp_path)
    wi = approved_nk2r(engine)
    first = engine.execute(wi)
    original_candidate_file = first["result"]["candidate_file"]

    # Simulate: same attempt's records exist on disk, but the receipt write
    # never happened (crash after adapter.run, before _finalize).
    engine.store.update(
        "work_items", "work_item_id", wi, {"state": WorkItemState.RUNNING}
    )
    engine.store.update(
        "assignments", "work_item_id", wi,
        {"state": AssignmentState.RUNNING, "owner_pid": dead_pid(), "ended_at": None},
    )
    engine.store.update(
        "effects", "idempotency_key", f"run:{wi}:1", {"receipt": None, "state": "stored"}
    )
    engine.store.conn.execute("DELETE FROM evidence_links WHERE work_item_id = ?", (wi,))
    engine.store.conn.commit()

    outcome = engine.execute(wi)
    assert outcome["reconciled"] is True
    assert outcome["result"]["candidate_file"] == original_candidate_file
    # No second attempt was created; no duplicate external run.
    assert len(engine.store.all("assignments", "work_item_id = ?", (wi,))) == 1
    run_dir = Path(original_candidate_file).parent
    assert len(list(run_dir.parent.glob("attempt-*"))) == 1
    assert engine.store.get("work_items", "work_item_id", wi)["state"] == "completed"


def test_live_owner_blocks_second_executor(tmp_path):
    engine = make_engine(tmp_path)
    wi = approved_nk2r(engine)
    engine.store.update(
        "work_items", "work_item_id", wi, {"state": WorkItemState.RUNNING}
    )
    engine.store.insert(
        "assignments",
        {
            "assignment_id": "asg_live",
            "work_item_id": wi,
            "executor_kind": "composite-adapter",
            "attempt": 1,
            "state": AssignmentState.RUNNING,
            "owner_pid": os.getpid(),  # alive: this test process
            "created_at": "2026-11-03T00:00:00+00:00",
            "ended_at": None,
        },
    )
    try:
        engine.execute(wi)
        assert False, "live owner must block re-execution"
    except RouterError as e:
        assert "already in progress" in str(e)


def test_attempt_scoped_run_directories(tmp_path):
    engine = make_engine(tmp_path)
    wi = approved_nk2r(engine)
    result = engine.execute(wi)["result"]
    run_dir = Path(result["candidate_file"]).parent
    assert run_dir.name == "attempt-1"
    assert run_dir.parent.name == wi
