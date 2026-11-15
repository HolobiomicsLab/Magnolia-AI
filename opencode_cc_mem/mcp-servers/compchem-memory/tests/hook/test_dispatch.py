"""Neutral hook dispatch across the committed behaviour modules."""

from __future__ import annotations

import json
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from compchem_memory import quick_search
from compchem_memory.hook import critic
from compchem_memory.hook.directive import DIRECTIVE
from compchem_memory.hook.dispatch import handle
from compchem_memory.hook.events import AgentRef, HookEvent, ToolCall
from compchem_memory.hook.state import SessionState

FIXTURES = Path(__file__).resolve().parents[1] / "fixtures" / "hooks"
CLIENT_TRANSCRIPTS = Path(__file__).resolve().parents[1] / "fixtures" / "client-transcripts"


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

    def record_critic(paths, harness, session_id, agent_id, turn_id):
        rows = _rows(paths.store / "transcripts" / harness / f"{session_id}.jsonl")
        observed.append((rows[-1]["text"], harness, session_id, agent_id, turn_id))

    monkeypatch.setattr(critic, "run_critic", record_critic)
    event = _event(
        project,
        "turn-end",
        assistant_text="The completed report.",
        agent=AgentRef("agent-7", "worker"),
    )

    handled = handle(event, env, time.monotonic() + 8)

    assert handled.response.context is None
    assert observed == [("The completed report.", "json", "session-1", "agent-7", None)]


def test_dispatch_turn_end_passes_turn_id_to_critic_and_probe(tmp_path, monkeypatch):
    from compchem_memory.hook import action_retrieval

    project, env = _workspace(tmp_path)
    observed = []

    monkeypatch.setattr(
        action_retrieval,
        "probe_application",
        lambda paths, harness, session_id, reply, turn_id: observed.append(
            ("probe", harness, session_id, reply, turn_id)
        ),
    )
    monkeypatch.setattr(
        critic,
        "run_critic",
        lambda paths, harness, session_id, agent_id, turn_id: observed.append(
            ("critic", harness, session_id, agent_id, turn_id)
        ),
    )
    event = _event(
        project,
        "turn-end",
        turn_id="turn-7",
        assistant_text="The completed report.",
        agent=AgentRef("agent-7", "worker"),
    )

    handle(event, env, time.monotonic() + 8)

    assert observed == [
        ("probe", "json", "session-1", "The completed report.", "turn-7"),
        ("critic", "json", "session-1", "agent-7", "turn-7"),
    ]


def test_claude_native_subagent_stop_recovers_and_judges_child_reply(
    tmp_path, monkeypatch,
):
    from compchem_memory.harness.render_claude import EVENTS
    from compchem_memory.hook import action_retrieval
    from compchem_memory.hook.adapters import ADAPTERS

    project, env = _workspace(tmp_path)
    raw = json.loads(
        (FIXTURES / "claude" / "subagent" / "09-SubagentStop.sub.json").read_text(
            encoding="utf-8"
        )
    )
    raw.update({
        "cwd": str(project),
        "session_id": "session-1",
        "agent_id": "a18415b0acfbb69d4",
        "prompt_id": "4bb02dbd-650d-4e13-a0bc-9511d9c860cb",
        "agent_transcript_path": str(
            CLIENT_TRANSCRIPTS / "claude" / "subagents" / "agent-a18415b0acfbb69d4.jsonl"
        ),
    })
    observed = []
    monkeypatch.setattr(
        action_retrieval,
        "probe_application",
        lambda paths, harness, session_id, reply, turn_id: observed.append(
            ("probe", harness, session_id, reply, turn_id)
        ),
    )
    monkeypatch.setattr(
        critic,
        "run_critic",
        lambda paths, harness, session_id, agent_id, turn_id: observed.append(
            ("critic", harness, session_id, agent_id, turn_id)
        ),
    )

    for _ in range(2):
        neutral_name = EVENTS[raw["hook_event_name"]]
        event = ADAPTERS["claude"].parse(raw, neutral_name)
        assert event is not None and event.event == "agent-end"
        handle(event, env, time.monotonic() + 8)

    rows = _rows(project / ".magnolia" / "transcripts" / "claude" / "session-1.jsonl")
    assistant = [row for row in rows if row["kind"] == "assistant"]
    assert [(row["text"], row["agent"]["id"]) for row in assistant] == [
        ("beta line", "a18415b0acfbb69d4")
    ]
    expected_turn = "4bb02dbd-650d-4e13-a0bc-9511d9c860cb"
    assert observed == [
        ("probe", "claude", "session-1", "beta line", expected_turn),
        ("critic", "claude", "session-1", "a18415b0acfbb69d4", expected_turn),
    ] * 2


