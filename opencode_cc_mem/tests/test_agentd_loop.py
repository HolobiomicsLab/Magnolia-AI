"""Event loop + lane-letter bridge tests.

Run from opencode_cc_mem/:
    python3 -m pytest tests/test_agentd_loop.py -q
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from router_runner import agentd
from router_runner.models import AssignmentState, WorkItemState


def make_engine(tmp_path):
    return agentd.get_engine(str(tmp_path / "agentd.db"), out_root=tmp_path / "runs")


def parked_with_notice(tmp_path):
    engine = make_engine(tmp_path)
    project = tmp_path / "project"
    req = agentd.ingest_letter(engine, agentd.NK2R_LETTER)
    wi = req["work_item_id"]
    engine.approve(wi, req["nonce"], "human:tao")
    engine.job_service.submit("run_loop_1")
    engine.park_on_job(wi, "run_loop_1")
    engine.follow_up(wi, "human:tao", "mid-wait note for the lane letter")
    inbox = project / "inbox"
    (inbox / "from-jobs").mkdir(parents=True)
    (inbox / "from-jobs" / "run_loop_1__completed.json").write_text(json.dumps({
        "kind": "job_notice",
        "run_id": "run_loop_1",
        "state": "COMPLETED",
        "category": "success",
        "event": {"source_id": "jobs:poller", "source_event_id": "run_loop_1:COMPLETED"},
    }))
    return engine, project, wi


def test_bridge_writes_self_contained_letter(tmp_path):
    engine, project, wi = parked_with_notice(tmp_path)
    summary = engine.consume_job_notices(project / "inbox")
    assert summary["woken"] == [wi]
    result = agentd.bridge_wake_to_letter(engine, wi, project / "inbox")
    assert result["written"] is True
    text = Path(result["path"]).read_text()
    assert "status: open" in text.splitlines()[0]
    assert wi in text
    assert "run_loop_1" in text
    assert "mid-wait note for the lane letter" in text
    assert "FINAL ANSWER:" in text


def test_bridge_is_idempotent(tmp_path):
    engine, project, wi = parked_with_notice(tmp_path)
    engine.consume_job_notices(project / "inbox")
    first = agentd.bridge_wake_to_letter(engine, wi, project / "inbox")
    second = agentd.bridge_wake_to_letter(engine, wi, project / "inbox")
    assert first["written"] is True
    assert second == {"written": False, "reason": "duplicate", "path": first["path"]}
    assert len(list((project / "inbox").glob("*.task.md"))) == 1


def test_run_loop_once_wakes_and_bridges_without_executing(tmp_path):
    engine, project, wi = parked_with_notice(tmp_path)
    db = str(tmp_path / "agentd.db")
    rc = agentd.run_loop(db, str(project), once=True)
    assert rc == 0
    letters = list((project / "inbox").glob("*.task.md"))
    assert len(letters) == 1
    assert "status: open" in letters[0].read_text().splitlines()[0]
    # The loop never executes: the Work Item is queued, no assignments exist
    # beyond the parked submission (park does not create assignments).
    fresh = make_engine(tmp_path)
    assert fresh.store.get("work_items", "work_item_id", wi)["state"] == WorkItemState.QUEUED
    assert fresh.store.all("assignments", "work_item_id = ?", (wi,)) == []


def test_run_loop_once_is_idempotent(tmp_path):
    engine, project, wi = parked_with_notice(tmp_path)
    db = str(tmp_path / "agentd.db")
    agentd.run_loop(db, str(project), once=True)
    agentd.run_loop(db, str(project), once=True)
    assert len(list((project / "inbox").glob("*.task.md"))) == 1
