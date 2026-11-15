"""JSON adapter and fail-open hook CLI lifecycle tests."""

from __future__ import annotations

import json
import os
import statistics
import subprocess
import sys
import time
from pathlib import Path
from types import SimpleNamespace

import pytest

from compchem_memory import quick_search
from compchem_memory.hook.__main__ import _commit_delivered, _log_error, _render_handled
from compchem_memory.hook.adapters import Rendered
from compchem_memory.hook.adapters import json_adapter
from compchem_memory.hook.dispatch import handle
from compchem_memory.hook.events import Block, HookEvent, HookResponse, ToolCall
from compchem_memory.hook import project as project_module
from compchem_memory.hook.state import SessionState
from compchem_memory.job_notices import push_job_notice


def _workspace(tmp_path: Path) -> Path:
    root = tmp_path / "workspace"
    project = root / "projects" / "alpha"
    (root / "rules").mkdir(parents=True)
    (project / ".magnolia" / "entries").mkdir(parents=True)
    (project / ".magnolia" / "GOAL.md").write_text("Goal\n", encoding="utf-8")
    return project


def _payload(project: Path, session_id: str, event: str) -> dict:
    row = {"session_id": session_id, "cwd": str(project)}
    if event == "prompt":
        row["prompt"] = "Summarise the project"
    elif event == "post-tool":
        row["tool"] = {
            "raw_name": "compchem-tools_run_shell",
            "call_id": "call-1",
            "input": {"cmd": "haddock3 run workflow"},
            "output": "completed successfully",
        }
    elif event == "turn-end":
        row["assistant_text"] = "A" * 300
    return row


def _run_cli(project: Path, state: Path, event: str, session_id: str, env: dict | None = None):
    process_env = dict(os.environ if env is None else env)
    process_env["HOME"] = str(state.parent / "home")
    process_env["XDG_STATE_HOME"] = str(state)
    return subprocess.run(
        [sys.executable, "-m", "compchem_memory.hook", "json", event],
        input=json.dumps(_payload(project, session_id, event)),
        capture_output=True,
        text=True,
        timeout=30,
        env=process_env,
    )


def test_dropped_tool_note_leaves_no_exposure(tmp_path, monkeypatch):
    project = _workspace(tmp_path)
    store = project / ".magnolia"
    entry = store / "entries" / "haddock.md"
    entry.write_text("Haddock workflow recovery\n", encoding="utf-8")
    hit = {"title": "Haddock workflow", "type": "error_resolution", "tier": "project",
           "score": 4, "path": str(entry), "gist": "Recover safely."}
    monkeypatch.setattr(quick_search, "search", lambda *args, **kwargs: [hit])
    event = HookEvent(
        "json", "post-tool", "s1", str(project),
        tool=ToolCall("run_shell", "compchem-tools", "compchem-tools_run_shell", "c1",
                      {"cmd": "haddock3 run workflow"}, "ok"),
    )
    handled = handle(event, {"XDG_STATE_HOME": str(tmp_path / "state")}, time.monotonic() + 8)
    adapter = SimpleNamespace(
        render=lambda response, current: Rendered("", "", 0, frozenset())
    )

    _render_handled(adapter, handled, event)

    assert handled.pending is not None
    assert not (store / "surfacing.jsonl").exists()


def test_dropped_notice_is_delivered_on_next_event(tmp_path, monkeypatch):
    project = _workspace(tmp_path)
    store = project / ".magnolia"
    env = {"XDG_STATE_HOME": str(tmp_path / "state")}
    monkeypatch.delenv("MAGNOLIA_JOB_NOTIFY", raising=False)
    monkeypatch.setattr(quick_search, "search", lambda *args, **kwargs: [])
    push_job_notice(str(project), run_id="run-1", tool="dock", state="completed", category="success")
    with SessionState.open(store, "json", "s1") as state:
        state.data["unshown_notices"] = ["⚠ (high) unsupported ranking"]
    expected = "✅ dock finished (completed) — run …run-1\n⚠ (high) unsupported ranking"

    def event():
        return HookEvent(
            "json", "post-tool", "s1", str(project),
            tool=ToolCall("run_shell", "compchem-tools", "compchem-tools_run_shell", "c1",
                          {"cmd": "ls"}, "ok"),
        )

    first = handle(event(), env, time.monotonic() + 8)
    assert first.response.notice == expected
    _commit_delivered(Rendered("", "", 0, frozenset({"tool_note"})), first, event())

    second = handle(event(), env, time.monotonic() + 8)
    assert second.response.notice == expected
    _commit_delivered(Rendered("", "", 0, frozenset({"notice"})), second, event())

    third = handle(event(), env, time.monotonic() + 8)
    assert third.response.notice is None
    assert not (store / ".job-notices.jsonl").exists()


