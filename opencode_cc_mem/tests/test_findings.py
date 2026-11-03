"""Outcome-status enforcement for findings (assessment §11.5).

Run from opencode_cc_mem/:
    python3 -m pytest tests/test_findings.py -q
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from router_runner import agentd
from router_runner.models import RouterError


def make_engine(tmp_path, adapter=None):
    return agentd.ControlEngine(
        agentd.ControlStore(str(tmp_path / "agentd.db")),
        agentd.default_policy(),
        adapter or agentd.CompositeAdapter(out_root=tmp_path / "runs"),
    )


def approved(engine):
    req = agentd.ingest_letter(engine, agentd.NK2R_LETTER)
    engine.approve(req["work_item_id"], req["nonce"], "human:tao")
    return req["work_item_id"]


class FailingAdapter:
    def run(self, work_item, mandate, attempt=1):
        return {
            "run_id": "run_fail",
            "protocol": work_item["protocol"],
            "protocol_hash": "x",
            "outcome": "failed",
            "gate_report": {"acceptance": {"calibration_passed": False}},
            "receipt": {"work_item_id": work_item["work_item_id"]},
        }

    def reconcile(self, work_item, mandate, attempt):
        return None


def test_write_path_defaults_to_intermediate_and_validates(tmp_path):
    engine = make_engine(tmp_path)
    wi = approved(engine)
    finding = engine.record_finding(wi, "scientific", "motif X scores well")
    assert finding["outcome_status"] == "intermediate"
    assert finding["attempt"] == 1
    try:
        engine.record_finding(wi, "scientific", "bad", outcome_status="final")
        assert False
    except RouterError:
        pass
    try:
        engine.record_finding(wi, "vibes", "bad kind")
        assert False
    except RouterError:
        pass


def test_completion_promotes_findings(tmp_path):
    engine = make_engine(tmp_path)
    wi = approved(engine)
    engine.record_finding(wi, "scientific", "finding A")
    engine.record_finding(wi, "operational", "finding B")
    engine.execute(wi)
    rows = engine.findings(wi)
    assert {r["outcome_status"] for r in rows} == {"completed_campaign"}
    # And the promotion is a recorded decision.
    decisions = engine.store.all("decisions")
    assert any(d["outcome"] == "findings_promoted" for d in decisions)


def test_failed_outcome_keeps_intermediate(tmp_path):
    engine = make_engine(tmp_path, adapter=FailingAdapter())
    wi = approved(engine)
    engine.record_finding(wi, "scientific", "premature claim")
    engine.execute(wi)
    rows = engine.findings(wi)
    assert [r["outcome_status"] for r in rows] == ["intermediate"]
    assert engine.store.get("work_items", "work_item_id", wi)["state"] == "failed"


def test_final_only_filter_for_guidance(tmp_path):
    engine = make_engine(tmp_path)
    wi = approved(engine)
    engine.record_finding(wi, "scientific", "intermediate observation")
    engine.record_finding(wi, "scientific", "completed result", outcome_status="completed_campaign")
    validated = engine.record_finding(wi, "operational", "human-approved lesson")
    engine.validate_finding(validated["finding_id"], "human:tao")
    final = engine.findings(wi, final_only=True)
    assert {r["text"] for r in final} == {"completed result", "human-approved lesson"}
    assert len(engine.findings(wi)) == 3


def test_validate_finding_requires_known_id(tmp_path):
    engine = make_engine(tmp_path)
    try:
        engine.validate_finding("fnd_nope", "human:tao")
        assert False
    except RouterError:
        pass


def test_context_package_findings_digest(tmp_path):
    engine = make_engine(tmp_path)
    wi = approved(engine)
    engine.record_finding(wi, "scientific", "will be promoted")
    engine.execute(wi)
    package = engine.context_package(wi)
    digest = package["findings_digest"]
    assert digest["counts"] == {"completed_campaign": 1}
    assert digest["final"][0]["text"] == "will be promoted"
