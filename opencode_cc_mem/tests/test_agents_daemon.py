"""Tests for the magnolia-agents-daemon (mailbox watcher for helper agents).

The daemon script is a standalone python3 file; tests import it by path.
All runner calls are stubbed — no LLM, no opencode, no network."""
import importlib.util
import os
import json
from importlib.machinery import SourceFileLoader
from pathlib import Path

import pytest

from campaign_runner.executors.contracts import (
    LimitInfo,
    ResumeArtifact,
    RunOutcome,
    SessionIdentity,
)
from campaign_runner.executors.registry import ExecutorRefused
from campaign_runner.workers import WorkerResult

_SCRIPT = Path(__file__).resolve().parents[1] / "softwares" / "bin" / "magnolia-agents-daemon"
_spec = importlib.util.spec_from_file_location(
    "agents_daemon", _SCRIPT, loader=SourceFileLoader("agents_daemon", str(_SCRIPT)))
d = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(d)


def _root(tmp_path):
    root = tmp_path / "mroot"
    root.mkdir(parents=True, exist_ok=True)
    for lane in ("literature", "xiulian"):
        project = root / "opencode_cc_mem" / "projects" / lane
        (project / "inbox").mkdir(parents=True, exist_ok=True)
        (project / "agent.json").write_text(
            json.dumps({"enabled": True}), encoding="utf-8"
        )
    return str(root)


def _task(inbox: Path, name: str = "20260924_ask.task.md", status: str = "open",
          body: str = "Summarize the paper.") -> Path:
    if not inbox.name.startswith("from-"):
        inbox = inbox / "from-xiulian"
    inbox.mkdir(parents=True, exist_ok=True)
    f = inbox / name
    f.write_text(f"# Task: ask\n- from: xiulian\n- date: 2026-09-24\n"
                 f"- status: {status}\n\n## Request\n{body}\n", encoding="utf-8")
    return f


def test_discover_finds_only_open_tasks(tmp_path):
    root = _root(tmp_path)
    inbox = Path(root) / "opencode_cc_mem" / "projects" / "literature" / "inbox" / "from-xiulian"
    open_t = _task(inbox, "a_open.task.md", status="open")
    _task(inbox, "b_done.task.md", status="done")
    _task(inbox, "c_not_a_task.md", status="open")       # wrong suffix
    (inbox / "d_free_letter.md").write_text("# Letter\nstatus: open\n")
    found = d.discover_tasks(root)
    assert found == [open_t]


def test_handle_task_runs_runner_writes_reply_and_marks_done(tmp_path):
    root = _root(tmp_path)
    inbox = Path(root) / "opencode_cc_mem" / "projects" / "literature" / "inbox"
    task = _task(inbox)
    calls = []

    def fake_runner(agent, project_dir, root_, prompt, timeout_s):
        calls.append((agent, project_dir, prompt, timeout_s))
        return "ACK: work complete"

    status = d.handle_task(task, "literature", root, runner=fake_runner)
    assert status == "done"
    # runner got the right project and a self-contained prompt
    agent, project_dir, prompt, timeout_s = calls[0]
    assert agent == "literature"
    assert project_dir.endswith("projects/literature")
    assert "Summarize the paper." in prompt
    assert "Do NOT try to write" in prompt or "do NOT try to write" in prompt
    assert timeout_s == d.TASK_TIMEOUT_S
    # reply written beside the letter
    reply = task.with_name("20260924_ask_reply.md")
    assert "ACK: work complete" in reply.read_text()
    # letter status flipped
    assert "status: done" in task.read_text()
    # state recorded -> not rediscovered
    assert d.discover_tasks(root) == []


def test_edited_letter_reopens(tmp_path):
    root = _root(tmp_path)
    inbox = Path(root) / "opencode_cc_mem" / "projects" / "literature" / "inbox"
    task = _task(inbox)
    d.handle_task(task, "literature", root, runner=lambda *a: "ok")
    assert d.discover_tasks(root) == []
    task.write_text(task.read_text().replace("status: done", "status: open") + "\nEdited.\n")
    found = d.discover_tasks(root)
    assert found == [task]


def test_handle_task_failure_marks_error_and_reports(tmp_path):
    root = _root(tmp_path)
    inbox = Path(root) / "opencode_cc_mem" / "projects" / "literature" / "inbox"
    task = _task(inbox)

    def boom(*a, **k):
        raise RuntimeError("opencode exploded")

    status = d.handle_task(task, "literature", root, runner=boom)
    assert status == "error"
    assert "status: error" in task.read_text()
    reply = task.with_name("20260924_ask_reply.md")
    assert "opencode exploded" in reply.read_text()
    assert d.discover_tasks(root) == []  # consumed, no retry loop


