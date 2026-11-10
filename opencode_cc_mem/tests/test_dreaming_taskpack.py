"""Tests for the memory-dreaming task pack (always-on agent integration).

Run from opencode_cc_mem/:
    python3 -m pytest tests/test_dreaming_taskpack.py -q
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import yaml

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from router_runner import agentd
from router_runner.campaign_adapter import protocol_hash
from router_runner.models import RouterError
from router_runner import taskpacks


def make_engine(tmp_path):
    return agentd.ControlEngine(
        agentd.ControlStore(str(tmp_path / "agentd.db")),
        agentd.default_policy(),
        agentd.CompositeAdapter(out_root=tmp_path / "runs"),
    )


def make_project_store(tmp_path):
    store = tmp_path / ".magnolia"
    (store / "staging").mkdir(parents=True)
    (store / "sessions").mkdir()
    (store / "sessions" / "2026-11-01_000000.jsonl").write_text(json.dumps({
        "timestamp": "2026-11-01T00:00:00+00:00",
        "event_type": "tool_call",
        "tool": "demo",
        "result_summary": "run completed",
    }) + "\n")
    (store / "staging" / "20261101_000000_000001_entry.md").write_text(
        "---\n"
        "id: '20261101_000000_000001'\n"
        "title: Demo entry\n"
        "type: workflow_note\n"
        "opencode_session_id: '2026-11-01_000000'\n"
        "---\n\nThe run completed.\n"
    )
    return store


def wrong_judge(entry, transcript):
    return {
        "verdict": "wrong",
        "corrected_text": "The run did NOT complete; it stopped at the gate.",
        "evidence": [{"source": "session 2026-11-01_000000", "quote": "run completed"}],
        "rationale": "transcript shows a stop, not a completion",
    }


def test_dreaming_protocol_is_registered_and_hashed():
    h = protocol_hash("memory-dreaming-v0.1")
    assert isinstance(h, str) and len(h) == 64
    try:
        protocol_hash("memory-dreaming-v999")
        assert False
    except RouterError:
        pass


def test_dreaming_fails_closed_without_magnolia_store(tmp_path):
    work_item = {"work_item_id": "wi_test", "protocol": "memory-dreaming-v0.1"}
    mandate = {"mandate_id": "m_test", "scope": "test"}
    try:
        taskpacks.run_dreaming(work_item, mandate, out_root=tmp_path / "runs")
        assert False, "expected RouterError for a project dir without .magnolia"
    except RouterError as exc:
        assert ".magnolia" in str(exc)


def test_dreaming_taskpack_end_to_end(tmp_path, monkeypatch):
    make_project_store(tmp_path)
    dreaming = taskpacks._load_dreaming()
    monkeypatch.setattr(dreaming, "_default_judge", wrong_judge)

    engine = make_engine(tmp_path)
    req = agentd.ingest_letter(engine, agentd.DREAMING_LETTER)
    engine.approve(req["work_item_id"], req["nonce"], agentd.operator())
    outcome = engine.execute(req["work_item_id"])
    result = outcome["result"]

    assert result["outcome"] == "completed"
    assert result["receipt"]["checked"] == 1
    assert result["receipt"]["corrected"] == 1
    assert result["receipt"]["mutations"] == 1
    assert all(result["gate_report"]["acceptance"].values())

    entry = tmp_path / ".magnolia" / "staging" / "20261101_000000_000001_entry.md"
    text = entry.read_text()
    assert "## Dreaming correction" in text
    assert "did NOT complete" in text
    meta = yaml.safe_load(text.split("---", 2)[1])
    assert meta["dreaming_status"] == "corrected"

    report = Path(result["candidate_file"])
    assert report.exists()
    assert "Dreaming pass report" in report.read_text()

    links = engine.store.all("evidence_links", "work_item_id = ?", (req["work_item_id"],))
    assert {l["kind"] for l in links} == {"run_record", "gate_report", "ranked_candidates"}
    wi = engine.store.get("work_items", "work_item_id", req["work_item_id"])
    assert wi["state"] == "completed"

    # Reconcile from the run record: no second execution.
    mandate = engine.store.get("mandates", "work_item_id", req["work_item_id"])
    reconciled = taskpacks.run_dreaming_reconcile(
        wi, mandate, attempt=1, out_root=tmp_path / "runs"
    )
    assert reconciled is not None
    assert reconciled["receipt"]["checked"] == 1
    assert reconciled["receipt"]["mutations"] == 1


def test_dreaming_reconcile_returns_none_without_record(tmp_path):
    work_item = {"work_item_id": "wi_missing", "protocol": "memory-dreaming-v0.1"}
    mandate = {"mandate_id": "m_missing", "scope": "test"}
    assert taskpacks.run_dreaming_reconcile(
        work_item, mandate, attempt=1, out_root=tmp_path / "runs"
    ) is None
