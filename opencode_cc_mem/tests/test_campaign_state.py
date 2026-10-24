import copy
import hashlib
import json
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from campaign_runner import CampaignBusyError, CampaignStore, StateError  # noqa: E402


def protocol_payload():
    return {
        "schema": 1,
        "id": "state-pilot",
        "project": "hsc70_new",
        "worker": {
            "model": "opencode-go/deepseek-v4.1-flash",
            "agent": "campaign-worker",
        },
        "reviewer": {"enabled": False},
        "budget": {
            "max_tokens": 1000,
            "max_wall_hours": 1,
            "max_core_hours": 1,
            "max_attempts_per_stage": 2,
            "max_parallel_jobs": 1,
            "max_followups": 0,
        },
        "stages": [
            {"id": "confirm", "type": "human"},
            {
                "id": "prepare",
                "type": "script",
                "command": ["/bin/true"],
                "gate": ["/bin/true"],
            },
            {
                "id": "analyze",
                "type": "opencode",
                "prompt": "prompts/analyze.md",
                "gate": ["/bin/true"],
            },
        ],
    }


def make_store(tmp_path):
    protocol = tmp_path / "protocol.json"
    protocol.write_text(json.dumps(protocol_payload()), encoding="utf-8")
    brief = tmp_path / "brief.md"
    brief.write_text("# Approved brief\n\nDo the fixed protocol.\n", encoding="utf-8")
    store = CampaignStore(tmp_path / "run")
    state = store.initialize(protocol, brief)
    return store, state, protocol, brief


def test_initialize_creates_campaign_and_journal(tmp_path):
    store, state, protocol, brief = make_store(tmp_path)
    assert state.state == "queued"
    assert state.stage == "confirm"
    assert state.attempt == 0
    assert store.campaign_path.is_file()
    assert store.protocol_path.is_file()
    assert store.brief_path.is_file()
    assert store.jobs_path.is_file()
    assert store.work_dir.is_dir()
    assert store.attempts_dir.is_dir()
    assert state.brief_sha256 == hashlib.sha256(brief.read_bytes()).hexdigest()
    events = store.read_events()
    assert len(events) == 1
    assert events[0]["to"] == "queued"
    assert store.verify_protocol().id == "state-pilot"


def test_initialize_twice_is_refused(tmp_path):
    store, _, protocol, brief = make_store(tmp_path)
    with pytest.raises(StateError, match="already initialized"):
        store.initialize(protocol, brief)


def test_transition_updates_state_and_journal(tmp_path):
    store, _, _, _ = make_store(tmp_path)
    state = store.transition("running", "start stage")
    assert state.state == "running"
    assert state.stage == "confirm"
    events = store.read_events()
    assert len(events) == 2
    assert events[-1]["from"] == "queued"
    assert events[-1]["to"] == "running"
    assert events[-1]["reason"] == "start stage"


def test_invalid_transition_is_refused(tmp_path):
    store, _, _, _ = make_store(tmp_path)
    with pytest.raises(StateError, match="not allowed"):
        store.transition("completed", "skip everything")


def test_human_gate_requires_approve(tmp_path):
    store, _, _, _ = make_store(tmp_path)
    store.transition("running", "start")
    store.transition("waiting_human", "need decision")
    with pytest.raises(StateError, match="use approve"):
        store.transition("queued", "bypass")
    state = store.approve("tao", "decision accepted")
    assert state.state == "queued"
    approvals = list((store.control_dir / "approvals").glob("*.json"))
    assert len(approvals) == 1
    record = json.loads(approvals[0].read_text())
    assert record["approver"] == "tao"
    assert record["reason"] == "decision accepted"
    assert record["stage"] == "confirm"


def test_approve_requires_waiting_human(tmp_path):
    store, _, _, _ = make_store(tmp_path)
    with pytest.raises(StateError, match="not waiting"):
        store.approve("tao", "too early")


def test_pause_and_resume(tmp_path):
    store, _, _, _ = make_store(tmp_path)
    paused = store.pause("stop for now")
    assert paused.state == "paused"
    resumed = store.resume("continue")
    assert resumed.state == "queued"


def test_waiting_human_cannot_be_paused(tmp_path):
    store, _, _, _ = make_store(tmp_path)
    store.transition("running", "start")
    store.transition("waiting_human", "need decision")
    with pytest.raises(StateError, match="cannot pause"):
        store.pause()


def test_halt_and_terminal_state(tmp_path):
    store, _, _, _ = make_store(tmp_path)
    halted = store.halt("operator stop")
    assert halted.state == "halted"
    with pytest.raises(StateError, match="already terminal"):
        store.halt()


def test_lock_is_single_instance(tmp_path):
    store, _, _, _ = make_store(tmp_path)
    with store.lock():
        with pytest.raises(CampaignBusyError):
            with store.lock():
                pass


def test_reconcile_repairs_stale_state_file(tmp_path):
    store, initial, _, _ = make_store(tmp_path)
    store.transition("running", "start")
    stale = copy.deepcopy(initial)
    store.save(stale)
    repaired = store.reconcile()
    assert repaired.state == "running"


def test_reconcile_reconstructs_missing_state_file(tmp_path):
    store, _, _, _ = make_store(tmp_path)
    store.transition("running", "start")
    store.campaign_path.unlink()
    repaired = store.reconcile()
    assert repaired.state == "running"
    assert repaired.protocol_id == "state-pilot"
    assert store.load().state == "running"


def test_corrupt_middle_event_is_rejected(tmp_path):
    store, _, _, _ = make_store(tmp_path)
    store.transition("running", "start")
    lines = store.events_path.read_text().splitlines()
    lines.insert(1, "{broken")
    store.events_path.write_text("\n".join(lines) + "\n")
    with pytest.raises(StateError, match="invalid event"):
        store.read_events()


def test_tampered_protocol_copy_is_refused(tmp_path):
    store, _, _, _ = make_store(tmp_path)
    body = store.protocol_path.read_text()
    store.protocol_path.write_text(body.replace("state-pilot", "evil-pilot"))
    with pytest.raises(StateError):
        store.load()


def test_missing_brief_is_refused(tmp_path):
    protocol = tmp_path / "protocol.json"
    protocol.write_text(json.dumps(protocol_payload()), encoding="utf-8")
    store = CampaignStore(tmp_path / "run")
    with pytest.raises(StateError, match="brief does not exist"):
        store.initialize(protocol, tmp_path / "missing.md")