def test_scan_once_routes_by_project(tmp_path):
    root = _root(tmp_path)
    lit_inbox = Path(root) / "opencode_cc_mem" / "projects" / "literature" / "inbox"
    xiu_inbox = Path(root) / "opencode_cc_mem" / "projects" / "xiulian" / "inbox"
    _task(lit_inbox, "l1.task.md")
    _task(xiu_inbox, "x1.task.md")
    seen = []

    def fake_runner(agent, project_dir, root_, prompt, timeout_s):
        seen.append(agent)
        return "ok"

    results = d.scan_once(root, runner=fake_runner)
    assert sorted(seen) == ["literature", "xiulian"]
    assert sorted(results) == ["literature:done", "xiulian:done"]


def test_missing_inbox_is_quiet(tmp_path):
    assert d.discover_tasks(_root(tmp_path)) == []


def test_lock_is_single_instance(tmp_path):
    root = _root(tmp_path)
    fd = d.acquire_lock(root)
    assert fd is not None
    try:
        assert d.acquire_lock(root) is None  # second acquisition refused
    finally:
        os.close(fd)


# ------------------------------------------------ answer gate + resume ---

def _evt(t, sid="ses_x", mid="msg_1", text=None):
    part = {"type": t}
    if t == "text":
        part.update(messageID=mid, text=text)
    elif t == "tool":
        part.update(tool="run_shell")
    return json.dumps({"type": t, "sessionID": sid, "part": part})


def test_parse_run_events_groups_by_message():
    out = "\n".join([
        _evt("step_start"),
        _evt("text", mid="m1", text="Let me look."),
        _evt("tool"),
        _evt("text", mid="m2", text="FINAL ANSWER: 42"),
    ])
    p = d.parse_run_events(out)
    assert p["session_id"] == "ses_x"
    assert p["final_text"] == "FINAL ANSWER: 42"      # LAST message only
    assert "Let me look." in p["all_text"]            # narration kept
    assert p["n_tools"] == 1


def test_is_final_answer_marker():
    assert d.is_final_answer("FINAL ANSWER: yes")
    assert d.is_final_answer("# FINAL ANSWER:\nthe answer")
    assert not d.is_final_answer("Still investigating, let me check...")
    assert not d.is_final_answer("")


def _parsed(final, sid="ses_1", tools=3, rc=0):
    return {"session_id": sid, "final_text": final,
            "all_text": final, "n_tools": tools, "rc": rc,
            "stderr_tail": ""}


def test_runner_returns_on_marker_first_pass(tmp_path, monkeypatch):
    root = _root(tmp_path)
    calls = []

    def fake_once(model, project_dir, env, timeout_s, extra_args, run_log, **kwargs):
        calls.append(extra_args)
        if run_log is not None:
            run_log.write_text("{}\n", encoding="utf-8")
        return _parsed("FINAL ANSWER: ok")

    monkeypatch.setattr(d, "_run_once", fake_once)
    out = d.default_runner("literature", "/tmp/x", root, "PROMPT", 60)
    assert out == "FINAL ANSWER: ok"
    assert calls == [["PROMPT"]]                      # no resume needed
    assert list((d.state_dir(root) / "runs").glob("*.jsonl"))  # log kept


def test_runner_resumes_same_session_then_succeeds(tmp_path, monkeypatch):
    root = _root(tmp_path)
    calls = []
    responses = [_parsed("Still looking at the KBs..."),
                 _parsed("FINAL ANSWER: done", tools=0)]

    def fake_once(model, project_dir, env, timeout_s, extra_args, run_log, **kwargs):
        calls.append(extra_args)
        return responses[len(calls) - 1]

    monkeypatch.setattr(d, "_run_once", fake_once)
    out = d.default_runner("literature", "/tmp/x", root, "PROMPT", 60)
    assert out == "FINAL ANSWER: done"
    assert calls[1][:3] == ["--session", "ses_1", d.RESUME_NOTE]


def test_runner_gives_up_after_max_attempts(tmp_path, monkeypatch):
    root = _root(tmp_path)
    calls = []

    def fake_once(model, project_dir, env, timeout_s, extra_args, run_log, **kwargs):
        calls.append(extra_args)
        return _parsed("narration only, no answer")

    monkeypatch.setattr(d, "_run_once", fake_once)
    with pytest.raises(RuntimeError) as ei:
        d.default_runner("literature", "/tmp/x", root, "PROMPT", 60)
    assert "FINAL ANSWER" in str(ei.value)            # says what was missing
    assert "narration only" in str(ei.value)          # carries the narration
    assert len(calls) == d.RESUME_MAX + 1             # 1 original + N resumes


def test_runner_raises_on_nonzero_rc(tmp_path, monkeypatch):
    root = _root(tmp_path)

    def fake_once(model, project_dir, env, timeout_s, extra_args, run_log, **kwargs):
        return _parsed("", sid=None, rc=2).copy() | {"stderr_tail": "boom"}

    monkeypatch.setattr(d, "_run_once", fake_once)
    with pytest.raises(RuntimeError, match="exit=2"):
        d.default_runner("literature", "/tmp/x", root, "P", 60)


