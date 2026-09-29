"""Tests for the magnolia-agents-daemon (mailbox watcher for helper agents).

The daemon script is a standalone python3 file; tests import it by path.
All runner calls are stubbed — no LLM, no opencode, no network."""
import importlib.util
import os
import json
from importlib.machinery import SourceFileLoader
from pathlib import Path

import pytest

_SCRIPT = Path(__file__).resolve().parents[1] / "softwares" / "bin" / "magnolia-agents-daemon"
_spec = importlib.util.spec_from_file_location(
    "agents_daemon", _SCRIPT, loader=SourceFileLoader("agents_daemon", str(_SCRIPT)))
d = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(d)


def _root(tmp_path):
    root = tmp_path / "mroot"
    root.mkdir(parents=True, exist_ok=True)
    return str(root)


def _task(inbox: Path, name: str = "20260924_ask.task.md", status: str = "open",
          body: str = "Summarize the paper.") -> Path:
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

    def fake_once(model, project_dir, env, timeout_s, extra_args, run_log):
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

    def fake_once(model, project_dir, env, timeout_s, extra_args, run_log):
        calls.append(extra_args)
        return responses[len(calls) - 1]

    monkeypatch.setattr(d, "_run_once", fake_once)
    out = d.default_runner("literature", "/tmp/x", root, "PROMPT", 60)
    assert out == "FINAL ANSWER: done"
    assert calls[1][:3] == ["--session", "ses_1", d.RESUME_NOTE]


def test_runner_gives_up_after_max_attempts(tmp_path, monkeypatch):
    root = _root(tmp_path)
    calls = []

    def fake_once(model, project_dir, env, timeout_s, extra_args, run_log):
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

    def fake_once(model, project_dir, env, timeout_s, extra_args, run_log):
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
