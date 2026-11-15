"""Neutral hook dispatch across the committed behaviour modules."""

from __future__ import annotations

import json
import time
from pathlib import Path

from compchem_memory import quick_search
from compchem_memory.hook import critic
from compchem_memory.hook.directive import DIRECTIVE
from compchem_memory.hook.dispatch import handle
from compchem_memory.hook.events import AgentRef, HookEvent, ToolCall
from compchem_memory.hook.state import SessionState

FIXTURES = Path(__file__).resolve().parents[1] / "fixtures" / "hooks"


def _workspace(tmp_path: Path) -> tuple[Path, dict[str, str]]:
    root = tmp_path / "workspace"
    project = root / "projects" / "alpha"
    (root / "rules").mkdir(parents=True)
    (project / ".magnolia").mkdir(parents=True)
    (project / ".magnolia" / "GOAL.md").write_text("Goal\n", encoding="utf-8")
    return project, {"XDG_STATE_HOME": str(tmp_path / "state")}


def _event(project: Path, kind: str, **values: object) -> HookEvent:
    return HookEvent(
        harness="json",
        event=kind,
        session_id=str(values.pop("session_id", "session-1")),
        cwd=str(project),
        **values,
    )


def _rows(path: Path) -> list[dict]:
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()]


def _post_tool(project: Path) -> HookEvent:
    return _event(
        project,
        "post-tool",
        tool=ToolCall(
            name="run_shell",
            server="compchem-tools",
            raw_name="compchem-tools_run_shell",
            call_id="call-1",
            input={"cmd": "haddock3 run workflow"},
            output="completed successfully",
        ),
    )


def test_dispatch_session_start_context(tmp_path, monkeypatch):
    project, env = _workspace(tmp_path)
    store = project / ".magnolia"
    (store / "boot-context.md").write_text("Remember this project.\n", encoding="utf-8")
    monkeypatch.delenv("MAGNOLIA_AUTORETRIEVE", raising=False)

    handled = handle(_event(project, "session-start"), env, time.monotonic() + 8)

    assert "Remember this project." in handled.response.context
    assert DIRECTIVE in handled.response.context
    assert handled.pending is None
    assert _rows(store / "transcripts" / "json" / "session-1.jsonl")[0]["kind"] == "session-start"
    assert _rows(store / "agent-sessions.jsonl")[0]["session_id"] == "session-1"


def test_dispatch_prompt_doorbell_context(tmp_path, monkeypatch):
    project, env = _workspace(tmp_path)
    monkeypatch.setenv("MAGNOLIA_JOB_NOTIFY", "0")

    handled = handle(
        _event(project, "prompt", prompt="@literature find three papers"),
        env,
        time.monotonic() + 8,
    )

    assert "@literature" in handled.response.context
    assert "already sent to helper agents" in handled.response.context
    assert DIRECTIVE in handled.response.context
    letters = list((project.parent / "literature" / "inbox" / "from-alpha").glob("*.task.md"))
    assert len(letters) == 1


def test_dispatch_post_tool_note(tmp_path, monkeypatch):
    project, env = _workspace(tmp_path)
    store = project / ".magnolia"
    entry = store / "entries" / "haddock.md"
    entry.parent.mkdir()
    entry.write_text("Haddock workflow recovery\n", encoding="utf-8")
    hit = {
        "title": "Haddock workflow recovery",
        "type": "error_resolution",
        "tier": "project",
        "score": 4,
        "provisional": False,
        "confidence": 0.9,
        "path": str(entry),
        "gist": "Use the recovered workflow.",
    }
    monkeypatch.setattr(quick_search, "search", lambda *args, **kwargs: [hit])
    monkeypatch.setenv("MAGNOLIA_JOB_NOTIFY", "0")

    handled = handle(_post_tool(project), env, time.monotonic() + 8)

    assert handled.pending is not None
    assert handled.response.tool_note == handled.pending.text
    assert "Haddock workflow recovery" in handled.response.tool_note
    assert not (store / "surfacing.jsonl").exists()


