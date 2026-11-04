"""Job park/wake loop tests: waiting_job + notice consumption.

Run from opencode_cc_mem/:
    python3 -m pytest tests/test_jobloop.py -q
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from router_runner import agentd
from router_runner.models import WorkItemState


def make_engine(tmp_path):
    return agentd.ControlEngine(
        agentd.ControlStore(str(tmp_path / "agentd.db")),
        agentd.default_policy(),
        agentd.CompositeAdapter(out_root=tmp_path / "runs"),
    )


def parked(engine):
    req = agentd.ingest_letter(engine, agentd.NK2R_LETTER)
    wi = req["work_item_id"]
    engine.approve(wi, req["nonce"], "human:tao")
    engine.park_on_job(wi, "run_x1")
    return wi


def notice(run_id="run_x1", state="COMPLETED", category="success"):
    return {
        "kind": "job_notice",
        "run_id": run_id,
        "state": state,
        "category": category,
        "event": {"source_id": "jobs:poller", "source_event_id": f"{run_id}:{state}"},
    }


def write_notice(inbox: Path, n: dict) -> Path:
    inbox.mkdir(parents=True, exist_ok=True)
    path = inbox / f"{n['run_id']}__{n['state'].lower()}.json"
    path.write_text(json.dumps(n))
    return path


def test_park_then_notice_wakes_owner_with_delta(tmp_path):
    engine = make_engine(tmp_path)
    wi = parked(engine)
    assert engine.store.get("work_items", "work_item_id", wi)["state"] == WorkItemState.WAITING_JOB

    # Mid-wait user info is a context post, not execution.
    engine.follow_up(wi, "human:tao", "use s20000 next round")
    write_notice(tmp_path / "inbox" / "from-jobs", notice())
    summary = engine.consume_job_notices(tmp_path / "inbox", include_context=True)

    assert summary["woken"] == [wi]
    wi_row = engine.store.get("work_items", "work_item_id", wi)
    assert wi_row["state"] == WorkItemState.QUEUED
    package = summary["context_packages"][wi]
    actors = {p["actor"] for p in package["delta"]["new_posts"]}
    assert actors == {"human:tao", "system:job-watcher"}
    kinds = {l["kind"] for l in package["evidence_index"]}
    assert "job_notice" in kinds


def test_notice_without_owner_is_left_unconsumed(tmp_path):
    engine = make_engine(tmp_path)
    write_notice(tmp_path / "inbox" / "from-jobs", notice(run_id="run_unknown"))
    summary = engine.consume_job_notices(tmp_path / "inbox")
    assert summary["woken"] == []
    assert summary["results"][0]["reason"] == "no_owner"
    # Not marked consumed: a later park on that run could still wake.
    assert engine.store.all("consumed_notices") == []


def test_duplicate_notice_is_idempotent(tmp_path):
    engine = make_engine(tmp_path)
    wi = parked(engine)
    write_notice(tmp_path / "inbox" / "from-jobs", notice())
    first = engine.consume_job_notices(tmp_path / "inbox")
    second = engine.consume_job_notices(tmp_path / "inbox")
    assert first["woken"] == [wi]
    assert second["woken"] == []
    assert second["results"][0]["reason"] == "duplicate"
    # Exactly one watcher post and one job_notice evidence link.
    posts = [p for p in engine.store.all("thread_posts") if p["actor"] == "system:job-watcher"]
    assert len(posts) == 1
    links = [l for l in engine.store.all("evidence_links") if l["kind"] == "job_notice"]
    assert len(links) == 1


def test_failed_job_wakes_for_analysis_not_auto_failure(tmp_path):
    engine = make_engine(tmp_path)
    wi = parked(engine)
    write_notice(tmp_path / "inbox" / "from-jobs", notice(state="FAILED", category="science_failure"))
    summary = engine.consume_job_notices(tmp_path / "inbox")
    assert summary["woken"] == [wi]
    # The worker decides what the failure means; the control plane only wakes.
    assert engine.store.get("work_items", "work_item_id", wi)["state"] == WorkItemState.QUEUED


def test_full_loop_demo(tmp_path):
    db = str(tmp_path / "demo.db")
    rc = agentd.demo_jobloop(db, out_root=tmp_path / "runs")
    assert rc == 0


def test_demo_local_end_to_end(tmp_path):
    db = str(tmp_path / "local.db")
    rc = agentd.demo_local(db, workdir=str(tmp_path / "wd"))
    assert rc == 0
    # Real production notice was emitted by job_watcher, not hand-written.
    notices = list((tmp_path / "wd" / "project" / "inbox" / "from-jobs").glob("*.json"))
    assert len(notices) == 1
    notice = json.loads(notices[0].read_text())
    assert notice["kind"] == "job_notice"
    assert notice["state"] == "COMPLETED"
    # Two linked Work Items, each with 60 candidates and promoted findings.
    engine = agentd.get_engine(db, out_root=tmp_path / "wd" / "runs")
    items = engine.store.all("work_items")
    assert len(items) == 2
    assert all(i["state"] == "completed" for i in items)
    child = [i for i in items if i["parent_work_item_id"]]
    assert len(child) == 1
    for item in items:
        assert engine.findings(item["work_item_id"], final_only=True)
    # Batch run directories exist and are attempt-scoped.
    runs = list((tmp_path / "wd" / "runs").glob("*/attempt-1/ranked_candidates.csv"))
    assert len(runs) == 2