def test_handle_task_prompt_has_answer_contract(tmp_path):
    root = _root(tmp_path)
    inbox = Path(root) / "opencode_cc_mem" / "projects" / "literature" / "inbox"
    task = _task(inbox)
    prompts = []

    def fake_runner(agent, project_dir, root_, prompt, timeout_s):
        prompts.append(prompt)
        return "FINAL ANSWER: ok"

    d.handle_task(task, "literature", root, runner=fake_runner)
    assert "FINAL ANSWER" in prompts[0]
    assert "never end your turn without it" in prompts[0]


def test_handle_task_overwrites_stale_reply(tmp_path):
    root = _root(tmp_path)
    inbox = Path(root) / "opencode_cc_mem" / "projects" / "literature" / "inbox"
    task = _task(inbox)
    reply = task.with_name("20260924_ask_reply.md")
    reply.write_text("# Reply\nSTALE OLD CONTENT", encoding="utf-8")

    status = d.handle_task(task, "literature", root,
                           runner=lambda *a: "FINAL ANSWER: fresh")
    assert status == "done"
    body = reply.read_text(encoding="utf-8")
    assert "fresh" in body
    assert "STALE" not in body


def test_helper_recipe_final_answer_continuation_and_stale_recovery(tmp_path, monkeypatch):
    root = _root(tmp_path)
    original_run_once = d._run_once
    state = {"done": {}, "lanes": {"literature": {"session_id": "ses_stale"}}}
    d.save_state(root, state)
    calls = []
    replies = [
        _parsed("", sid=None, rc=2).copy() | {"stderr_tail": "stale session"},
        _parsed("still working", sid="ses_fresh"),
        _parsed("FINAL ANSWER: complete", sid="ses_fresh"),
    ]

    def fake_once(model, project_dir, env, timeout_s, extra_args, run_log, **kwargs):
        calls.append((model, list(extra_args), sorted(env), kwargs["binary"]))
        return replies[len(calls) - 1]

    monkeypatch.setattr(d, "_run_once", fake_once)
    monkeypatch.setattr(d, "_render_project_config", lambda *args: None)
    monkeypatch.setattr(d, "_ensure_tools_daemon", lambda *args: None)
    output = d.default_runner("literature", "/project", root, "PROMPT", 60)

    assert output == "FINAL ANSWER: complete"
    assert calls[0][1] == ["--session", "ses_stale", "PROMPT"]
    assert calls[1][1] == ["PROMPT"]
    assert calls[2][1] == ["--session", "ses_fresh", d.RESUME_NOTE]
    assert d.load_state(root)["lanes"]["literature"]["session_id"] == "ses_fresh"

    captured = {}

    class Out:
        returncode = 0
        stdout = _evt("text", sid="ses_fresh", text="FINAL ANSWER: complete")
        stderr = ""

    def fake_subprocess_run(argv, **kwargs):
        captured["argv"] = argv
        captured["env"] = kwargs["env"]
        captured["timeout_seconds"] = kwargs["timeout"]
        return Out()

    monkeypatch.setattr(d.subprocess, "run", fake_subprocess_run)
    original_run_once(
        "provider/model", "/project", {"BASE_ENV": "ignored"}, 60,
        ["PROMPT"], None, binary=str(tmp_path / "opencode"),
    )
    snapshot = json.loads(
        (Path(__file__).resolve().parent / "snapshots" / "opencode" / "helper.json").read_text()
    )
    observed = {
        "argv": [item.replace(str(tmp_path), "@@TMP@@") for item in captured["argv"]],
        "environment_keys": sorted(captured["env"]),
        "step_cap_config": captured["env"].get("OPENCODE_CONFIG_CONTENT"),
        "timeout_seconds": captured["timeout_seconds"],
        "agent_file_bytes": None,
    }
    assert observed == snapshot


# ---------------------------------------------------------------------------
# Registry, wait_for, scheduled sweeps (2026-10-07)
# ---------------------------------------------------------------------------


def _lane(root, name, cfg=None):
    """Create a lane: project dir + inbox + optional agent.json."""
    proj = Path(root) / "opencode_cc_mem" / "projects" / name
    (proj / "inbox").mkdir(parents=True, exist_ok=True)
    if cfg is not None:
        (proj / "agent.json").write_text(json.dumps(cfg), encoding="utf-8")
    return proj


def test_registry_unknown_project_becomes_lane(tmp_path):
    root = _root(tmp_path)
    inbox = _lane(root, "perspicacite", {"enabled": True}) / "inbox"
    _task(inbox, "x.task.md")
    lanes = d.discover_lanes(root)
    assert "perspicacite" in lanes            # registry lane
    assert "literature" in lanes and "xiulian" in lanes   # defaults stay
    found = d.discover_tasks(root, lanes)
    assert any("perspicacite" in str(f) for f in found)


