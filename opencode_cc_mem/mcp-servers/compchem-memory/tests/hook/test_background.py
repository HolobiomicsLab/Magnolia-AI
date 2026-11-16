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
        "turn_id": "u1",
    }
    job = folder / "opencode-turn.json"
    job.write_text(json.dumps(payload), encoding="utf-8")
    exported = {"messages": [
        {"info": {"role": "user", "id": "u1"}, "parts": [{"type": "text", "text": "check"}]},
        {"info": {"role": "assistant", "id": "a1"}, "parts": [{"type": "text", "text": "done" * 80}]},
    ]}
    from compchem_memory import opencode_ingest

    monkeypatch.setattr(opencode_ingest, "live_session_exporter", lambda: lambda _sid: exported)
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


def _opencode_payload(tmp_path, project, store, session_id, turn_id, operation="critic"):
    return {
        **_payload(tmp_path, project, store),
        "harness": "opencode", "operation": operation,
        "session_id": session_id, "turn_id": turn_id,
    }


def _opencode_job(store, name, payload):
    path = store / "hook-workers" / f"opencode-turn-{name}.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload), encoding="utf-8")
    return path


def _messages(*turns):
    messages = []
    for turn_id, reply in turns:
        messages.append({
            "info": {"role": "user", "id": turn_id},
            "parts": [{"type": "text", "text": f"question {turn_id}"}],
        })
        if reply is not None:
            messages.append({
                "info": {"role": "assistant", "id": f"a-{turn_id}"},
                "parts": [{"type": "text", "text": reply}],
            })
    return messages


def test_late_opencode_payload_judges_its_handed_off_turn(tmp_path, monkeypatch):
    project = tmp_path / "project"
    store = project / ".magnolia"
    live = {"messages": _messages(("u1", "one " * 80), ("u2", None))}
    from compchem_memory import opencode_ingest

    monkeypatch.setattr(opencode_ingest, "live_session_exporter", lambda: lambda _sid: live)
    observed = []

    def write_verdict(paths, harness, session_id, agent_id, turn_id):
        observed.append(turn_id)
        output = paths.store / "claim-critic" / f"{session_id}.jsonl"
        output.parent.mkdir(parents=True, exist_ok=True)
        output.write_text(json.dumps({"harness": harness, "turn_id": turn_id}) + "\n")

    monkeypatch.setattr(critic, "run_critic", write_verdict)
    first = _opencode_job(store, "late", _opencode_payload(
        tmp_path, project, store, "s1", "u1",
    ))
    background.run_opencode_job(first)

    assert observed == ["u1"]
    assert not first.exists()


def test_terminal_event_boundary_resolves_to_its_user_turn(tmp_path, monkeypatch):
    project = tmp_path / "project"
    store = project / ".magnolia"
    exported = {
        "messages": _messages(("u1", "one " * 80), ("u2", "two " * 80)),
        "turn_boundaries": {"msg_terminal_1": "u1", "msg_terminal_2": "u2"},
    }
    from compchem_memory import opencode_ingest

    monkeypatch.setattr(opencode_ingest, "live_session_exporter", lambda: lambda _sid: exported)
    observed = []

    def write_verdict(paths, harness, session_id, agent_id, turn_id):
        observed.append(turn_id)
        output = paths.store / "claim-critic" / f"{session_id}.jsonl"
        output.parent.mkdir(parents=True, exist_ok=True)
        prior = output.read_text() if output.exists() else ""
        output.write_text(prior + json.dumps({"harness": harness, "turn_id": turn_id}) + "\n")

    monkeypatch.setattr(critic, "run_critic", write_verdict)
    job = _opencode_job(store, "boundary", _opencode_payload(
        tmp_path, project, store, "s1", "msg_terminal_1",
    ))
    background.run_opencode_job(job)

    assert observed == ["u1"]