def test_dispatch_turn_end_runs_critic_last(tmp_path, monkeypatch):
    project, env = _workspace(tmp_path)
    observed = []

    def record_critic(paths, harness, session_id, agent_id):
        rows = _rows(paths.store / "transcripts" / harness / f"{session_id}.jsonl")
        observed.append((rows[-1]["text"], harness, session_id, agent_id))

    monkeypatch.setattr(critic, "run_critic", record_critic)
    event = _event(
        project,
        "turn-end",
        assistant_text="The completed report.",
        agent=AgentRef("agent-7", "worker"),
    )

    handled = handle(event, env, time.monotonic() + 8)

    assert handled.response.context is None
    assert observed == [("The completed report.", "json", "session-1", "agent-7")]


def test_dispatch_binds_on_first_event_of_any_kind(tmp_path, monkeypatch):
    project, env = _workspace(tmp_path)
    monkeypatch.setenv("MAGNOLIA_JOB_NOTIFY", "0")

    handle(_event(project, "pre-tool", tool=ToolCall("read", None, "Read", None, {})), env, time.monotonic() + 8)

    binding = Path(env["XDG_STATE_HOME"]) / "magnolia" / "bindings" / "json-session-1.json"
    assert json.loads(binding.read_text(encoding="utf-8"))["project"]["project_dir"] == str(project)
    assert len(_rows(project / ".magnolia" / "agent-sessions.jsonl")) == 1



def _letters(project: Path) -> list[Path]:
    return list((project.parent / "literature" / "inbox").rglob("*.task.md"))


def _queue_notice(project: Path, text: str = "critic flag") -> None:
    with SessionState.open(project / ".magnolia", "json", "session-1") as state:
        state.data["unshown_notices"] = [text]


def test_subagent_prompt_rings_nothing(tmp_path, monkeypatch):
    project, env = _workspace(tmp_path)
    monkeypatch.setenv("MAGNOLIA_JOB_NOTIFY", "0")
    monkeypatch.delenv("MAGNOLIA_AUTORETRIEVE", raising=False)
    _queue_notice(project)
    # Shape of codex/subagent/01-UserPromptSubmit.sub.json: a prompt carrying agent_id.
    sub = json.loads((FIXTURES / "codex" / "subagent" / "01-UserPromptSubmit.sub.json").read_text())
    event = _event(project, "prompt", prompt="@literature find three papers",
                   agent=AgentRef(sub["agent_id"], sub.get("agent_type")))

    handled = handle(event, env, time.monotonic() + 8)

    assert handled.response.context is None and handled.response.notice is None
    assert handled.notices is None
    assert _letters(project) == []
    rows = _rows(project / ".magnolia" / "transcripts" / "json" / "session-1.jsonl")
    assert rows[-1]["kind"] == "prompt" and rows[-1]["agent"]["id"] == sub["agent_id"]

    main = handle(_event(project, "prompt", prompt="hello"), env, time.monotonic() + 8)
    assert DIRECTIVE in main.response.context
    assert main.response.notice == "critic flag"


def test_claude_hand_back_prompt_rings_nothing(tmp_path, monkeypatch):
    project, env = _workspace(tmp_path)
    monkeypatch.setenv("MAGNOLIA_JOB_NOTIFY", "0")
    monkeypatch.delenv("MAGNOLIA_AUTORETRIEVE", raising=False)
    _queue_notice(project)
    hand_back = json.loads((FIXTURES / "claude" / "parent" / "21-UserPromptSubmit.json").read_text())["prompt"]
    assert hand_back.startswith("<agent-message ")
    prompt = hand_back.replace('{"result":"sub"}', "@literature find three papers")

    handled = handle(_event(project, "prompt", prompt=prompt), env, time.monotonic() + 8)

    assert handled.response.context is None and handled.response.notice is None
    assert _letters(project) == []
    with SessionState.open(project / ".magnolia", "json", "session-1") as state:
        assert state.data["unshown_notices"] == ["critic flag"]


def test_subagent_post_tool_leaves_notices_queued(tmp_path, monkeypatch):
    project, env = _workspace(tmp_path)
    monkeypatch.setenv("MAGNOLIA_JOB_NOTIFY", "0")
    monkeypatch.setattr(quick_search, "search", lambda *args, **kwargs: [])
    _queue_notice(project)
    event = _post_tool(project)
    event.agent = AgentRef("child-1", "Explore")

    handled = handle(event, env, time.monotonic() + 8)

    assert handled.response.notice is None
    assert handle(_post_tool(project), env, time.monotonic() + 8).response.notice == "critic flag"