def test_registry_disabled_lane_removed(tmp_path):
    root = _root(tmp_path)
    _lane(root, "xiulian", {"enabled": False})
    lanes = d.discover_lanes(root)
    assert "xiulian" not in lanes and "literature" in lanes


def test_unregistered_project_not_discovered(tmp_path):
    root = _root(tmp_path)
    inbox = _lane(root, "nobody") / "inbox"     # no agent.json
    _task(inbox, "x.task.md")
    lanes = d.discover_lanes(root)
    found = d.discover_tasks(root, lanes)
    assert not any("nobody" in str(f) for f in found)


def test_wait_for_defers_until_run_record_lands(tmp_path):
    root = _root(tmp_path)
    proj = _lane(root, "xiulian", {"enabled": True})
    t = _task(proj / "inbox", "idea.task.md", status="open",
              body="Prototype the idea.\nwait_for: job:20261007_arm3")
    lanes = d.discover_lanes(root)

    # parked: discovery skips it
    assert not d.discover_tasks(root, lanes)

    # job record lands -> the delivery pass re-opens the letter
    runs = proj / ".magnolia" / "runs"
    runs.mkdir(parents=True)
    (runs / "20261007_arm3.yaml").write_text("status: success\n", encoding="utf-8")
    n = d.deliver_wait_for(root, lanes)
    assert n == 1
    text = t.read_text(encoding="utf-8")
    import re as _re
    assert _re.search(r"^wait_for:", text, _re.M) is None  # parked line gone
    assert "Job result" in text and "success" in text
    # now discoverable again
    assert d.discover_tasks(root, lanes)


def test_wait_for_matches_date_prefixed_run_records(tmp_path):
    """Real records are written as YYYYMMDD_<run_id>.yaml (submit_job naming);
    the exact-name lookup alone never matches them (found 2026-10-08 on the
    first live wait_for)."""
    root = _root(tmp_path)
    proj = _lane(root, "xiulian", {"enabled": True})
    t = _task(proj / "inbox", "idea2.task.md", status="open",
              body="Prototype the idea.\nwait_for: job:replay-eval_20261008_112903_12cd20")
    lanes = d.discover_lanes(root)
    runs = proj / ".magnolia" / "runs"
    runs.mkdir(parents=True)
    (runs / "20261008_replay-eval_20261008_112903_12cd20.yaml").write_text(
        "status: success\n", encoding="utf-8")
    n = d.deliver_wait_for(root, lanes)
    assert n == 1
    text = t.read_text(encoding="utf-8")
    assert "Job result" in text and "success" in text
    assert d.discover_tasks(root, lanes)


# ---- status subsystem rewrite (panel verdict #1: value-agnostic, header-only)


def test_set_status_rewrites_any_value_and_get_status_reads_header():
    letter = "---\nstatus: prototyped\nfrom: literature\n---\n\nbody\n"
    assert d.get_status(letter) == "prototyped"
    out = d.set_status(letter, "open")
    assert "status: open" in out and "prototyped" not in out
    assert d.is_open_task(out) is True
    assert d.is_open_task(letter) is False          # prototyped is not runnable


def test_body_status_line_never_reopens_edited_letter(tmp_path):
    """A body line `status: open` must not count: the old matcher re-ran such
    letters after any later edit (zombie reprocess)."""
    root = _root(tmp_path)
    inbox = Path(root) / "opencode_cc_mem" / "projects" / "literature" / "inbox"
    task = _task(inbox, body="example header:\n\n- status: open\n")
    def fake_runner(agent, project_dir, root_, prompt, timeout_s):
        return "FINAL ANSWER: ok"
    assert d.handle_task(task, "literature", root, runner=fake_runner) == "done"
    # simulate a later edit of the consumed letter
    task.write_text(task.read_text() + "\nfollow-up note\n", encoding="utf-8")
    assert d.discover_tasks(root) == []             # no zombie reprocess


def test_deferred_rid_validation():
    ok = "wait_for: job:replay-eval_20261008_121845_7db43d"
    assert d.is_deferred_task(ok) == "replay-eval_20261008_121845_7db43d"
    assert d.is_deferred_task("wait_for: job:../../etc/passwd") is None
    assert d.is_deferred_task("wait_for: job:*") is None


def test_wait_for_ignores_non_terminal_run_records(tmp_path):
    """submit_job writes the record at submission; a `running` record must not
    burn the single hop (panel verdict #2)."""
    root = _root(tmp_path)
    proj = _lane(root, "xiulian", {"enabled": True})
    _task(proj / "inbox", "idea3.task.md", status="open",
          body="Prototype.\nwait_for: job:arm_x")
    lanes = d.discover_lanes(root)
    runs = proj / ".magnolia" / "runs"
    runs.mkdir(parents=True)
    (runs / "arm_x.yaml").write_text("status: running\n", encoding="utf-8")
    assert d.deliver_wait_for(root, lanes) == 0
    (runs / "arm_x.yaml").write_text("status: success\n", encoding="utf-8")
    assert d.deliver_wait_for(root, lanes) == 1