def test_vibe_turn_end_records_reply_text(tmp_path, monkeypatch):
    from compchem_memory.hook import action_retrieval
    from compchem_memory.hook.transcript import append_event

    project, env = _workspace(tmp_path)
    turn_id = "turn-2c40443c7526b229e2076d661938b73f"
    observed = []

    monkeypatch.setattr(
        action_retrieval,
        "probe_application",
        lambda paths, harness, session_id, reply, event_turn_id: observed.append(
            ("probe", reply, event_turn_id)
        ),
    )
    monkeypatch.setattr(
        critic,
        "run_critic",
        lambda paths, harness, session_id, agent_id, event_turn_id: observed.append(
            ("critic", agent_id, event_turn_id)
        ),
    )
    event = HookEvent(
        harness="vibe",
        event="turn-end",
        session_id="session-1",
        cwd=str(project),
        turn_id=turn_id,
        transcript_ref=str(CLIENT_TRANSCRIPTS / "vibe" / "session"),
    )
    append_event(
        project / ".magnolia",
        HookEvent(
            harness="vibe", event="turn-end", session_id="session-1",
            cwd=str(project), turn_id=turn_id, assistant_text="CHECKING.",
        ),
    )

    handle(event, env, time.monotonic() + 8)

    rows = _rows(project / ".magnolia" / "transcripts" / "vibe" / "session-1.jsonl")
    assert [row["text"] for row in rows if row["kind"] == "assistant"] == [
        "CHECKING.",
        'note.txt contains exactly one line: "alpha line". '
        "It is a minimal placeholder note with no other content or instructions.",
    ]
    assert observed == [
        ("probe", rows[-1]["text"], turn_id),
        ("critic", None, turn_id),
    ]


def test_client_recovery_keeps_native_final_reply_last(tmp_path, monkeypatch):
    from compchem_memory.hook import action_retrieval

    project, env = _workspace(tmp_path)
    turn_id = "01a1df67-264d-7472-b477-66828e7d31b6"
    final = (
        "```sh\ncat note.txt\n```\n\n> “alpha line”\n\n"
        "The note contains a single line: “alpha line.”"
    )
    monkeypatch.setattr(action_retrieval, "probe_application", lambda *args: None)
    monkeypatch.setattr(critic, "run_critic", lambda *args: None)
    event = HookEvent(
        harness="codex",
        event="turn-end",
        session_id="session-1",
        cwd=str(project),
        turn_id=turn_id,
        assistant_text=final,
        transcript_ref=str(CLIENT_TRANSCRIPTS / "codex" / "rollout.jsonl"),
    )

    handle(event, env, time.monotonic() + 8)

    rows = _rows(project / ".magnolia" / "transcripts" / "codex" / "session-1.jsonl")
    assert [row["text"] for row in rows if row["kind"] == "assistant"] == ["CHECKING.", final]


def _codex_transcript(path: Path, turn_id: str, *texts: str) -> Path:
    rows = [{"type": "event_msg", "payload": {"type": "task_started", "turn_id": turn_id}}]
    rows.extend({"type": "response_item", "payload": {
        "type": "message", "role": "assistant",
        "content": [{"type": "output_text", "text": text}],
    }} for text in texts)
    rows.append({"type": "event_msg", "payload": {"type": "task_complete", "turn_id": turn_id}})
    path.write_text("".join(json.dumps(row) + "\n" for row in rows), encoding="utf-8")
    return path


def _quiet_turn_end(monkeypatch) -> None:
    from compchem_memory.hook import action_retrieval

    monkeypatch.setattr(action_retrieval, "probe_application", lambda *args: None)
    monkeypatch.setattr(critic, "run_critic", lambda *args: None)


def test_client_recovery_basic_auth_is_idempotent_across_two_stops(tmp_path, monkeypatch):
    project, env = _workspace(tmp_path)
    turn_id = "basic-turn"
    reply = "Authorization: Basic ZmFrZS1jcmVkZW50aWFscw=="
    transcript = _codex_transcript(tmp_path / "codex.jsonl", turn_id, reply)
    _quiet_turn_end(monkeypatch)

    for _ in range(2):
        handle(HookEvent(
            harness="codex", event="turn-end", session_id="session-1",
            cwd=str(project), turn_id=turn_id, assistant_text=reply,
            transcript_ref=str(transcript),
        ), env, time.monotonic() + 8)

    rows = _rows(project / ".magnolia" / "transcripts" / "codex" / "session-1.jsonl")
    assert [row["text"] for row in rows if row["kind"] == "assistant"] == [
        "Authorization: Basic [REDACTED_SECRET]",
    ]


