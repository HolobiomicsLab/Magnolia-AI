"""Detached probe/critic worker completion semantics."""

import json

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