# ---- ticket trailer: the lane decides, the daemon writes (verdict seam)


def _ticket(inbox: Path, name: str, status: str = "open") -> Path:
    if not inbox.name.startswith("from-"):
        inbox = inbox / "from-literature"
    inbox.mkdir(parents=True, exist_ok=True)
    f = inbox / name
    f.write_text(f"---\nstatus: {status}\nfrom: literature\nto: xiulian\n"
                 f"ticket: idea\n---\n\n# Idea ticket\n\n## Idea\nDo a thing.\n",
                 encoding="utf-8")
    return f


def test_ticket_trailer_parks_on_run(tmp_path):
    root = _root(tmp_path)
    proj = _lane(root, "xiulian", {"enabled": True})
    ticket = _ticket(proj / "inbox", "idea_park.task.md")

    def builder(agent, project_dir, root_, prompt, timeout_s):
        return ("FINAL ANSWER: prototype built.\n"
                "ticket-status: prototyped\n"
                "park: job:replay-eval_20261008_121845_7db43d\n")

    result = d.handle_task(ticket, "xiulian", root, runner=builder)
    assert result == "prototyped"
    text = ticket.read_text(encoding="utf-8")
    assert "status: prototyped" in text and "status: done" not in text
    assert "wait_for: job:replay-eval_20261008_121845_7db43d" in text
    # not consumed: parked, then delivered once the record is terminal
    assert d.discover_tasks(root) == []             # parked letters never auto-run
    runs = proj / ".magnolia" / "runs"
    runs.mkdir(parents=True, exist_ok=True)
    (runs / "20261008_replay-eval_20261008_121845_7db43d.yaml").write_text(
        "status: success\n", encoding="utf-8")
    assert d.deliver_wait_for(root, d.discover_lanes(root)) == 1
    text = ticket.read_text(encoding="utf-8")
    assert "Job result" in text and d.get_status(text) == "open"


def test_ticket_trailer_evaluated_closes_lifecycle(tmp_path):
    """Full evaluation hop: parked ticket -> terminal record -> daemon
    re-delivers with the result -> lane replies trailer -> ticket evaluated."""
    root = _root(tmp_path)
    proj = _lane(root, "xiulian", {"enabled": True})
    ticket = _ticket(proj / "inbox", "idea_eval.task.md", status="open")
    runs = proj / ".magnolia" / "runs"
    runs.mkdir(parents=True, exist_ok=True)
    (runs / "20261008_replay-eval_20261008_121845_7db43d.yaml").write_text(
        "status: fail\n", encoding="utf-8")
    letter = d._upsert_wait_for(d.set_status(ticket.read_text(), "prototyped"),
                                "replay-eval_20261008_121845_7db43d")
    ticket.write_text(letter, encoding="utf-8")
    lanes = d.discover_lanes(root)
    assert d.discover_tasks(root, lanes) == []       # parked: not auto-run
    assert d.deliver_wait_for(root, lanes) == 1      # terminal -> re-opened
    text = ticket.read_text(encoding="utf-8")
    assert d.get_status(text) == "open" and "Job result" in text

    def evaluator(agent, project_dir, root_, prompt, timeout_s):
        assert "Job result" in prompt               # re-delivery context visible
        return "FINAL ANSWER: gate FAIL, negative result.\nticket-status: evaluated\n"

    result = d.handle_task(ticket, "xiulian", root, runner=evaluator)
    assert result == "evaluated"
    assert d.get_status(ticket.read_text()) == "evaluated"
    assert d.discover_tasks(root, lanes) == []       # evaluated never re-runs


def test_ticket_without_trailer_is_consumed_done(tmp_path):
    root = _root(tmp_path)
    proj = _lane(root, "xiulian", {"enabled": True})
    ticket = _ticket(proj / "inbox", "idea_plain.task.md")
    result = d.handle_task(ticket, "xiulian", root,
                           runner=lambda *a, **k: "FINAL ANSWER: narrated only")
    assert result == "done"
    assert d.get_status(ticket.read_text()) == "done"
    assert d.discover_tasks(root) == []             # no infinite re-run


def test_non_ticket_letter_ignores_trailer(tmp_path):
    root = _root(tmp_path)
    inbox = Path(root) / "opencode_cc_mem" / "projects" / "literature" / "inbox"
    task = _task(inbox, body="Mention ticket-status: evaluated in prose.")
    result = d.handle_task(task, "literature", root,
                           runner=lambda *a, **k: "FINAL ANSWER: ok")
    assert result == "done"
    assert d.get_status(task.read_text()) == "done"


# ---- panel micro-fixes: per-lane model, --once lock, session self-heal


