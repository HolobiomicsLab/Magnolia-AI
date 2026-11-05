"""Deterministic tests for the Router vertical-slice prototype.

Run from opencode_cc_mem/:
    python3 -m pytest tests/test_router_runner.py -q
    python3 tests/test_router_runner.py        # standalone, no pytest needed
"""

from __future__ import annotations

import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from router_runner.admission import AdmissionPolicy
from router_runner.engine import ControlEngine
from router_runner.models import (
    ApprovalError,
    IllegalTransition,
    RouterError,
    TriggerSpec,
    WorkSpec,
)
from router_runner.store import ControlStore

SPEC = WorkSpec(
    project="demo",
    protocol="demo-docking-v1",
    scope="test scope",
    budget="0 (fake)",
)


def make_engine(db_path):
    policy = AdmissionPolicy(
        allowed_senders=frozenset({"human:tao"}),
        allowed_projects=frozenset({"demo"}),
    )
    return ControlEngine(ControlStore(str(db_path)), policy)


def letter(event="evt-1", actor="human:tao", project="demo"):
    return TriggerSpec(
        source_id="inbox:test",
        source_event_id=event,
        actor=actor,
        project_hint=project,
        payload={"title": "t"},
    )


def admitted_work_item(engine, event="evt-1"):
    trigger, _ = engine.ingest(letter(event))
    req = engine.request_work(trigger["trigger_id"], SPEC)
    engine.approve(req["work_item_id"], req["nonce"], "human:tao")
    return req


def test_duplicate_intake_creates_one_trigger(tmp_path):
    engine = make_engine(tmp_path / "a.db")
    t1, created1 = engine.ingest(letter())
    t2, created2 = engine.ingest(letter())
    assert created1 and not created2
    assert t1["trigger_id"] == t2["trigger_id"]
    assert len(engine.store.all("triggers")) == 1


def test_duplicate_work_request_rejected(tmp_path):
    engine = make_engine(tmp_path / "a.db")
    admitted_work_item(engine)
    trigger = engine.store.find_one("triggers", "1=1", ())
    try:
        engine.request_work(trigger["trigger_id"], SPEC)
        assert False, "duplicate work request should fail"
    except RouterError as e:
        assert "duplicate" in str(e)


def test_admission_rejects_unknown_sender(tmp_path):
    engine = make_engine(tmp_path / "a.db")
    trigger, _ = engine.ingest(letter(actor="mallory"))
    try:
        engine.request_work(trigger["trigger_id"], SPEC)
        assert False
    except RouterError as e:
        assert "sender not allowed" in str(e)


def test_admission_rejects_unknown_project(tmp_path):
    engine = make_engine(tmp_path / "a.db")
    trigger, _ = engine.ingest(letter(project="other_project"))
    try:
        engine.request_work(trigger["trigger_id"], SPEC)
        assert False
    except RouterError as e:
        assert "project not allowed" in str(e)


def test_approval_requires_correct_nonce(tmp_path):
    engine = make_engine(tmp_path / "a.db")
    trigger, _ = engine.ingest(letter())
    req = engine.request_work(trigger["trigger_id"], SPEC)
    try:
        engine.approve(req["work_item_id"], "f" * 32, "human:tao")
        assert False
    except ApprovalError:
        pass
    wi = engine.store.get("work_items", "work_item_id", req["work_item_id"])
    assert wi["state"] == "candidate" and wi["mandate_id"] is None


def test_approval_is_single_use(tmp_path):
    engine = make_engine(tmp_path / "a.db")
    req = admitted_work_item(engine)
    try:
        engine.approve(req["work_item_id"], req["nonce"], "human:tao")
        assert False
    except ApprovalError as e:
        assert "single-use" in str(e)


def test_approval_binds_exact_content(tmp_path):
    engine = make_engine(tmp_path / "a.db")
    trigger, _ = engine.ingest(letter())
    req = engine.request_work(trigger["trigger_id"], SPEC)
    # Simulate content drift after the approval request was issued.
    engine.store.update(
        "work_items", "work_item_id", req["work_item_id"], {"scope": "wider scope"}
    )
    try:
        engine.approve(req["work_item_id"], req["nonce"], "human:tao")
        assert False
    except ApprovalError as e:
        assert "content changed" in str(e)


def test_no_mandate_no_execution(tmp_path):
    engine = make_engine(tmp_path / "a.db")
    trigger, _ = engine.ingest(letter())
    req = engine.request_work(trigger["trigger_id"], SPEC)
    try:
        engine.execute(req["work_item_id"])
        assert False
    except RouterError as e:
        assert "no Mandate" in str(e)


def test_execute_completes_with_receipt_and_evidence(tmp_path):
    engine = make_engine(tmp_path / "a.db")
    req = admitted_work_item(engine)
    outcome = engine.execute(req["work_item_id"])
    result = outcome["result"]
    wi = engine.store.get("work_items", "work_item_id", req["work_item_id"])
    assert wi["state"] == "completed" and wi["outcome"] == "completed"
    assert result["receipt"]["mandate_id"] == wi["mandate_id"]
    links = engine.store.all("evidence_links", "work_item_id = ?", (wi["work_item_id"],))
    assert {l["kind"] for l in links} == {"run_record", "gate_report"}
    effect = engine.store.get("effects", "idempotency_key", f"run:{wi['work_item_id']}:1")
    assert effect["state"] == "done"


def test_reexecution_is_idempotent(tmp_path):
    engine = make_engine(tmp_path / "a.db")
    req = admitted_work_item(engine)
    engine.execute(req["work_item_id"])
    again = engine.execute(req["work_item_id"])
    assert again["already_completed"] is True
    assert len(engine.store.all("assignments")) == 1
    assert len(engine.store.all("effects", "kind = ?", ("campaign_run",))) == 1


def test_reply_is_receipted_and_idempotent(tmp_path):
    engine = make_engine(tmp_path / "a.db")
    req = admitted_work_item(engine)
    first = engine.reply(req["work_item_id"], "done")
    second = engine.reply(req["work_item_id"], "done")
    assert first["already_sent"] is False and second["already_sent"] is True


def test_restart_recovers_state(tmp_path):
    db = tmp_path / "a.db"
    engine = make_engine(db)
    req = admitted_work_item(engine)
    engine.execute(req["work_item_id"])
    engine.store.close()
    engine2 = make_engine(db)
    wi = engine2.store.get("work_items", "work_item_id", req["work_item_id"])
    assert wi["state"] == "completed"
    assert len(engine2.store.all("evidence_links")) == 2


def test_illegal_transition_rejected(tmp_path):
    engine = make_engine(tmp_path / "a.db")
    req = admitted_work_item(engine)
    engine.execute(req["work_item_id"])
    try:
        engine.stop(req["work_item_id"], "human:tao")
        assert False
    except IllegalTransition:
        pass


def test_human_stop_prevents_resume(tmp_path):
    engine = make_engine(tmp_path / "a.db")
    req = admitted_work_item(engine)
    engine.stop(req["work_item_id"], "human:tao")
    try:
        engine.execute(req["work_item_id"])
        assert False
    except IllegalTransition as e:
        assert "stopped" in str(e)


if __name__ == "__main__":
    failures = 0
    for name, fn in sorted(globals().items()):
        if name.startswith("test_") and callable(fn):
            with tempfile.TemporaryDirectory() as tmp:
                try:
                    fn(Path(tmp))
                    print(f"PASS {name}")
                except Exception as exc:  # noqa: BLE001
                    failures += 1
                    print(f"FAIL {name}: {exc}")
    sys.exit(1 if failures else 0)