def test_dropped_tool_note_is_offered_again(tmp_path, monkeypatch):
    project = _workspace(tmp_path)
    store = project / ".magnolia"
    entry = store / "entries" / "haddock.md"
    entry.write_text("Haddock workflow recovery\n", encoding="utf-8")
    hit = {"title": "Haddock workflow", "type": "error_resolution", "tier": "project",
           "score": 4, "path": str(entry), "gist": "Recover safely."}
    monkeypatch.setattr(quick_search, "search", lambda *args, **kwargs: [hit])
    env = {"XDG_STATE_HOME": str(tmp_path / "state")}

    def event(call_id):
        return HookEvent(
            "json", "post-tool", "s1", str(project),
            tool=ToolCall("run_shell", "compchem-tools", "compchem-tools_run_shell", call_id,
                          {"cmd": "haddock3 run workflow"}, "ok"),
        )

    for call_id in ("c1", "c2", "c3"):
        handled = handle(event(call_id), env, time.monotonic() + 8)
        assert handled.pending is not None
        _commit_delivered(Rendered("", "", 0, frozenset()), handled, event(call_id))
    with SessionState.open(store, "json", "s1") as state:
        assert state.data.get("injections", 0) == 0

    delivered = handle(event("c4"), env, time.monotonic() + 8)
    _commit_delivered(Rendered("", "", 0, frozenset({"tool_note"})), delivered, event("c4"))
    with SessionState.open(store, "json", "s1") as state:
        assert state.data["injections"] == 1
    rows = [json.loads(line) for line in (store / "action-retrieval.jsonl").read_text().splitlines()]
    assert [row["injected"] for row in rows if "injected" in row] == [0, 0, 0, 1]
    assert (store / "surfacing.jsonl").exists()


def test_block_rendered_by_adapter():
    event = HookEvent("json", "pre-tool", "s1", "/tmp")

    rendered = json_adapter.render(
        HookResponse(context="context before block", block=Block("unsafe operation")),
        event,
    )

    assert rendered.exit_code == 2
    assert rendered.stderr == "unsafe operation\n"
    assert json.loads(rendered.stdout) == {"context": "context before block"}
    assert rendered.emitted == frozenset({"context"})


def test_json_adapter_parses_cli_harness_and_emits_present_fields():
    event = json_adapter.parse(
        {"harness": "json", "session_id": "s1", "cwd": "/tmp"},
        "prompt",
    )
    rendered = json_adapter.render(HookResponse(context="ctx", notice="note"), event)

    assert event.harness == "json" and event.event == "prompt"
    assert json.loads(rendered.stdout) == {"context": "ctx", "notice": "note"}
    assert rendered.emitted == frozenset({"context", "notice"})


def test_core_without_any_model_credential(tmp_path):
    project = _workspace(tmp_path)
    state = tmp_path / "state"
    clean_env = {
        key: value for key, value in os.environ.items()
        if not key.endswith("_API_KEY")
        and not key.endswith("_BASE_URL")
        and not key.startswith("MAGNOLIA_CRITIC_")
    }
    clean_env.update({"MAGNOLIA_CRITIC": "1", "MAGNOLIA_JOB_NOTIFY": "0"})

    for event in ("session-start", "prompt", "post-tool", "turn-end"):
        proc = _run_cli(project, state, event, "no-credential", clean_env)
        assert proc.returncode == 0, (event, proc.stderr)

    assert not (project / ".magnolia" / "claim-critic").exists()


def test_cli_logs_post_bind_failure_to_project_store(tmp_path):
    project = _workspace(tmp_path)
    state = tmp_path / "state"
    payload = _payload(project, "missing-tool", "post-tool")
    payload.pop("tool")
    env = dict(os.environ)
    env["HOME"] = str(tmp_path / "home")
    env["XDG_STATE_HOME"] = str(state)

    proc = subprocess.run(
        [sys.executable, "-m", "compchem_memory.hook", "json", "post-tool"],
        input=json.dumps(payload),
        capture_output=True,
        text=True,
        timeout=30,
        env=env,
    )

    assert proc.returncode == 0 and proc.stdout == "" and proc.stderr == ""
    rows = [
        json.loads(line)
        for line in (project / ".magnolia" / "hook-errors.jsonl").read_text().splitlines()
    ]
    assert rows[0]["harness"] == "json" and rows[0]["event"] == "post-tool"
    assert rows[0]["error"].startswith("AttributeError:")


def test_error_log_falls_back_to_state_home_when_binding_raises(tmp_path, monkeypatch):
    state = tmp_path / "state"
    env = {"HOME": str(tmp_path / "home"), "XDG_STATE_HOME": str(state)}
    event = HookEvent("json", "prompt", "broken-binding", str(tmp_path))

    def raise_binding_error(*args, **kwargs):
        raise OSError("binding failed")

    monkeypatch.setattr(project_module, "bind_project", raise_binding_error)

    _log_error(ValueError("dispatch failed"), "json", "prompt", event, env)

    rows = [
        json.loads(line)
        for line in (state / "magnolia" / "hook-errors.jsonl").read_text().splitlines()
    ]
    assert rows[0]["error"] == "ValueError: dispatch failed"


