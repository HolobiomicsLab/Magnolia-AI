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
