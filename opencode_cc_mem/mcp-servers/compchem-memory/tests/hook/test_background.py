"""Detached probe/critic worker completion semantics."""

import json
import fcntl

from compchem_memory.hook import action_retrieval, background, critic
from compchem_memory.hook.state import SessionState


def test_worker_runs_probe_and_critic_once_then_marks_done(tmp_path, monkeypatch):
    project = tmp_path / "project"
    store = project / ".magnolia"
    store.mkdir(parents=True)
    payload = {
        "root": str(tmp_path), "project_dir": str(project), "store": str(store),
        "name": "project", "root_mode": False, "harness": "vibe",
        "session_id": "s1", "agent_id": None, "turn_id": "t1",
        "reply_text": "final reply", "key": "main--t1",
    }
    job = store / "hook-workers" / "job.json"
    job.parent.mkdir(parents=True)
    job.write_text(json.dumps(payload), encoding="utf-8")
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
    payload = {
        "root": str(tmp_path), "project_dir": str(project), "store": str(store),
        "name": "project", "root_mode": False, "harness": "vibe",
        "session_id": "s1", "agent_id": None, "turn_id": "t1",
        "reply_text": "final reply", "key": "main--t1",
    }
    first = folder / "main--t1-1.json"
    duplicate = folder / "main--t1-2.json"
    first.write_text(json.dumps(payload), encoding="utf-8")
    duplicate.write_text(json.dumps(payload), encoding="utf-8")
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
    payload = {
        "root": str(tmp_path), "project_dir": str(project), "store": str(store),
        "name": "project", "root_mode": False, "harness": "vibe",
        "session_id": "s1", "agent_id": None, "turn_id": "t1",
        "reply_text": "secret-ish reply", "key": "main--t1",
    }
    blocked = folder / "main--t1-2.json"
    blocked.write_text(json.dumps(payload), encoding="utf-8")
    with (folder / "main--t1.lock").open("a+") as held:
        fcntl.flock(held.fileno(), fcntl.LOCK_EX)
        background.run_job(blocked)
    assert not blocked.exists()

    malformed = folder / "malformed.json"
    malformed.write_text("not json", encoding="utf-8")
    background.run_job(malformed)
    assert not malformed.exists()