def test_cli_writes_utf8_bytes_with_ascii_python_io_encoding(tmp_path):
    project = _workspace(tmp_path)
    state = tmp_path / "state"
    env = dict(os.environ)
    env.update({
        "HOME": str(tmp_path / "home"),
        "PYTHONIOENCODING": "ascii",
        "XDG_STATE_HOME": str(state),
    })

    proc = subprocess.run(
        [sys.executable, "-m", "compchem_memory.hook", "json", "session-start"],
        input=json.dumps(_payload(project, "ascii-output", "session-start")).encode("utf-8"),
        capture_output=True,
        timeout=30,
        env=env,
    )

    output = json.loads(proc.stdout.decode("utf-8"))
    assert proc.returncode == 0 and output["context"]
    assert proc.stderr == b""


def test_cli_stdout_failure_logs_error_without_exposure(tmp_path):
    project = _workspace(tmp_path)
    entry = project / ".magnolia" / "entries" / "haddock.md"
    entry.write_text(
        "---\ntitle: Haddock workflow\ntype: error_resolution\n---\n"
        "haddock3 run workflow recovery\n",
        encoding="utf-8",
    )
    env = dict(os.environ)
    env.update({
        "HOME": str(tmp_path / "home"),
        "MAGNOLIA_JOB_NOTIFY": "0",
        "XDG_STATE_HOME": str(tmp_path / "state"),
    })
    proc = subprocess.Popen(
        [sys.executable, "-m", "compchem_memory.hook", "json", "post-tool"],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        env=env,
    )
    assert proc.stdin is not None and proc.stdout is not None and proc.stderr is not None
    proc.stdout.close()
    proc.stdin.write(json.dumps(_payload(project, "closed-stdout", "post-tool")).encode("utf-8"))
    proc.stdin.close()
    stderr = proc.stderr.read()
    returncode = proc.wait(timeout=30)

    assert returncode == 0 and stderr == b""
    rows = [
        json.loads(line)
        for line in (project / ".magnolia" / "hook-errors.jsonl").read_text().splitlines()
    ]
    assert rows[0]["error"].startswith("BrokenPipeError:")
    assert not (project / ".magnolia" / "surfacing.jsonl").exists()


def test_cli_post_tool_exits_within_deadline(tmp_path):
    project = _workspace(tmp_path)
    entry = project / ".magnolia" / "entries" / "haddock.md"
    entry.write_text(
        "---\ntitle: Haddock workflow\ntype: error_resolution\n---\n"
        "haddock3 run workflow recovery\n",
        encoding="utf-8",
    )
    env = dict(os.environ)
    env.update({
        "MAGNOLIA_HOOK_TEST": "1",
        "MAGNOLIA_HOOK_TEST_SEARCH_SLEEP": "10",
        "MAGNOLIA_JOB_NOTIFY": "0",
    })
    started = time.perf_counter()

    proc = _run_cli(project, tmp_path / "state", "post-tool", "slow-search", env)

    elapsed = time.perf_counter() - started
    assert proc.returncode == 0 and elapsed < 2.5, (elapsed, proc.stderr)
    rows = [
        json.loads(line)
        for line in (project / ".magnolia" / "action-retrieval.jsonl").read_text().splitlines()
    ]
    assert any(row.get("skipped") == "timeout" for row in rows)


@pytest.mark.parametrize(
    "stdin",
    [
        "",
        "not json",
        "{}",
        '{"event":"prompt"}',
        '{"harness":"json","event":"post-tool","session_id":"s","cwd":"/nope",'
        '"tool":{"raw_name":"x","output":[1,2]}}',
    ],
)
def test_cli_garbage_payloads_exit_zero(stdin, tmp_path):
    env = dict(os.environ)
    env["HOME"] = str(tmp_path / "home")
    env["XDG_STATE_HOME"] = str(tmp_path / "state")
    proc = subprocess.run(
        [sys.executable, "-m", "compchem_memory.hook", "json", "post-tool"],
        input=stdin,
        capture_output=True,
        text=True,
        timeout=30,
        env=env,
    )
    assert proc.returncode == 0 and proc.stdout == ""


def test_cli_cold_start_under_budget(tmp_path):
    project = _workspace(tmp_path)
    state = tmp_path / "state"
    env = dict(os.environ)
    env["MAGNOLIA_JOB_NOTIFY"] = "0"
    medians = {}

    for event in ("session-start", "post-tool"):
        durations = []
        for index in range(5):
            started = time.perf_counter()
            proc = _run_cli(project, state, event, f"cold-{event}-{index}", env)
            durations.append(time.perf_counter() - started)
            assert proc.returncode == 0, proc.stderr
        medians[event] = statistics.median(durations)

    assert medians["session-start"] < 1.0
    assert medians["post-tool"] < 1.0