def test_second_stop_appends_only_newly_visible_block(tmp_path, monkeypatch):
    project, env = _workspace(tmp_path)
    turn_id = "late-block-turn"
    transcript = _codex_transcript(tmp_path / "codex.jsonl", turn_id, "first")
    _quiet_turn_end(monkeypatch)

    handle(HookEvent(
        harness="codex", event="turn-end", session_id="session-1",
        cwd=str(project), turn_id=turn_id, assistant_text="first",
        transcript_ref=str(transcript),
    ), env, time.monotonic() + 8)
    _codex_transcript(transcript, turn_id, "first", "second")
    handle(HookEvent(
        harness="codex", event="turn-end", session_id="session-1",
        cwd=str(project), turn_id=turn_id, assistant_text="second",
        transcript_ref=str(transcript),
    ), env, time.monotonic() + 8)

    rows = _rows(project / ".magnolia" / "transcripts" / "codex" / "session-1.jsonl")
    assert [row["text"] for row in rows if row["kind"] == "assistant"] == ["first", "second"]


def test_client_recovery_collapses_whitespace_for_comparison_only(tmp_path, monkeypatch):
    project, env = _workspace(tmp_path)
    turn_id = "whitespace-turn"
    transcript = _codex_transcript(tmp_path / "codex.jsonl", turn_id, "alpha\n\n  beta")
    _quiet_turn_end(monkeypatch)
    event = HookEvent(
        harness="codex", event="turn-end", session_id="session-1",
        cwd=str(project), turn_id=turn_id, assistant_text="  alpha beta  ",
        transcript_ref=str(transcript),
    )

    handle(event, env, time.monotonic() + 8)

    rows = _rows(project / ".magnolia" / "transcripts" / "codex" / "session-1.jsonl")
    assert [row["text"] for row in rows if row["kind"] == "assistant"] == ["  alpha beta  "]


def test_client_recovery_two_concurrent_stops_append_each_block_once(tmp_path, monkeypatch):
    from compchem_memory.hook import transcript as transcript_module

    project, env = _workspace(tmp_path)
    turn_id = "concurrent-turn"
    source = _codex_transcript(tmp_path / "codex.jsonl", turn_id, "first", "last")
    _quiet_turn_end(monkeypatch)
    original_read_turns = transcript_module.read_turns

    def delayed_read_turns(path):
        rows = original_read_turns(path)
        time.sleep(0.05)
        return rows

    monkeypatch.setattr(transcript_module, "read_turns", delayed_read_turns)

    def stop(_index):
        return handle(HookEvent(
            harness="codex", event="turn-end", session_id="session-1",
            cwd=str(project), turn_id=turn_id, assistant_text="last",
            transcript_ref=str(source),
        ), env, time.monotonic() + 8)

    with ThreadPoolExecutor(max_workers=2) as executor:
        list(executor.map(stop, range(2)))

    rows = _rows(project / ".magnolia" / "transcripts" / "codex" / "session-1.jsonl")
    assert [row["text"] for row in rows if row["kind"] == "assistant"] == ["first", "last"]
    with SessionState.open(project / ".magnolia", "codex", "session-1") as state:
        assert len(state.data["recovered_turns"]) == 1


def test_client_recovery_keeps_only_last_200_turn_keys(tmp_path, monkeypatch):
    project, env = _workspace(tmp_path)
    turn_id = "new-turn"
    source = _codex_transcript(tmp_path / "codex.jsonl", turn_id, "reply")
    _quiet_turn_end(monkeypatch)
    with SessionState.open(project / ".magnolia", "codex", "session-1") as state:
        state.data["recovered_turns"] = [
            ["session-1", f"old-{index}", None] for index in range(200)
        ]

    handle(HookEvent(
        harness="codex", event="turn-end", session_id="session-1",
        cwd=str(project), turn_id=turn_id, assistant_text="reply",
        transcript_ref=str(source),
    ), env, time.monotonic() + 8)

    with SessionState.open(project / ".magnolia", "codex", "session-1") as state:
        assert len(state.data["recovered_turns"]) == 200
        assert state.data["recovered_turns"][0] == ["session-1", "old-1", None]
        assert state.data["recovered_turns"][-1] == ["session-1", turn_id, None]


def test_non_vibe_recovery_does_not_fill_missing_assistant_text(tmp_path, monkeypatch):
    project, env = _workspace(tmp_path)
    turn_id = "missing-native-turn"
    source = _codex_transcript(tmp_path / "codex.jsonl", turn_id, "recovered")
    _quiet_turn_end(monkeypatch)
    event = HookEvent(
        harness="codex", event="turn-end", session_id="session-1",
        cwd=str(project), turn_id=turn_id, assistant_text=None,
        transcript_ref=str(source),
    )

    handle(event, env, time.monotonic() + 8)

    assert event.assistant_text is None
    rows = _rows(project / ".magnolia" / "transcripts" / "codex" / "session-1.jsonl")
    assert [row["text"] for row in rows if row["kind"] == "assistant"] == ["recovered"]


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


