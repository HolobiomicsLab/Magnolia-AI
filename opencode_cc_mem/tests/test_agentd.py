"""Tests for agentd + the NK2R task pack (always-on agent system).

Run from opencode_cc_mem/:
    python3 -m pytest tests/test_agentd.py -q
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from router_runner import agentd
from router_runner.campaign_adapter import protocol_hash
from router_runner.models import RouterError


def make_engine(tmp_path):
    return agentd.ControlEngine(
        agentd.ControlStore(str(tmp_path / "agentd.db")),
        agentd.default_policy(),
        agentd.CompositeAdapter(out_root=tmp_path / "runs"),
    )


def test_nk2r_protocol_is_registered_and_hashed():
    h = protocol_hash("nk2r-challenge-v0.1")
    assert isinstance(h, str) and len(h) == 64
    try:
        protocol_hash("nk2r-challenge-v999")
        assert False
    except RouterError:
        pass


def test_nk2r_taskpack_end_to_end(tmp_path):
    engine = make_engine(tmp_path)
    req = agentd.ingest_letter(engine, agentd.NK2R_LETTER)
    engine.approve(req["work_item_id"], req["nonce"], "human:tao")
    outcome = engine.execute(req["work_item_id"])
    result = outcome["result"]
    assert result["outcome"] == "completed"
    assert result["receipt"]["selected"] == 60
    assert all(result["gate_report"]["acceptance"].values())
    candidate_file = Path(result["candidate_file"])
    assert candidate_file.exists()
    assert len(candidate_file.read_text().strip().splitlines()) == 61
    links = engine.store.all("evidence_links", "work_item_id = ?", (req["work_item_id"],))
    assert {l["kind"] for l in links} == {"run_record", "gate_report", "ranked_candidates"}
    wi = engine.store.get("work_items", "work_item_id", req["work_item_id"])
    assert wi["state"] == "completed"


def test_watch_stages_candidates_without_executing(tmp_path):
    inbox = tmp_path / "inbox"
    inbox.mkdir()
    (inbox / "letter.json").write_text(json.dumps(agentd.NK2R_LETTER))
    engine = make_engine(tmp_path)
    summary = agentd.watch(inbox, str(tmp_path / "agentd.db"), once=True,
                           out_root=tmp_path / "runs")
    assert summary["executed"] == 0
    assert len(summary["staged"]) == 1
    assert summary["rejected"] == []
    wi = engine.store.find_one("work_items", "1=1", ())
    assert wi["state"] == "candidate"
    assert engine.store.all("assignments") == []


def test_watch_is_idempotent_across_sweeps(tmp_path):
    inbox = tmp_path / "inbox"
    inbox.mkdir()
    (inbox / "letter.json").write_text(json.dumps(agentd.NK2R_LETTER))
    db = str(tmp_path / "agentd.db")
    agentd.watch(inbox, db, once=True, out_root=tmp_path / "runs")
    summary = agentd.watch(inbox, db, once=True, out_root=tmp_path / "runs")
    # Second sweep: same letter is rejected as a duplicate, no new Work Item.
    assert summary["staged"] == []
    assert len(summary["rejected"]) == 1
    engine = make_engine(tmp_path)
    assert len(engine.store.all("work_items")) == 1
    assert len(engine.store.all("triggers")) == 1