def test_per_lane_model_forwarded_only_when_set(tmp_path):
    root = _root(tmp_path)
    inbox = Path(root) / "opencode_cc_mem" / "projects" / "literature" / "inbox"
    task = _task(inbox)
    seen = {}

    def runner(agent, project_dir, root_, prompt, timeout_s, **kw):
        seen["model"] = kw.get("model")
        return "FINAL ANSWER: ok"

    d.handle_task(task, "literature", root, runner=runner,
                  lane_cfg={"enabled": True, "model": "zai-coding-plan/glm-5.3"})
    assert seen["model"] == "zai-coding-plan/glm-5.3"
    # no model in lane cfg -> kwarg absent (5-arg test runners keep working)
    d.handle_task(task, "literature", root, runner=runner,
                  lane_cfg={"enabled": True})
    assert seen["model"] is None


def test_once_locks_by_default_and_force_overrides(tmp_path, monkeypatch):
    """Behavioral contract: while the lock is held elsewhere, a --once scan
    must NOT run; --force overrides. (acquire_lock is monkeypatched rather
    than flock-held: same-process double-flock semantics are unreliable.)"""
    root = _root(tmp_path)
    _lane(root, "literature", {"enabled": True})
    scans = []
    monkeypatch.setattr(d, "acquire_lock", lambda _root: None)   # lock held elsewhere
    monkeypatch.setattr(d, "scan_once", lambda *a, **k: scans.append(1) or [])
    assert d.main(["--once", "--root", root]) == 0
    assert scans == []                        # lock held -> no scan
    monkeypatch.setattr(d, "acquire_lock", lambda _root: 5)      # lock acquired
    assert d.main(["--once", "--root", root, "--force"]) == 0
    assert len(scans) == 1                    # force scans despite the lock


def test_runner_self_heals_stale_session_id(tmp_path, monkeypatch):
    """A stale stored session id made every future letter fail on resume with
    no self-heal (panel A5 brick facet): the daemon must clear the id and
    retry the same letter in a fresh session, once."""
    root = _root(tmp_path)
    proj = _lane(root, "xiulian", {"enabled": True})
    state = d.load_state(root)
    state.setdefault("lanes", {}).setdefault("xiulian", {})["session_id"] = "ses_stale"
    d.save_state(root, state)
    calls = []

    def fake_run_once(model, project_dir, env, timeout_s, extra_args, run_log,
                      binary="opencode"):
        calls.append(list(extra_args))
        if "--session" in extra_args:
            return {"rc": 1, "session_id": None, "final_text": "",
                    "n_tools": 0, "all_text": "", "stderr_tail": "stale"}
        return {"rc": 0, "session_id": "ses_fresh", "final_text": "FINAL ANSWER: ok",
                "n_tools": 1, "all_text": "FINAL ANSWER: ok", "stderr_tail": ""}

    monkeypatch.setattr(d, "_run_once", fake_run_once)
    out = d.default_runner("xiulian", str(proj), root, "do the thing", 60)
    assert out == "FINAL ANSWER: ok"
    assert len(calls) == 2
    assert "--session" in calls[0] and "--session" not in calls[1]   # fresh retry
    assert d.load_state(root)["lanes"]["xiulian"]["session_id"] == "ses_fresh"


def test_scheduled_sweep_letter_written_once(tmp_path):
    root = _root(tmp_path)
    _lane(root, "literature", {"enabled": True, "schedule_days": 1,
                               "schedule_task": "Run the sweep."})
    lanes = d.discover_lanes(root)
    assert d.scheduled_sweep_letters(root, lanes) == 1
    letters = list((Path(root) / "opencode_cc_mem" / "projects" / "literature" / "inbox").glob("*scheduled-sweep.task.md"))
    assert len(letters) == 1 and "Run the sweep." in letters[0].read_text()
    assert d.admit_letter(root, "literature", letters[0]).is_scheduled is True
    # second call same day: no duplicate
    assert d.scheduled_sweep_letters(root, lanes) == 0


def test_lane_session_persisted_after_run(tmp_path, monkeypatch):
    """After a run the lane's session id is stored in state["lanes"][agent]
    so the next letter queues into the SAME session (v2 inbox model)."""
    root = _root(tmp_path)
    _lane(root, "literature", {"enabled": True})
    calls = []

    def fake_run_once(model, project_dir, env, timeout_s, extra_args, run_log, binary="opencode"):
        calls.append(list(extra_args))
        return {"session_id": "ses_lane1", "final_text": "FINAL ANSWER: ok",
                "all_text": "x", "n_tools": 0, "rc": 0, "stderr_tail": ""}

    monkeypatch.setattr(d, "_run_once", fake_run_once)
    out = d.default_runner("literature", str(tmp_path), root, "do the task", 60)
    assert "FINAL ANSWER" in out
    state = d.load_state(root)
    assert state["lanes"]["literature"]["session_id"] == "ses_lane1"
    # second letter resumes the same session
    out2 = d.default_runner("literature", str(tmp_path), root, "next letter", 60)
    assert calls[-1][:2] == ["--session", "ses_lane1"]


