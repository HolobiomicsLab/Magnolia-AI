"""Provider failures keep every distillation path retryable."""

from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import MagicMock

import httpx

from compchem_memory import distill_log, llm, opencode_ingest, startup_scan
from compchem_memory.storage import ensure_project_store


def _configure_failing_provider(monkeypatch, project: Path) -> None:
    monkeypatch.setenv("DEEPSEEK_API_KEY", "test-key")
    monkeypatch.setenv("MAGNOLIA_PROJECT_DIR", str(project))

    def fail_connection(*args, **kwargs):
        raise httpx.ConnectError("simulated provider outage")

    monkeypatch.setattr(llm.httpx, "post", fail_connection)


def _tool_event_session(project: Path) -> Path:
    session = project / ".magnolia" / "sessions" / "closed.jsonl"
    session.parent.mkdir(parents=True, exist_ok=True)
    session.write_text(
        json.dumps({
            "timestamp": "2026-11-17T08:30:37Z",
            "event_type": "session_start",
            "session_id": "closed",
        })
        + "\n",
        encoding="utf-8",
    )
    return session


def _hook_session(project: Path) -> Path:
    store = ensure_project_store(str(project))
    (store / "agent-sessions.jsonl").write_text(
        json.dumps({
            "ts": "2026-11-17T08:30:37Z",
            "harness": "claude",
            "session_id": "dialogue",
        })
        + "\n",
        encoding="utf-8",
    )
    transcript = store / "transcripts" / "claude" / "dialogue.jsonl"
    transcript.parent.mkdir(parents=True, exist_ok=True)
    transcript.write_text(
        json.dumps({"kind": "prompt", "text": "Remember this result"}) + "\n"
        + json.dumps({"kind": "session-end"})
        + "\n",
        encoding="utf-8",
    )
    return store


def test_startup_provider_failure_keeps_tool_event_session_retryable(
    tmp_path: Path, monkeypatch,
) -> None:
    project = tmp_path / "project"
    session = _tool_event_session(project)
    _configure_failing_provider(monkeypatch, project)

    result = startup_scan._distill_tool_events(project)

    assert result == {
        "mode": "tool_event",
        "scanned": 1,
        "distilled": 0,
        "skipped": 1,
        "opencode_ingested": 0,
    }
    assert not session.with_suffix(".distilled").exists()
    assert not (project / "extraction-state.yaml").exists()
    notices = distill_log.drain_distill_notices(str(project))
    assert len(notices) == 1
    assert "retry" in notices[0].lower()


def test_successful_zero_candidate_tool_event_extraction_seals(
    tmp_path: Path, monkeypatch,
) -> None:
    project = tmp_path / "project"
    session = _tool_event_session(project)
    monkeypatch.setenv("DEEPSEEK_API_KEY", "test-key")
    monkeypatch.setenv("MAGNOLIA_PROJECT_DIR", str(project))
    response = MagicMock()
    response.raise_for_status.return_value = None
    response.json.return_value = {
        "choices": [{"message": {"content": "[]"}, "finish_reason": "stop"}]
    }
    monkeypatch.setattr(llm.httpx, "post", lambda *args, **kwargs: response)

    result = startup_scan._distill_tool_events(project)

    assert result["distilled"] == 1
    assert session.with_suffix(".distilled").is_file()
    state = (project / "extraction-state.yaml").read_text(encoding="utf-8")
    assert "2026-11-17T08:30:37Z" in state
    assert distill_log.drain_distill_notices(str(project)) == []


def test_dialogue_provider_failure_keeps_progress_retryable_and_notices_once(
    tmp_path: Path, monkeypatch,
) -> None:
    project = tmp_path / "project"
    store = _hook_session(project)
    _configure_failing_provider(monkeypatch, project)

    assert opencode_ingest.ingest_opencode_sessions(str(store)) == []
    assert opencode_ingest.ingest_opencode_sessions(str(store)) == []

    assert not (store / "opencode-distilled" / "claude-dialogue.json").exists()
    notices = distill_log.drain_distill_notices(str(project))
    assert len(notices) == 1
    assert "retry" in notices[0].lower()
