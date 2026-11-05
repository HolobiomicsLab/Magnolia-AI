"""Authorized cancel/cleanup/resubmit flow tests.

Run from opencode_cc_mem/:
    python3 -m pytest tests/test_cancel_resubmit.py -q
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from router_runner import agentd
from router_runner.job_service import MARKER
from router_runner.models import ApprovalError, RouterError, WorkItemState, WorkSpec

NEW_SPEC = WorkSpec(
    project="demo", protocol="nk2r-challenge-v0.1",
    scope="NK2R funnel with s20000", budget="local seconds",
)


def make_engine(tmp_path):
    return agentd.get_engine(str(tmp_path / "agentd.db"), out_root=tmp_path / "runs")


def parked(engine, run_id="run_A", event=None):
    req = agentd.ingest_letter(
        engine, {**agentd.NK2R_LETTER, "source_event_id": event or run_id}
    )
    wi = req["work_item_id"]
    engine.approve(wi, req["nonce"], agentd.operator())
    engine.job_service.submit(run_id)
    engine.park_on_job(wi, run_id)
    return wi


def test_happy_path_cancel_cleanup_resubmit(tmp_path):
    engine = make_engine(tmp_path)
    wi = parked(engine)
    old_dir = Path(engine.job_service.jobs["run_A"]["run_dir"])
    outcome = engine.cancel_and_resubmit(wi, NEW_SPEC, "user changed sampling", agentd.operator())

    assert outcome["resubmitted"] is True
    assert outcome["cancelled"] == "run_A"
    assert not old_dir.exists(), "old run dir must be deleted"
    new_run = outcome["new_run_id"]
    assert (engine.job_service.root / new_run / MARKER).exists()
    wi_row = engine.store.get("work_items", "work_item_id", wi)
    assert wi_row["state"] == WorkItemState.WAITING_JOB
    assert wi_row["job_run_id"] == new_run
    assert wi_row["scope"] == "NK2R funnel with s20000"
    links = [l for l in engine.store.all("evidence_links") if l["kind"] == "superseded_run"]
    assert len(links) == 1 and links[0]["ref_id"] == "run_A"
    decisions = [d["outcome"] for d in engine.store.all("decisions")]
    for expected in ("cancelled", "cleaned", "amended", "resubmitted"):
        assert expected in decisions


def test_spec_change_requires_approver(tmp_path):
    engine = make_engine(tmp_path)
    wi = parked(engine)
    try:
        engine.cancel_and_resubmit(wi, NEW_SPEC, "unauthorized change", None)
        assert False
    except ApprovalError:
        pass
    # Nothing was cancelled or cleaned.
    assert (engine.job_service.root / "run_A" / MARKER).exists()
    assert engine.job_service.status("run_A") == "submitted"


def test_terminal_race_fetches_instead_of_deleting(tmp_path):
    engine = make_engine(tmp_path)
    wi = parked(engine, run_id="run_B")
    engine.job_service.set_state("run_B", "completed")
    outcome = engine.cancel_and_resubmit(wi, NEW_SPEC, "late change", agentd.operator())
    assert outcome == {"resubmitted": False, "reason": "already_terminal", "run_id": "run_B"}
    assert (engine.job_service.root / "run_B" / MARKER).exists()
    assert engine.store.get("work_items", "work_item_id", wi)["job_run_id"] == "run_B"


def test_cleanup_guard_refuses_missing_marker(tmp_path):
    engine = make_engine(tmp_path)
    service = engine.job_service
    service.submit("run_C")
    (service.root / "run_C" / MARKER).unlink()
    try:
        service.cleanup("run_C")
        assert False
    except RouterError as e:
        assert "marker" in str(e)
    assert (service.root / "run_C").exists()


def test_cancel_requires_waiting_job(tmp_path):
    engine = make_engine(tmp_path)
    req = agentd.ingest_letter(engine, agentd.NK2R_LETTER)
    engine.approve(req["work_item_id"], req["nonce"], agentd.operator())
    try:
        engine.cancel_and_resubmit(req["work_item_id"], NEW_SPEC, "too early", agentd.operator())
        assert False
    except RouterError as e:
        assert "waiting_job" in str(e)


def test_demo_cancel(tmp_path):
    rc = agentd.demo_cancel(str(tmp_path / "demo.db"), workdir=str(tmp_path / "wd"))
    assert rc == 0
