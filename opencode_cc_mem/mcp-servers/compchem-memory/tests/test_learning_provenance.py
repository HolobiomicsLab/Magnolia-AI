"""Learning provenance survives capture, deduplication, and confirmation."""

import json
from pathlib import Path

import yaml

from compchem_memory.extraction import AutomaticMemoryExtractor
from compchem_memory.hook.events import AgentRef, HookEvent, ToolCall
from compchem_memory.hook.sessions import record_session
from compchem_memory.hook.transcript import append_event
from compchem_memory.opencode_ingest import ingest_opencode_sessions
from compchem_memory.staging_io import save_candidate
from compchem_memory.storage import ensure_project_store
from compchem_memory.tiers.project import ProjectManager


def _frontmatter(path: str | Path) -> dict:
    return yaml.safe_load(Path(path).read_text().split("---", 2)[1])


def test_staged_learning_records_origin(tmp_path):
    project = tmp_path / "project"
    store = ensure_project_store(str(project))
    event = HookEvent(
        harness="claude",
        event="post-tool",
        session_id="ses-1",
        cwd=str(project),
        agent=AgentRef("agent-7", "researcher"),
        tool=ToolCall(
            name="run_shell",
            server="compchem-tools",
            raw_name="mcp__compchem-tools__run_shell",
            call_id="call-1",
            input={"cmd": "analyse"},
            output="score=-82.4",
        ),
        model="claude-sonnet-5",
        role="worker",
    )
    record_session(store, "claude", "ses-1", str(project), None)
    append_event(store, event)

    saved = ingest_opencode_sessions(
        str(store),
        distiller=lambda _: [{
            "title": "Observed docking score",
            "content": "score=-82.4",
            "type": "scientific_finding",
            "tools": ["run_shell"],
        }],
    )

    assert _frontmatter(saved[0])["origins"] == [{
        "client": "claude",
        "session_id": "ses-1",
        "agent_id": "agent-7",
        "agent_type": "researcher",
        "tool": "run_shell",
        "mcp_server": "compchem-tools",
        "model": "claude-sonnet-5",
        "role": "worker",
    }]


def test_tool_event_candidate_preserves_server_and_unknown_client(tmp_path, monkeypatch):
    project = tmp_path / "project"
    store = ensure_project_store(str(project))
    session = store / "sessions" / "captured.jsonl"
    events = [
        {"event_type": "session_start", "session_id": "captured"},
        {"event_type": "tool_error", "session_id": "captured",
         "source": "member-analysis", "tool": "score_pose", "error": "bad pose"},
    ]
    session.write_text("\n".join(json.dumps(event) for event in events) + "\n")
    monkeypatch.setattr("compchem_memory.extraction.is_llm_available", lambda: False)

    saved = AutomaticMemoryExtractor(str(project)).commit(session, str(project))
    origin = _frontmatter(saved[0])["origins"][0]

    assert origin == {
        "session_id": "captured",
        "tool": "score_pose",
        "mcp_server": "member-analysis",
    }
    assert "client" not in origin


def test_mixed_and_unknown_origins_remain_honest(tmp_path):
    store = ensure_project_store(str(tmp_path / "project"))
    candidate = {
        "title": "HADDOCK result",
        "content": "score=-82.4",
        "tools": ["haddock3"],
        "origins": [
            {"client": "codex", "session_id": "mixed", "tool": "haddock3",
             "mcp_server": "compchem-tools"},
            {"session_id": "manual-only"},
        ],
    }

    path = save_candidate(store, candidate, source="opencode_distill")
    origins = _frontmatter(path)["origins"]

    assert origins[0]["tool"] == "haddock3"
    assert origins[1] == {"session_id": "manual-only"}
    assert all("model" not in origin and "role" not in origin for origin in origins)


def test_dedup_unions_origins_without_losing_evidence(tmp_path):
    project = tmp_path / "project"
    store = ensure_project_store(str(project))
    first = save_candidate(store, {
        "title": "Stable docking score",
        "content": "score=-82.4",
        "type": "scientific_finding",
        "tags": ["docking"],
        "origins": [{"client": "claude", "session_id": "s1"}],
    }, source="opencode_distill", opencode_session_id="s1")
    second = save_candidate(store, {
        "title": "Stable docking score",
        "content": "replicate score=-82.1",
        "type": "scientific_finding",
        "tags": ["docking"],
        "origins": [{"client": "codex", "session_id": "s2"}],
    }, source="opencode_distill", opencode_session_id="s2")

    assert second == first
    meta = _frontmatter(first)
    assert meta["origins"] == [
        {"client": "claude", "session_id": "s1"},
        {"client": "codex", "session_id": "s2"},
    ]
    assert "replicate score=-82.1" in Path(first).read_text()


def test_confirmation_displays_and_retains_origins(tmp_path):
    project = tmp_path / "project"
    store = ensure_project_store(str(project))
    staged = save_candidate(store, {
        "title": "Confirmed provenance",
        "content": "measured result",
        "origins": [{"client": "vibe", "session_id": "v1", "role": "worker"}],
    }, source="opencode_distill")
    manager = ProjectManager(Path.home() / ".magnolia")

    shown = manager.search_staging(str(project), keyword="measured")
    promoted = manager.confirm_staging(str(project), Path(staged).name)

    assert shown[0]["origins"] == [
        {"client": "vibe", "session_id": "v1", "role": "worker"}
    ]
    assert _frontmatter(promoted)["origins"] == shown[0]["origins"]


def test_member_mcp_origin_not_excluded(tmp_path):
    project = tmp_path / "project"
    store = ensure_project_store(str(project))
    record_session(store, "codex", "member-session", str(project), None)
    append_event(store, HookEvent(
        harness="codex",
        event="post-tool",
        session_id="member-session",
        cwd=str(project),
        tool=ToolCall(
            name="assay",
            server="member-lab",
            raw_name="mcp__member_lab__assay",
            call_id="call-member",
            input={},
            output="active",
        ),
    ))

    saved = ingest_opencode_sessions(
        str(store),
        distiller=lambda _: [{
            "title": "Member assay",
            "content": "active",
            "tools": ["assay"],
        }],
    )

    assert _frontmatter(saved[0])["origins"] == [{
        "client": "codex",
        "session_id": "member-session",
        "tool": "assay",
        "mcp_server": "member-lab",
    }]