def test_opencode_policy_change_starts_fresh_session(tmp_path, monkeypatch):
    root = _root(tmp_path)
    _lane(root, "literature", {"enabled": True})
    project = str(tmp_path)
    d.save_state(
        root,
        {
            "done": {},
            "lanes": {
                "literature": {
                    "session_id": "legacy-id",
                    "session": {
                        "client": "opencode",
                        "model": "deepseek/deepseek-flash",
                        "session_id": "old-policy-id",
                        "project_digest": d._project_digest(project),
                        "role": "helper",
                        "policy_digest": "obsolete-policy",
                    },
                }
            },
        },
    )
    calls = []

    def fake_run_once(model, project_dir, env, timeout_s, extra_args, run_log, **kwargs):
        calls.append(list(extra_args))
        return {"session_id": "fresh-id", "final_text": "FINAL ANSWER: ok",
                "all_text": "ok", "n_tools": 0, "rc": 0, "stderr_tail": ""}

    monkeypatch.setattr(d, "_run_once", fake_run_once)
    d.default_runner("literature", project, root, "new policy", 60)

    assert calls == [["new policy"]]
    session = d.load_state(root)["lanes"]["literature"]["session"]
    assert session["session_id"] == "fresh-id"
    assert session["policy_digest"] != "obsolete-policy"


# ---------------------------------------------------------------------------
# Plan 3 helper routes, identity-bound sessions, quota and visible refusals
# ---------------------------------------------------------------------------


def _registered_letter(root: str, *, model: str = "claude-code/haiku", capabilities=None):
    sender = _lane(root, "xiulian")
    lane = _lane(
        root,
        "literature",
        {
            "enabled": True,
            "model": model,
            "required_capabilities": capabilities or [],
        },
    )
    inbox = lane / "inbox" / "from-xiulian"
    return _task(inbox), sender


def _worker_result(
    *,
    session_id="session-1",
    final="FINAL ANSWER: done",
    outcome=None,
    client="claude-code",
    model="haiku",
    policy="policy-1",
    project="project-1",
):
    identity = SessionIdentity(client, model, session_id, project, "helper", policy)
    artifact = ResumeArtifact(client, session_id, policy, "helper", project)
    return WorkerResult(
        0,
        False,
        0.01,
        session_id,
        final,
        final,
        0,
        {},
        1,
        [],
        [],
        outcome=outcome or RunOutcome("success"),
        session_identity=identity,
        resume_artifact=artifact,
    )


def test_daemon_route_and_model_reach_client(tmp_path, monkeypatch):
    root = _root(tmp_path)
    task, _ = _registered_letter(
        root, model="claude-code/haiku", capabilities=["native_read"]
    )
    lane = task.parents[2]
    captured = {}

    class FakeExecutor:
        prepared = type("Prepared", (), {"invocation_dir": tmp_path / "invocation"})()

        def run(self, prompt, cwd, attempt_dir, **kwargs):
            captured.update(cwd=Path(cwd), prompt=prompt)
            return _worker_result()

    def helper_executor(route, context):
        captured["route"] = route
        return FakeExecutor()

    monkeypatch.setattr(d, "_helper_executor", helper_executor)
    monkeypatch.setattr(d, "_role_capabilities", lambda: {})
    monkeypatch.setattr(d, "_configuration_digest", lambda context: "policy-1")

    answer = d.default_runner("literature", str(lane), root, "PROMPT", 60)

    assert answer == "FINAL ANSWER: done"
    assert captured["route"].client == "claude-code"
    assert captured["route"].model == "haiku"


def test_daemon_session_resume_and_route_switch(tmp_path, monkeypatch):
    root = _root(tmp_path)
    lane = _lane(root, "literature", {"enabled": True, "model": "claude-code/haiku"})
    (lane / "docs").mkdir()
    calls = []

    class FakeExecutor:
        def __init__(self, route, context):
            self.route = route
            self.context = context
            self.prepared = type(
                "Prepared", (), {"invocation_dir": tmp_path / f"inv-{len(calls)}"}
            )()

        def run(self, prompt, cwd, attempt_dir, **kwargs):
            calls.append((self.route.client, self.route.model, kwargs.get("resume_session")))
            sid = kwargs.get("resume_session") or f"session-{len(calls)}"
            return _worker_result(
                session_id=sid,
                client=self.route.client,
                model=self.route.model,
                policy=f"policy-{self.route.client}",
                project=self.context.project_identity,
            )

    monkeypatch.setattr(d, "_helper_executor", lambda route, context: FakeExecutor(route, context))
    monkeypatch.setattr(d, "_role_capabilities", lambda: {})
    monkeypatch.setattr(
        d, "_configuration_digest", lambda context: f"policy-{context.route.client}"
    )

    d.default_runner("literature", str(lane), root, "one", 60)
    d.default_runner("literature", str(lane), root, "two", 60)
    cfg = json.loads((lane / "agent.json").read_text())
    cfg["model"] = "codex/gpt-5.6-sol"
    (lane / "agent.json").write_text(json.dumps(cfg), encoding="utf-8")
    d.default_runner("literature", str(lane), root, "three", 60)

    assert calls[0][2] is None
    assert calls[1][2] == "session-1"
    assert calls[2][0:2] == ("codex", "gpt-5.6-sol")
    assert calls[2][2] is None


