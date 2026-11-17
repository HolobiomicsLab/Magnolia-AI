"""Detached probe/critic worker completion semantics."""

import json
import fcntl
import time
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
    (store / "opencode-sessions.jsonl").write_text(
        json.dumps({"opencode_session_id": "s1"}) + "\n", encoding="utf-8",
    )
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
    store.mkdir(parents=True, exist_ok=True)
    mapping = store / "opencode-sessions.jsonl"
    with mapping.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps({"opencode_session_id": session_id}) + "\n")
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


def _write_verdict_outcome(observed):
    def write(paths, harness, session_id, _agent_id, turn_id):
        from compchem_memory.hook.critic import CriticOutcome

        observed.append(turn_id)
        output = paths.store / "claim-critic" / f"{session_id}.jsonl"
        output.parent.mkdir(parents=True, exist_ok=True)
        prior = output.read_text() if output.exists() else ""
        output.write_text(
            prior + json.dumps({"harness": harness, "turn_id": turn_id}) + "\n",
            encoding="utf-8",
        )
        return CriticOutcome.verdict()

    return write


def test_v1_turn_without_event_identity_uses_queued_at_boundary(tmp_path, monkeypatch):
    project = tmp_path / "project"
    store = project / ".magnolia"
    queued_at = time.time()
    exported = {"messages": [
        {"info": {"role": "user", "id": "u1", "time": {"created": queued_at - 50}}, "parts": []},
        {"info": {"role": "assistant", "id": "a1"}, "parts": []},
        {"info": {"role": "user", "id": "u2", "time": {"created": queued_at + 50}}, "parts": []},
    ]}
    from compchem_memory import opencode_ingest

    monkeypatch.setattr(opencode_ingest, "live_session_exporter", lambda: lambda _sid: exported)
    observed = []
    monkeypatch.setattr(critic, "run_critic", _write_verdict_outcome(observed))
    payload = _opencode_payload(tmp_path, project, store, "s1", None)
    payload["queued_at"] = queued_at
    job = _opencode_job(store, "v1", payload)

    background.run_opencode_job(job)

    assert observed == ["u1"]
    assert not job.exists()


def test_v2_durable_sequence_resolves_session_message_boundary(tmp_path, monkeypatch):
    project = tmp_path / "project"
    store = project / ".magnolia"
    exported = {
        "messages": _messages(("u1", "one"), ("u2", "two")),
        "turn_sequences": {"13": "u1", "25": "u2"},
    }
    from compchem_memory import opencode_ingest

    monkeypatch.setattr(opencode_ingest, "live_session_exporter", lambda: lambda _sid: exported)
    observed = []
    monkeypatch.setattr(critic, "run_critic", _write_verdict_outcome(observed))
    payload = _opencode_payload(tmp_path, project, store, "s1", None)
    payload["durable_seq"] = 25
    job = _opencode_job(store, "seq", payload)

    background.run_opencode_job(job)

    assert observed == ["u2"]
    assert not job.exists()


def test_identity_falls_through_sequence_and_event_to_queued_boundary(tmp_path, monkeypatch):
    project = tmp_path / "project"
    store = project / ".magnolia"
    queued_at = time.time()
    exported = {"messages": [
        {"info": {"role": "user", "id": "u1", "time": {"created": queued_at - 5}},
         "parts": []},
        {"info": {"role": "assistant", "id": "a1"}, "parts": []},
    ]}
    from compchem_memory import opencode_ingest

    monkeypatch.setattr(opencode_ingest, "live_session_exporter", lambda: lambda _sid: exported)
    observed = []
    monkeypatch.setattr(critic, "run_critic", _write_verdict_outcome(observed))
    for name, fields in (
        ("seq", {"durable_seq": 25}),
        ("event", {"event_id": "evt_terminal"}),
    ):
        payload = _opencode_payload(tmp_path, project, store, f"s-{name}", None)
        payload.update(fields)
        payload["queued_at"] = queued_at
        background.run_opencode_job(_opencode_job(store, name, payload))

    assert observed == ["u1", "u1"]


def test_capture_completes_without_turn_identity(tmp_path, monkeypatch):
    project = tmp_path / "project"
    store = project / ".magnolia"
    exported = {"messages": _messages(("u1", "captured"))}
    from compchem_memory import opencode_ingest

    monkeypatch.setattr(opencode_ingest, "live_session_exporter", lambda: lambda _sid: exported)
    payload = _opencode_payload(
        tmp_path, project, store, "s1", None, operation="capture",
    )
    payload["durable_seq"] = 99
    job = _opencode_job(store, "capture-no-identity", payload)

    background.run_opencode_job(job)

    assert not job.exists()
    assert (store / "opencode-transcripts" / "s1.json").exists()
    assert not (store / "hook-workers" / "rejected").exists()


