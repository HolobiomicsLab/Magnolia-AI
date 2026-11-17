"""Provider failures keep every distillation path retryable."""

from __future__ import annotations

import json
import hashlib
from pathlib import Path
from unittest.mock import MagicMock

import httpx
import pytest
import yaml

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
        + "\n"
        + json.dumps({
            "timestamp": "2026-11-17T08:30:38Z",
            "event_type": "tool_success",
            "tool": "synthetic-tool",
            "result_summary": "synthetic result",
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
    retry = yaml.safe_load(
        (project / "extraction-state.yaml").read_text()
    )["session_retries"]["closed.jsonl"]
    assert retry["attempts"] == 1
    assert retry["exhausted"] is False
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
    assert "2026-11-17T08:30:38Z" in state
    assert distill_log.drain_distill_notices(str(project)) == []


@pytest.mark.parametrize(
    "rows",
    (
        [],
        [{"timestamp": "2026-11-17T08:30:37Z", "event_type": "session_start"}],
    ),
)
def test_closed_session_without_extractable_events_seals_without_llm(
    tmp_path: Path, monkeypatch, rows,
) -> None:
    project = tmp_path / "project"
    session = project / ".magnolia" / "sessions" / "empty.jsonl"
    session.parent.mkdir(parents=True)
    session.write_text(
        "".join(json.dumps(row) + "\n" for row in rows), encoding="utf-8"
    )
    monkeypatch.setattr(
        "compchem_memory.extraction.AutomaticMemoryExtractor._generate_candidates",
        lambda *_args: pytest.fail("empty session reached candidate generation"),
    )

    result = startup_scan._distill_tool_events(project)

    assert result == {
        "mode": "tool_event",
        "scanned": 1,
        "distilled": 1,
        "skipped": 0,
        "opencode_ingested": 0,
    }
    assert session.with_suffix(".distilled").is_file()


def test_tool_event_provider_failure_backs_off_per_session(
    tmp_path: Path, monkeypatch,
) -> None:
    project = tmp_path / "project"
    _tool_event_session(project)
    calls = []
    monkeypatch.setattr("compchem_memory.extraction.is_llm_available", lambda: True)
    monkeypatch.setattr(
        "compchem_memory.extraction.AutomaticMemoryExtractor._llm_distill",
        lambda _self, events: calls.append(events),
    )

    startup_scan._distill_tool_events(project)
    startup_scan._distill_tool_events(project)

    assert len(calls) == 1
    state = yaml.safe_load((project / "extraction-state.yaml").read_text())
    retry = state["session_retries"]["closed.jsonl"]
    assert retry["attempts"] == 1
    assert retry["exhausted"] is False
    assert retry["next_retry_at"]


def test_tool_event_provider_failure_stops_at_cap_unsealed_and_reported(
    tmp_path: Path, monkeypatch,
) -> None:
    project = tmp_path / "project"
    session = _tool_event_session(project)
    (project / "extraction-state.yaml").write_text(
        yaml.safe_dump({
            "last_cursor": "",
            "session_retries": {
                "closed.jsonl": {
                    "attempts": 4,
                    "next_retry_at": "2026-01-01T00:00:00+00:00",
                    "exhausted": False,
                    "fingerprint": hashlib.sha256(session.read_bytes()).hexdigest(),
                }
            },
        }),
        encoding="utf-8",
    )
    calls = []
    monkeypatch.setattr("compchem_memory.extraction.is_llm_available", lambda: True)
    monkeypatch.setattr(
        "compchem_memory.extraction.AutomaticMemoryExtractor._llm_distill",
        lambda _self, events: calls.append(events),
    )

    startup_scan._distill_tool_events(project)
    startup_scan._distill_tool_events(project)

    assert len(calls) == 1
    assert not session.with_suffix(".distilled").exists()
    retry = yaml.safe_load(
        (project / "extraction-state.yaml").read_text()
    )["session_retries"]["closed.jsonl"]
    assert retry == {
        "attempts": 5,
        "next_retry_at": None,
        "exhausted": True,
        "fingerprint": hashlib.sha256(session.read_bytes()).hexdigest(),
    }
    notices = distill_log.drain_distill_notices(str(project))
    assert len(notices) == 2
    assert "magnolia memory retry closed.jsonl" in notices[-1]


def test_new_tool_event_activity_rearms_exhausted_retry(
    tmp_path: Path, monkeypatch,
) -> None:
    project = tmp_path / "project"
    session = _tool_event_session(project)
    old_fingerprint = hashlib.sha256(session.read_bytes()).hexdigest()
    (project / "extraction-state.yaml").write_text(
        yaml.safe_dump({
            "last_cursor": "",
            "session_retries": {
                "closed.jsonl": {
                    "attempts": 5,
                    "next_retry_at": None,
                    "exhausted": True,
                    "fingerprint": old_fingerprint,
                }
            },
        }),
        encoding="utf-8",
    )
    with session.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps({
            "timestamp": "2026-11-17T08:31:00Z",
            "event_type": "tool_success",
            "tool": "new-tool",
            "result_summary": "new result",
        }) + "\n")
    calls = []
    monkeypatch.setattr("compchem_memory.extraction.is_llm_available", lambda: True)
    monkeypatch.setattr(
        "compchem_memory.extraction.AutomaticMemoryExtractor._llm_distill",
        lambda _self, events: calls.append(events),
    )

    startup_scan._distill_tool_events(project)

    assert len(calls) == 1
    retry = yaml.safe_load(
        (project / "extraction-state.yaml").read_text()
    )["session_retries"]["closed.jsonl"]
    assert retry["attempts"] == 1
    assert retry["exhausted"] is False


def test_dialogue_provider_failure_keeps_progress_retryable_and_notices_once(
    tmp_path: Path, monkeypatch,
) -> None:
    project = tmp_path / "project"
    store = _hook_session(project)
    _configure_failing_provider(monkeypatch, project)

    assert opencode_ingest.ingest_opencode_sessions(str(store)) == []
    assert opencode_ingest.ingest_opencode_sessions(str(store)) == []

    progress = json.loads(
        (store / "opencode-distilled" / "claude-dialogue.json").read_text()
    )
    assert progress["done"] is False
    assert progress["retry"]["attempts"] == 1
    notices = distill_log.drain_distill_notices(str(project))
    assert len(notices) == 1
    assert "retry" in notices[0].lower()


def test_dialogue_provider_failure_persists_backoff_without_retry_storm(
    tmp_path: Path,
) -> None:
    project = tmp_path / "project"
    store = _hook_session(project)
    calls = []

    def fail_distillation(transcript):
        calls.append(transcript)
        return None

    assert opencode_ingest.ingest_opencode_sessions(
        str(store), distiller=fail_distillation
    ) == []
    assert opencode_ingest.ingest_opencode_sessions(
        str(store), distiller=fail_distillation
    ) == []

    assert len(calls) == 1
    progress = json.loads(
        (store / "opencode-distilled" / "claude-dialogue.json").read_text()
    )
    assert progress["done"] is False
    assert progress["retry"]["attempts"] == 1
    assert progress["retry"]["exhausted"] is False
    assert progress["retry"]["next_retry_at"]


def test_manual_dialogue_distill_bypasses_exhausted_retry(tmp_path: Path) -> None:
    store = tmp_path / ".magnolia"
    marker = store / "opencode-distilled" / "session.json"
    marker.parent.mkdir(parents=True)
    marker.write_text(json.dumps({
        "cursor": None,
        "candidates": 0,
        "done": False,
        "retry": {"attempts": 5, "next_retry_at": None, "exhausted": True},
    }) + "\n")
    export = {
        "messages": [{
            "info": {"id": "m1", "role": "user"},
            "parts": [{"type": "text", "text": "new evidence"}],
        }],
    }

    result = opencode_ingest.distill_session_transcript(
        str(store), "session", commit=True, force_retry=True,
        exporter=lambda _sid: export, distiller=lambda _text: [],
    )

    assert result == {"status": "committed", "saved": []}
    assert "retry" not in json.loads(marker.read_text())


def test_success_does_not_erase_other_extractors_retry_state(
    tmp_path: Path, monkeypatch,
) -> None:
    from compchem_memory.extraction import AutomaticMemoryExtractor

    project = tmp_path / "project"
    first = _tool_event_session(project)
    second = first.with_name("second.jsonl")
    second.write_text(first.read_text().replace("closed", "second"))
    stale = AutomaticMemoryExtractor(str(project))
    failing = AutomaticMemoryExtractor(str(project))
    monkeypatch.setattr(failing, "_generate_candidates", lambda _events: None)
    monkeypatch.setattr(stale, "_generate_candidates", lambda _events: [])

    assert failing.commit(first, str(project)) is None
    assert stale.commit(second, str(project)) == []

    retries = yaml.safe_load(
        (project / "extraction-state.yaml").read_text()
    )["session_retries"]
    assert retries["closed.jsonl"]["attempts"] == 1