def test_daemon_quota_bypasses_fresh_session_recovery(tmp_path, monkeypatch):
    root = _root(tmp_path)
    lane = _lane(root, "literature", {"enabled": True, "model": "claude-code/haiku"})
    (lane / "docs").mkdir()
    d.save_state(
        root,
        {
            "done": {},
            "lanes": {
                "literature": {
                    "session": {
                        "client": "claude-code",
                        "model": "haiku",
                        "session_id": "stale",
                        "project_digest": d._project_digest(lane),
                        "role": "helper",
                        "policy_digest": "policy-claude-code",
                        "configuration_identity_digest": "policy-claude-code",
                        "launch_path": str(tmp_path / "launch.json"),
                    }
                }
            },
        },
    )
    calls = []

    class FakeExecutor:
        prepared = type("Prepared", (), {"invocation_dir": tmp_path})()

        def run(self, *args, **kwargs):
            calls.append(kwargs.get("resume_session"))
            return _worker_result(
                session_id="stale",
                final="",
                outcome=RunOutcome(
                    "quota", LimitInfo("quota", None, "fixture", "hold-1")
                ),
            )

    monkeypatch.setattr(d, "_helper_executor", lambda route, context: FakeExecutor())
    monkeypatch.setattr(d, "_role_capabilities", lambda: {})
    monkeypatch.setattr(d, "_configuration_digest", lambda context: "policy-claude-code")

    with pytest.raises(d.HelperQuotaWaiting):
        d.default_runner("literature", str(lane), root, "PROMPT", 60)

    assert calls == ["stale"]


def test_daemon_wait_survives_restart(tmp_path):
    root = _root(tmp_path)
    task, _ = _registered_letter(root)

    def quota(*args, **kwargs):
        raise d.HelperQuotaWaiting("claude-code", "hold-1", None, "quota")

    assert d.handle_task(task, "literature", root, runner=quota) == "waiting_quota"
    assert d.get_status(task.read_text()) == "open"
    assert str(task.resolve()) in d.load_state(root)["quota_waits"]
    assert d.discover_tasks(root, d.discover_lanes(root)) == []
    assert d.resume_quota_waits(root, "literature") == 1
    assert d.discover_tasks(root, d.discover_lanes(root)) == [task]


def test_sender_refusal_starts_no_client_and_acquires_no_slot(tmp_path, monkeypatch):
    root = _root(tmp_path)
    lane = Path(root) / "opencode_cc_mem" / "projects" / "literature"
    task = _task(lane / "inbox" / "from-missing")
    task.write_text(task.read_text().replace("from: xiulian", "from: missing"))
    calls = []

    monkeypatch.setattr(
        d,
        "_build_helper_context",
        lambda *args, **kwargs: calls.append("governor") or None,
    )

    assert d.handle_task(task, "literature", root, runner=d.default_runner) == "refused"
    assert calls == []
    assert "did not launch" in task.with_name("20260924_ask_reply.md").read_text()


def test_daemon_no_tools_helper_reports_capability_limit(tmp_path, monkeypatch):
    root = _root(tmp_path)
    task, _ = _registered_letter(root, capabilities=[])
    starts = []

    def refuse(route, context):
        starts.append("registry")
        raise ExecutorRefused("helper no-tools capability row is unavailable")

    monkeypatch.setattr(d, "_helper_executor", refuse)
    monkeypatch.setattr(d, "_role_capabilities", lambda: {})
    result = d.handle_task(task, "literature", root, runner=d.default_runner)

    assert result == "refused"
    assert starts == ["registry"]
    assert "capability" in task.with_name("20260924_ask_reply.md").read_text()


def test_daemon_required_capability_unavailable_refuses(tmp_path, monkeypatch):
    root = _root(tmp_path)
    task, _ = _registered_letter(root, capabilities=["browser"])
    launched = []

    def refuse(route, context):
        assert context.required_capabilities == ("browser",)
        raise ExecutorRefused("unsupported required capabilities: browser")

    monkeypatch.setattr(d, "_helper_executor", refuse)
    monkeypatch.setattr(d, "_role_capabilities", lambda: {})
    monkeypatch.setattr(d, "_run_once", lambda *a, **k: launched.append(True))

    assert d.handle_task(task, "literature", root, runner=d.default_runner) == "refused"
    assert launched == []