def test_unresolvable_turn_is_logged_and_rejected_once(tmp_path, monkeypatch):
    project = tmp_path / "project"
    store = project / ".magnolia"
    exported = {"messages": _messages(("u1", "one"))}
    from compchem_memory import opencode_ingest

    exports = {"count": 0}

    def export(_sid):
        exports["count"] += 1
        return exported

    monkeypatch.setattr(opencode_ingest, "live_session_exporter", lambda: export)
    payload = _opencode_payload(tmp_path, project, store, "s1", "evt_unknown")
    job = _opencode_job(store, "unknown", payload)

    background.run_opencode_job(job)
    background.drain_opencode_payloads(store)

    assert exports["count"] == 1
    assert not job.exists()
    assert list((store / "hook-workers" / "rejected").glob("opencode-turn-unknown*.json"))
    error = (store / "hook-workers" / "opencode-turn-errors.jsonl").read_text()
    assert "unresolvable turn identity" in error
    assert "opencode-turn-unknown.json" in error


def test_skipped_short_critic_completes_without_judge_call(tmp_path, monkeypatch):
    project = tmp_path / "project"
    store = project / ".magnolia"
    exported = {"messages": _messages(("u1", "Done."))}
    from compchem_memory import opencode_ingest

    monkeypatch.setenv("MAGNOLIA_CRITIC", "1")
    monkeypatch.setattr(opencode_ingest, "live_session_exporter", lambda: lambda _sid: exported)
    judge_calls = []
    monkeypatch.setattr(critic, "_judge", lambda *_args: judge_calls.append(True))
    job = _opencode_job(store, "short", _opencode_payload(
        tmp_path, project, store, "s1", "u1",
    ))

    background.run_opencode_job(job)
    for index in range(5):
        background.run_opencode_job(_opencode_job(
            store, f"later-{index}",
            _opencode_payload(tmp_path, project, store, "s1", "u1", operation="capture"),
        ))

    assert judge_calls == []
    assert not job.exists()
    with SessionState.open(store, "opencode", "s1") as state:
        row = state.data["opencode_turns"]["critic--u1"]
        assert row["status"] == "done"
        assert row["reason"] == "short reply"


def test_critic_error_retries_are_capped_per_payload(tmp_path, monkeypatch):
    project = tmp_path / "project"
    store = project / ".magnolia"
    exported = {"messages": _messages(("u1", "claim " * 80))}
    from compchem_memory import opencode_ingest
    from compchem_memory.llm import CallResult, Route

    monkeypatch.setenv("MAGNOLIA_CRITIC", "1")
    monkeypatch.setattr(opencode_ingest, "live_session_exporter", lambda: lambda _sid: exported)
    monkeypatch.setattr(
        critic, "resolve_route",
        lambda _role: Route("openai", "judge", "https://invalid", "key", "openai"),
    )
    judge_calls = []

    def fail_judge(*_args):
        judge_calls.append(True)
        return CallResult("", "error", "judge", "HTTP 500", 500)

    monkeypatch.setattr(critic, "_judge", fail_judge)
    job = _opencode_job(store, "error", _opencode_payload(
        tmp_path, project, store, "s1", "u1",
    ))

    background.run_opencode_job(job)
    for index in range(5):
        background.run_opencode_job(_opencode_job(
            store, f"later-{index}",
            _opencode_payload(tmp_path, project, store, "s1", "u1", operation="capture"),
        ))

    assert len(judge_calls) == background._ATTEMPT_LIMIT
    assert not job.exists()
    expired = list((store / "hook-workers" / "expired").glob("opencode-turn-error*.json"))
    assert len(expired) == 1
    saved = json.loads(expired[0].read_text())
    assert saved["attempts"] == background._ATTEMPT_LIMIT
    assert "HTTP 500" in saved["last_error"]


