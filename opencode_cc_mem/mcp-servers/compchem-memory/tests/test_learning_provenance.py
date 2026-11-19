"""Learning provenance survives capture, deduplication, and confirmation."""

import json
from pathlib import Path

import yaml

from compchem_memory.consolidation import apply_merge, consolidate_project_findings
from compchem_memory.extraction import AutomaticMemoryExtractor
from compchem_memory.hook.dispatch import _record_session
from compchem_memory.hook.events import AgentRef, HookEvent, ToolCall
from compchem_memory.hook.project import ProjectPaths
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
    project = tmp_path / "project"
    store = ensure_project_store(str(project))
    claim_titles = {
        "claude": ("Orbital signal", "Joint telescope result", "Observatory context"),
        "codex": ("Docking energy", "Combined contact map", "Computation context"),
    }
    for client in ("claude", "codex"):
        session_id = f"{client}-mixed"
        record_session(store, client, session_id, str(project), None)
        for suffix in ("alpha", "beta"):
            tool = f"{client}_{suffix}"
            append_event(store, HookEvent(
                harness=client,
                event="post-tool",
                session_id=session_id,
                cwd=str(project),
                tool=ToolCall(
                    name=tool,
                    server=f"{client}-server",
                    raw_name=tool,
                    call_id=f"{session_id}-{suffix}",
                    input={},
                    output=f"{suffix} result",
                ),
            ))

    def distiller(transcript):
        client = "claude" if "tool:claude_alpha" in transcript else "codex"
        titles = claim_titles[client]
        return [
            {"title": titles[0], "content": "alpha",
             "type": "scientific_finding", "tools": [f"{client}_alpha"]},
            {"title": titles[1], "content": "alpha and beta",
             "type": "success_pattern",
             "tools": [f"{client}_alpha", f"{client}_beta"]},
            {"title": titles[2], "content": "session-wide",
             "type": "workflow_note"},
        ]

    saved = ingest_opencode_sessions(str(store), distiller=distiller)
    by_title = {_frontmatter(path)["title"]: _frontmatter(path) for path in saved}

    for client in ("claude", "codex"):
        session_id = f"{client}-mixed"
        titles = claim_titles[client]
        alpha = by_title[titles[0]]["origins"]
        mixed = by_title[titles[1]]["origins"]
        session = by_title[titles[2]]["origins"]
        assert alpha == [{
            "client": client,
            "session_id": session_id,
            "tool": f"{client}_alpha",
            "mcp_server": f"{client}-server",
        }]
        assert [origin["tool"] for origin in mixed] == [
            f"{client}_alpha", f"{client}_beta",
        ]
        assert session == [{"client": client, "session_id": session_id}]
        assert all("model" not in origin and "role" not in origin for origin in mixed)


def test_consolidation_cluster_merge_keeps_all_origins(tmp_path):
    store = ensure_project_store(str(tmp_path / "project"))
    staging = store / "staging"
    origins = [
        {"client": "claude", "session_id": "s1"},
        {"client": "codex", "session_id": "s2"},
    ]
    for index, origin in enumerate(origins, start=1):
        meta = {
            "title": f"Same finding {index}",
            "type": "scientific_finding",
            "confidence": 0.8,
            "opencode_session_id": origin["session_id"],
            "observed_in_sessions": [origin["session_id"]],
            "origins": [origin],
        }
        (staging / f"finding-{index}.md").write_text(
            "---\n" + yaml.safe_dump(meta) + "---\n\n"
            + ("long canonical body" if index == 1 else "short body") + "\n"
        )

    consolidate_project_findings(
        str(store),
        clusterer=lambda _: [{
            "ids": ["finding-1.md", "finding-2.md"],
            "confidence": 0.9,
            "rationale": "same claim",
        }],
    )
    artifact = json.loads(
        (store / "reflex" / "consolidation-proposal.json").read_text()
    )
    proposal = artifact["proposals"][0]
    assert proposal["merged_preview"]["origins"] == origins

    applied = apply_merge(proposal["sources"])
    assert _frontmatter(applied["merged"])["origins"] == origins


def test_dispatch_records_only_bound_model_and_role(tmp_path, monkeypatch):
    monkeypatch.delenv("MAGNOLIA_ROLE", raising=False)
    store = tmp_path / ".magnolia"
    store.mkdir()
    paths = ProjectPaths(tmp_path, tmp_path, store, "project", False)
    _record_session(paths, HookEvent(
        harness="codex",
        event="session-start",
        session_id="bound",
        cwd=str(tmp_path),
        model="gpt-bound",
        role="worker",
    ))
    _record_session(paths, HookEvent(
        harness="claude",
        event="session-start",
        session_id="unknown",
        cwd=str(tmp_path),
    ))

    rows = [
        json.loads(line)
        for line in (store / "agent-sessions.jsonl").read_text().splitlines()
    ]
    assert rows[0]["origin"] == {
        "client": "codex",
        "session_id": "bound",
        "model": "gpt-bound",
        "role": "worker",
    }
    assert rows[1]["origin"] == {
        "client": "claude",
        "session_id": "unknown",
    }


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