def _start(project: Path, env: dict[str, str], harness: str) -> str:
    event = _event(project, "session-start")
    event.harness = harness
    return handle(event, env, time.monotonic() + 8).response.context


def _boot_fixture(tmp_path: Path, boot: str) -> tuple[Path, dict[str, str]]:
    project, env = _workspace(tmp_path)
    (project / ".magnolia" / "boot-context.md").write_text(boot, encoding="utf-8")
    (tmp_path / "workspace" / "rules" / "r.md").write_text("RULE TEXT\n", encoding="utf-8")
    (tmp_path / "workspace" / "AGENTS.md").write_text("AGENTS TEXT\n", encoding="utf-8")
    return project, env


def test_claude_session_start_caps_dynamic_part_and_points(tmp_path, monkeypatch):
    from compchem_memory.hook.dispatch import CLAUDE_CONTEXT_LIMIT

    monkeypatch.delenv("MAGNOLIA_AUTORETRIEVE", raising=False)
    assert CLAUDE_CONTEXT_LIMIT == 1800
    project, env = _boot_fixture(tmp_path, "B" * 5000)
    context = _start(project, env, "claude")
    assert DIRECTIVE in context
    label = "--- boot-context.md ---\n"  # the section label counts toward the limit
    assert "B" * (1800 - len(label)) in context and "B" * (1801 - len(label)) not in context
    assert "RULE TEXT" not in context and "AGENTS TEXT" not in context
    assert (
        f"[Magnolia] The full project memory summary is in {project}/.magnolia/boot-context.md"
        " and audit-report.md: read both before your first task."
    ) in context


def test_claude_session_start_short_dynamic_part_has_no_pointer(tmp_path, monkeypatch):
    monkeypatch.delenv("MAGNOLIA_AUTORETRIEVE", raising=False)
    project, env = _boot_fixture(tmp_path, "short boot")
    context = _start(project, env, "claude")
    assert "short boot" in context and DIRECTIVE in context
    assert "The full project memory summary" not in context
    assert "RULE TEXT" not in context


def test_codex_session_start_full_payload(tmp_path, monkeypatch):
    monkeypatch.delenv("MAGNOLIA_AUTORETRIEVE", raising=False)
    project, env = _boot_fixture(tmp_path, "B" * 5000)
    context = _start(project, env, "codex")
    assert "B" * 5000 in context and "RULE TEXT" in context and DIRECTIVE in context
    assert "AGENTS TEXT" not in context


def test_vibe_session_start_has_agents_md_first(tmp_path, monkeypatch):
    monkeypatch.delenv("MAGNOLIA_AUTORETRIEVE", raising=False)
    project, env = _boot_fixture(tmp_path, "boot")
    context = _start(project, env, "vibe")
    assert "RULE TEXT" in context and DIRECTIVE in context
    assert context.index("AGENTS TEXT") < context.index("--- boot-context.md ---")


def test_vibe_turn_end_without_turn_id_takes_the_last_prompt_turn(tmp_path, monkeypatch):
    """Vibe's post_agent payload has no turn_id: the agent's last prompt supplies it."""
    from compchem_memory.hook import action_retrieval

    project, env = _workspace(tmp_path)
    turn_id = "turn-2c40443c7526b229e2076d661938b73f"
    child = AgentRef(id="child-1", type="reviewer")
    observed = []
    monkeypatch.setattr(
        action_retrieval, "probe_application",
        lambda paths, harness, session_id, reply, event_turn_id: observed.append(
            ("probe", event_turn_id)
        ),
    )
    monkeypatch.setattr(
        critic, "run_critic",
        lambda paths, harness, session_id, agent_id, event_turn_id: observed.append(
            ("critic", agent_id, event_turn_id)
        ),
    )
    common = dict(harness="vibe", session_id="session-1", cwd=str(project))
    handle(HookEvent(event="prompt", prompt="read note", turn_id=turn_id, **common),
           env, time.monotonic() + 8)
    handle(HookEvent(event="prompt", prompt="review", turn_id="turn-child", agent=child,
                     **common), env, time.monotonic() + 8)

    handle(HookEvent(event="turn-end", transcript_ref=str(CLIENT_TRANSCRIPTS / "vibe" / "session"),
                     **common), env, time.monotonic() + 8)
    handle(HookEvent(event="turn-end", agent=child, **common), env, time.monotonic() + 8)

    rows = _rows(project / ".magnolia" / "transcripts" / "vibe" / "session-1.jsonl")
    replies = [row for row in rows if row["kind"] == "assistant" and row["agent"] is None]
    assert replies and all(row["turn_id"] == turn_id for row in replies)
    assert observed == [
        ("probe", turn_id), ("critic", None, turn_id),
        ("probe", "turn-child"), ("critic", "child-1", "turn-child"),
    ]