def test_payload_is_reread_under_lock_before_attempt_is_charged(tmp_path, monkeypatch):
    project = tmp_path / "project"
    store = project / ".magnolia"
    exported = {"messages": _messages(("u1", "claim " * 80))}
    from compchem_memory import opencode_ingest
    from compchem_memory.hook.critic import CriticOutcome

    monkeypatch.setattr(opencode_ingest, "live_session_exporter", lambda: lambda _sid: exported)
    calls = []
    monkeypatch.setattr(
        critic, "run_critic",
        lambda *_args: calls.append(True) or CriticOutcome.error("HTTP 500"),
    )
    job = _opencode_job(store, "stale", _opencode_payload(
        tmp_path, project, store, "s1", "u1",
    ))
    real_read = background._read_opencode_payload
    nested = {"count": 0}

    def read_then_interleave(path, current_store):
        payload = real_read(path, current_store)
        if path == job and nested["count"] < background._ATTEMPT_LIMIT:
            nested["count"] += 1
            background.drain_opencode_payloads(store)
        return payload

    monkeypatch.setattr(background, "_read_opencode_payload", read_then_interleave)
    for _ in range(background._ATTEMPT_LIMIT):
        background.drain_opencode_payloads(store)

    expired = list((store / "hook-workers" / "expired").glob("opencode-turn-stale*.json"))
    assert len(calls) <= background._ATTEMPT_LIMIT
    assert not job.exists()
    assert len(expired) == 1
    assert json.loads(expired[0].read_text())["attempts"] == background._ATTEMPT_LIMIT


def test_erroring_lock_holder_rescans_payload_that_skipped_its_lock(tmp_path, monkeypatch):
    project = tmp_path / "project"
    store = project / ".magnolia"
    exported = {"messages": _messages(("u1", "claim " * 80))}
    from compchem_memory import opencode_ingest
    from compchem_memory.hook.critic import CriticOutcome

    monkeypatch.setattr(opencode_ingest, "live_session_exporter", lambda: lambda _sid: exported)
    monkeypatch.setattr(critic, "run_critic", lambda *_args: CriticOutcome.error("HTTP 500"))
    old = _opencode_job(store, "0-old", _opencode_payload(
        tmp_path, project, store, "s1", "u1",
    ))
    created = {}
    real_run = background._run_opencode_payload

    def run_and_interleave(path, payload, *args, **kwargs):
        if path == old and not created:
            created["job"] = _opencode_job(store, "1-new", _opencode_payload(
                tmp_path, project, store, "s1", None, operation="capture",
            ))
            background.drain_opencode_payloads(store)
        return real_run(path, payload, *args, **kwargs)

    monkeypatch.setattr(background, "_run_opencode_payload", run_and_interleave)
    background.drain_opencode_payloads(store)

    assert old.exists()
    assert not created["job"].exists()


def test_no_route_override_records_typed_skip(tmp_path, monkeypatch):
    project = tmp_path / "project"
    store = project / ".magnolia"
    exported = {"messages": _messages(("u1", "claim " * 80))}
    from compchem_memory import opencode_ingest

    monkeypatch.setattr(opencode_ingest, "live_session_exporter", lambda: lambda _sid: exported)
    job = _opencode_job(store, "no-route", _opencode_payload(
        tmp_path, project, store, "s1", "u1",
    ))

    background.drain_opencode_payloads(store, critic_skip_reason="no route")

    assert not job.exists()
    with SessionState.open(store, "opencode", "s1") as state:
        row = state.data["opencode_turns"]["critic--u1"]
    assert row["status"] == "done"
    assert row["reason"] == "no route"


def test_drain_rescans_payload_skipped_behind_session_lock(tmp_path, monkeypatch):
    project = tmp_path / "project"
    store = project / ".magnolia"
    exports = {
        "sA": {"messages": _messages(("uA", "alpha"))},
        "sB": {"messages": _messages(("uB", "beta"))},
    }
    from compchem_memory import opencode_ingest

    monkeypatch.setattr(opencode_ingest, "live_session_exporter", lambda: exports.get)
    blocked = _opencode_job(store, "00-a", _opencode_payload(
        tmp_path, project, store, "sA", "uA", operation="capture",
    ))
    trigger = _opencode_job(store, "01-b", _opencode_payload(
        tmp_path, project, store, "sB", "uB", operation="capture",
    ))
    lock_path = background._worker_lock(store, "opencode", "sA")
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    held = lock_path.open("a+")
    fcntl.flock(held.fileno(), fcntl.LOCK_EX)
    original = background._run_opencode_payload

    def run_and_release(path, payload):
        result = original(path, payload)
        if payload["session_id"] == "sB":
            fcntl.flock(held.fileno(), fcntl.LOCK_UN)
            held.close()
        return result

    monkeypatch.setattr(background, "_run_opencode_payload", run_and_release)

    background.run_opencode_job(trigger)

    assert not blocked.exists()
    assert not trigger.exists()