def test_no_database_worker_progresses_to_second_live_turn(tmp_path, monkeypatch):
    project = tmp_path / "project"
    store = project / ".magnolia"
    live = {"messages": _messages(("u1", "one " * 80))}
    from compchem_memory import opencode_ingest

    monkeypatch.delenv("OPENCODE_DB", raising=False)
    monkeypatch.setattr(opencode_ingest, "export_session", lambda _sid: live)
    observed = []

    def write_verdict(paths, harness, session_id, agent_id, turn_id):
        observed.append(turn_id)
        output = paths.store / "claim-critic" / f"{session_id}.jsonl"
        output.parent.mkdir(parents=True, exist_ok=True)
        prior = output.read_text() if output.exists() else ""
        output.write_text(prior + json.dumps({"harness": harness, "turn_id": turn_id}) + "\n")

    monkeypatch.setattr(critic, "run_critic", write_verdict)
    background.run_opencode_job(_opencode_job(store, "t1", _opencode_payload(
        tmp_path, project, store, "s1", "u1",
    )))
    live["messages"] = _messages(("u1", "one " * 80), ("u2", "two " * 80))
    background.run_opencode_job(_opencode_job(store, "t2", _opencode_payload(
        tmp_path, project, store, "s1", "u2",
    )))

    assert observed == ["u1", "u2"]
    dump = json.loads((store / "opencode-transcripts" / "s1.json").read_text())
    assert [row["info"]["id"] for row in dump["messages"] if row["info"]["role"] == "user"] == [
        "u1", "u2",
    ]


def test_later_session_worker_drains_project_payloads(tmp_path, monkeypatch):
    project = tmp_path / "project"
    store = project / ".magnolia"
    exports = {
        "sA": {"messages": _messages(("uA", "alpha " * 80))},
        "sB": {"messages": _messages(("uB", "beta " * 80))},
    }
    from compchem_memory import opencode_ingest

    monkeypatch.setattr(opencode_ingest, "live_session_exporter", lambda: exports.get)
    observed = []

    def write_verdict(paths, harness, session_id, agent_id, turn_id):
        observed.append((session_id, turn_id))
        output = paths.store / "claim-critic" / f"{session_id}.jsonl"
        output.parent.mkdir(parents=True, exist_ok=True)
        output.write_text(json.dumps({"harness": harness, "turn_id": turn_id}) + "\n")

    monkeypatch.setattr(critic, "run_critic", write_verdict)
    stale = _opencode_job(store, "stale", _opencode_payload(
        tmp_path, project, store, "sA", "uA",
    ))
    trigger = _opencode_job(store, "trigger", _opencode_payload(
        tmp_path, project, store, "sB", "uB",
    ))
    background.run_opencode_job(trigger)

    assert observed == [("sA", "uA"), ("sB", "uB")]
    assert not stale.exists() and not trigger.exists()


def test_bad_opencode_payload_does_not_abort_drain(tmp_path, monkeypatch):
    project = tmp_path / "project"
    store = project / ".magnolia"
    bad = _opencode_job(store, "00-bad", {"broken": True})
    good = _opencode_job(store, "01-good", _opencode_payload(
        tmp_path, project, store, "s1", "u1", operation="capture",
    ))
    from compchem_memory import opencode_ingest

    monkeypatch.setattr(
        opencode_ingest, "live_session_exporter",
        lambda: lambda _sid: {"messages": _messages(("u1", "done"))},
    )
    background.run_opencode_job(good)

    assert not good.exists()
    assert not bad.exists()
    assert list((store / "hook-workers" / "rejected").glob("opencode-turn-00-bad*.json"))


def test_failed_payload_quarantine_does_not_abort_drain(tmp_path, monkeypatch):
    project = tmp_path / "project"
    store = project / ".magnolia"
    bad = _opencode_job(store, "00-bad", {"broken": True})
    good = _opencode_job(store, "01-good", _opencode_payload(
        tmp_path, project, store, "s1", "u1", operation="capture",
    ))
    from compchem_memory import opencode_ingest

    monkeypatch.setattr(
        opencode_ingest, "live_session_exporter",
        lambda: lambda _sid: {"messages": _messages(("u1", "done"))},
    )

    def fail_move(*_args):
        raise OSError("simulated quarantine failure")

    monkeypatch.setattr(background, "_move_payload", fail_move)

    background.run_opencode_job(good)

    assert bad.exists()
    assert not good.exists()


def test_expired_opencode_payload_is_moved_aside_with_reason(tmp_path):
    project = tmp_path / "project"
    store = project / ".magnolia"
    payload = _opencode_payload(tmp_path, project, store, "s1", "u1", operation="capture")
    payload["queued_at"] = 1
    job = _opencode_job(store, "expired", payload)

    background.run_opencode_job(job)

    assert not job.exists()
    assert list((store / "hook-workers" / "expired").glob("opencode-turn-expired*.json"))
    log = (store / "hook-workers" / "opencode-turn-errors.jsonl").read_text()
    assert "exceeded 86400 second retry bound" in log
