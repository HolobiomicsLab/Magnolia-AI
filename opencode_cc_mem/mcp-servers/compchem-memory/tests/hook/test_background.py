"""Detached probe/critic worker completion semantics."""

import json
import fcntl
from pathlib import Path

from compchem_memory.hook import action_retrieval, background, critic
from compchem_memory.hook.state import SessionState


def _payload(tmp_path, project, store):
    return {
        "root": str(tmp_path), "project_dir": str(project), "store": str(store),
        "name": "project", "root_mode": False, "harness": "vibe", "session_id": "s1",
    }


def _queue_turn(store):
    with SessionState.open(store, "vibe", "s1") as state:
        state.data["background_queue"] = [{
            "agent_id": None, "turn_id": "t1", "reply_text": "final reply",
            "key": "main--t1", "queued": 1.0,
        }]
        state.data["background_turns"] = {
            "main--t1": {"status": "queued", "attempts": 0},
        }
        state.data["background_worker"] = {"status": "running", "started": 1.0}


def test_worker_runs_probe_and_critic_once_then_marks_done(tmp_path, monkeypatch):
    project = tmp_path / "project"
    store = project / ".magnolia"
    store.mkdir(parents=True)
    payload = _payload(tmp_path, project, store)
    job = store / "hook-workers" / "job.json"
    job.parent.mkdir(parents=True)
    job.write_text(json.dumps(payload), encoding="utf-8")
    _queue_turn(store)
    observed = []
    monkeypatch.setattr(action_retrieval, "probe_application", lambda *args: observed.append("probe"))
    monkeypatch.setattr(critic, "run_critic", lambda *args: observed.append("critic"))

    background.run_job(job)
    background.run_job(job)

    assert observed == ["probe", "critic"]
    with SessionState.open(store, "vibe", "s1") as state:
        assert state.data["background_turns"]["main--t1"]["status"] == "done"


def test_worker_rechecks_done_under_lock_and_removes_duplicate_job(tmp_path, monkeypatch):
    project = tmp_path / "project"
    store = project / ".magnolia"
    folder = store / "hook-workers"
    folder.mkdir(parents=True)
    payload = _payload(tmp_path, project, store)
    first = folder / "main--t1-1.json"
    duplicate = folder / "main--t1-2.json"
    first.write_text(json.dumps(payload), encoding="utf-8")
    duplicate.write_text(json.dumps(payload), encoding="utf-8")
    _queue_turn(store)
    observed = []
    monkeypatch.setattr(action_retrieval, "probe_application", lambda *args: observed.append("probe"))
    monkeypatch.setattr(critic, "run_critic", lambda *args: observed.append("critic"))

    background.run_job(first)
    background.run_job(duplicate)

    assert observed == ["probe", "critic"]
    assert not first.exists() and not duplicate.exists()


def test_blocked_worker_and_malformed_job_are_removed(tmp_path):
    project = tmp_path / "project"
    store = project / ".magnolia"
    folder = store / "hook-workers"
    folder.mkdir(parents=True)
    payload = _payload(tmp_path, project, store)
    blocked = folder / "main--t1-2.json"
    blocked.write_text(json.dumps(payload), encoding="utf-8")
    with (folder / "vibe--s1--worker.lock").open("a+") as held:
        fcntl.flock(held.fileno(), fcntl.LOCK_EX)
        background.run_job(blocked)
    assert not blocked.exists()

    malformed = folder / "malformed.json"
    malformed.write_text("not json", encoding="utf-8")
    background.run_job(malformed)
    assert not malformed.exists()


def test_opencode_worker_finalizes_capture_and_critic_once(tmp_path, monkeypatch):
    project = tmp_path / "project"
    store = project / ".magnolia"
    folder = store / "hook-workers"
    folder.mkdir(parents=True)
    payload = {
        **_payload(tmp_path, project, store),
        "harness": "opencode",
        "operation": "critic",
        "session_id": "s1",
    }
    job = folder / "opencode-turn.json"
    job.write_text(json.dumps(payload), encoding="utf-8")
    exported = {"messages": [
        {"info": {"role": "user", "id": "u1"}, "parts": [{"type": "text", "text": "check"}]},
        {"info": {"role": "assistant", "id": "a1"}, "parts": [{"type": "text", "text": "done" * 80}]},
    ]}
    from compchem_memory import opencode_ingest

    monkeypatch.setattr(opencode_ingest, "plugin_dump_exporter", lambda _store: lambda _sid: exported)
    observed = []

    def write_critic(paths, harness, session_id, agent_id, turn_id):
        observed.append((harness, session_id, turn_id))
        out = paths.store / "claim-critic" / f"{session_id}.jsonl"
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text(json.dumps({"turn_id": turn_id}) + "\n", encoding="utf-8")

    monkeypatch.setattr(critic, "run_critic", write_critic)

    background.run_opencode_job(job)
    duplicate = folder / "opencode-turn-duplicate.json"
    duplicate.write_text(json.dumps(payload), encoding="utf-8")
    background.run_opencode_job(duplicate)

    capture = json.loads((store / "opencode-transcripts" / "s1.json").read_text())
    assert capture["messages"] == exported["messages"]
    assert observed == [("opencode", "s1", "u1")]
    assert len((store / "claim-critic" / "s1.jsonl").read_text().splitlines()) == 1
