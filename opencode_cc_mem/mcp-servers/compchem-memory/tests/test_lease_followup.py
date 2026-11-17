"""Regression coverage for the project-writer lease follow-up findings."""

from __future__ import annotations

import json
import threading
from pathlib import Path
from types import SimpleNamespace

import pytest


def _write_tool_event(path: Path, timestamp: str, tool: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({
        "timestamp": timestamp,
        "event_type": "tool_call",
        "tool": tool,
    }) + "\n")


def test_tool_event_sweep_retries_interleaved_session_before_sealing(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A different session moving the shared cursor must not lose learnings."""
    from compchem_memory import extraction, startup_scan

    project = tmp_path / "project"
    sessions = project / ".magnolia" / "sessions"
    sessions.mkdir(parents=True)
    (project / ".magnolia" / ".current-session-id").write_text("active")
    closed = sessions / "closed.jsonl"
    active = sessions / "active.jsonl"
    _write_tool_event(closed, "2026-11-17T01:00:00Z", "closed")
    _write_tool_event(active, "2026-11-17T02:00:00Z", "active")

    interleaved = False

    def generate(self, events):
        nonlocal interleaved
        tool = events[0]["tool"]
        if tool == "closed" and not interleaved:
            interleaved = True
            with active.open("a") as stream:
                stream.write(json.dumps({
                    "timestamp": "2026-11-17T02:05:00Z",
                    "event_type": "tool_call",
                    "tool": "active",
                }) + "\n")
            worker = threading.Thread(
                target=lambda: extraction.AutomaticMemoryExtractor(str(project)).commit(
                    active, str(project),
                )
            )
            worker.start()
            worker.join(5)
            assert not worker.is_alive()
        return [{
            "title": f"learning from {tool}",
            "content": f"learned in {tool}",
            "type": "note",
            "tags": [tool],
        }]

    monkeypatch.setattr(
        extraction.AutomaticMemoryExtractor,
        "_generate_candidates",
        generate,
    )

    first = startup_scan._distill_tool_events(project)
    assert first["skipped"] == 1
    assert not closed.with_suffix(".distilled").exists()
    assert not active.with_suffix(".distilled").exists()

    startup_scan._distill_tool_events(project)

    staged = "\n".join(
        path.read_text() for path in (project / ".magnolia" / "staging").glob("*.md")
    )
    assert "learned in closed" in staged
    assert "learned in active" in staged
    assert closed.with_suffix(".distilled").exists()
    assert not active.with_suffix(".distilled").exists()


def test_manual_dialogue_distill_reports_concurrent_skip(
    tmp_path: Path,
) -> None:
    from compchem_memory.opencode_ingest import distill_session_transcript

    store = tmp_path / ".magnolia"
    store.mkdir()
    record = store / "opencode-distilled" / "session.json"
    export = {
        "messages": [{
            "info": {"id": "m1", "role": "user"},
            "parts": [{"type": "text", "text": "remember this"}],
        }]
    }

    def concurrent_distiller(_transcript: str) -> list[dict]:
        record.parent.mkdir(parents=True, exist_ok=True)
        record.write_text(json.dumps({"cursor": "other", "candidates": 1}))
        return [{"title": "candidate", "content": "content", "type": "note"}]

    result = distill_session_transcript(
        str(store),
        "session",
        commit=True,
        exporter=lambda _sid: export,
        distiller=concurrent_distiller,
    )

    assert result == {
        "status": "skipped",
        "reason": "concurrent write",
        "retry": True,
        "saved": [],
    }
    assert json.loads(record.read_text())["cursor"] == "other"
    assert list((store / "staging").glob("*.md")) == []


@pytest.mark.parametrize("tool_name", [
    "memory_extract_from_session",
    "memory_distill_session",
])
def test_user_forced_tool_event_skip_is_reported(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    tool_name: str,
) -> None:
    from compchem_memory import server

    project = tmp_path / "project"
    session = project / ".magnolia" / "sessions" / "active.jsonl"
    _write_tool_event(session, "2026-11-17T02:00:00Z", "active")

    class SessionManager:
        def get_session_log_path(self):
            return str(session)

    class Extractor:
        def should_extract(self, _path):
            return True

        def commit(self, _path, _project):
            return None

    commits: list[str] = []
    monkeypatch.setattr(server, "_get_session_mgr", lambda _pd: SessionManager())
    monkeypatch.setattr(server, "_get_extractor", lambda _pd: Extractor())
    monkeypatch.setattr(server, "_safe_version_commit", lambda _store, message: commits.append(message))
    monkeypatch.setattr(server, "is_llm_available", lambda: False)

    tool = getattr(server, tool_name)
    payload = json.loads(getattr(tool, "fn", tool)(
        **({"commit": True} if tool_name == "memory_distill_session" else {}),
        project_dir=str(project),
    ))

    assert payload["status"] == "skipped"
    assert payload["reason"] == "concurrent write"
    assert payload["retry"] is True
    assert commits == []


def test_user_confirmed_actions_remain_whole_call_leased() -> None:
    from compchem_memory.tool_catalog import memory_tool_requires_lease

    for name in (
        "memory_confirm",
        "memory_apply_consolidation",
        "memory_apply_promotions",
    ):
        assert memory_tool_requires_lease(name) is True


def test_consolidation_fingerprints_before_loading_findings(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from compchem_memory import consolidation

    store = tmp_path / ".magnolia"
    (store / "staging").mkdir(parents=True)
    (store / "reflex").mkdir()
    order: list[str] = []
    monkeypatch.setattr(
        consolidation,
        "file_fingerprint",
        lambda _paths: order.append("fingerprint") or {},
    )
    monkeypatch.setattr(
        consolidation,
        "_load_findings",
        lambda *_args: order.append("read") or [],
    )

    consolidation.consolidate_project_findings(str(store), clusterer=lambda _entries: [])

    assert order[:2] == ["fingerprint", "read"]


def test_promotion_fingerprints_before_loading_entries(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from compchem_memory import promotion

    store = tmp_path / ".magnolia"
    (store / "entries").mkdir(parents=True)
    (store / "reflex").mkdir()
    rules = tmp_path / "rules"
    rules.mkdir()
    order: list[str] = []
    monkeypatch.setattr(
        promotion,
        "file_fingerprint",
        lambda _paths: order.append("fingerprint") or {},
    )
    monkeypatch.setattr(
        promotion,
        "eligible_entries",
        lambda _store: order.append("read") or [],
    )

    promotion.propose_promotions(str(store), rules_dir=str(rules))

    assert order[:2] == ["fingerprint", "read"]


def test_handover_fingerprints_before_reading_state(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from compchem_memory import handover

    project = tmp_path / "project"
    store = project / ".magnolia"
    transcript = store / "transcripts" / "claude" / "session.jsonl"
    transcript.parent.mkdir(parents=True)
    transcript.write_text(json.dumps({"kind": "prompt", "text": "work"}) + "\n")
    (store / "agent-sessions.jsonl").write_text(json.dumps({
        "ts": "2026-11-17T01:00:00Z",
        "harness": "claude",
        "session_id": "session",
    }) + "\n")
    state = store / handover.HANDOVER_STATE_FILE
    state.write_text("## To do\n- old\n")
    order: list[str] = []
    original_read_text = Path.read_text

    def read_text(path: Path, *args, **kwargs):
        if path == state:
            order.append("read")
        return original_read_text(path, *args, **kwargs)

    monkeypatch.setattr(Path, "read_text", read_text)
    monkeypatch.setattr(
        handover,
        "file_fingerprint",
        lambda _paths: order.append("fingerprint") or {},
    )

    handover.generate_handover(str(project), llm=lambda *_args, **_kwargs: None)

    assert order[:2] == ["fingerprint", "read"]


def test_writable_store_refuses_write_when_lock_cannot_open(
    tmp_path: Path,
) -> None:
    from compchem_memory import writer_lease

    project = tmp_path / "project"
    store = project / ".magnolia"
    store.mkdir(parents=True)
    lock = store / ".writer.lock"
    lock.write_text("")
    lock.chmod(0)
    entered = False
    try:
        with pytest.raises(PermissionError):
            with writer_lease.project_writer_lease(project):
                entered = True
    finally:
        lock.chmod(0o600)

    assert entered is False


def test_read_only_store_still_allows_read_only_boot_path(tmp_path: Path) -> None:
    from compchem_memory.writer_lease import project_writer_lease

    project = tmp_path / "project"
    store = project / ".magnolia"
    store.mkdir(parents=True)
    store.chmod(0o555)
    try:
        with project_writer_lease(project):
            observed = "booted"
    finally:
        store.chmod(0o755)

    assert observed == "booted"


def test_boot_context_recomputes_when_a_source_changes(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from compchem_memory import boot_context

    project = tmp_path / "project"
    store = project / ".magnolia"
    store.mkdir(parents=True)
    goal = store / "GOAL.md"
    goal.write_text("old view")
    calls = 0

    def assemble_context(**_kwargs):
        nonlocal calls
        calls += 1
        content = goal.read_text()
        if calls == 1:
            goal.write_text("new view")
        return SimpleNamespace(content=content, sources=[], tokens_used=2)

    monkeypatch.setattr(boot_context, "assemble_context", assemble_context)

    boot_context.regenerate_boot_context(str(project))

    assert calls == 2
    assert (store / "boot-context.md").read_text() == "new view"
