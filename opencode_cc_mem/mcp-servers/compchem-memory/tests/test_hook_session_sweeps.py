"""Sustainable sweeps for append-only hook transcripts."""

from __future__ import annotations

import io
import json
import os
import time
from pathlib import Path

import pytest

from compchem_memory import handover, opencode_ingest
from compchem_memory.storage import ensure_project_store
from compchem_memory.transcript_source import (
    SESSION_FINGERPRINT_KEY,
    hook_session_closed,
    list_sessions,
)


@pytest.fixture
def store(tmp_path: Path) -> Path:
    ensure_project_store(str(tmp_path))
    return tmp_path / ".magnolia"


def _write_jsonl(path: Path, rows: list[dict]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        "".join(json.dumps(row) + "\n" for row in rows),
        encoding="utf-8",
    )


def _record_hook_session(
    store: Path,
    session_id: str,
    rows: list[dict],
    *,
    harness: str = "claude",
) -> Path:
    sessions = store / "agent-sessions.jsonl"
    with sessions.open("a", encoding="utf-8") as stream:
        stream.write(json.dumps({
            "ts": "2026-11-15T09:00:00Z",
            "harness": harness,
            "session_id": session_id,
        }) + "\n")
    path = store / "transcripts" / harness / f"{session_id}.jsonl"
    _write_jsonl(path, rows)
    return path


def _candidate(transcript: str) -> list[dict]:
    return [{"title": "finding", "content": transcript, "type": "note"}]


def _merge_handover(system: str, user: str, **kwargs) -> str:
    return "## Done\n- merged hook session\n"


def _transcript_opens(monkeypatch: pytest.MonkeyPatch, store: Path) -> list[Path]:
    opened: list[Path] = []
    original = io.open
    transcript_root = store / "transcripts"

    def tracking_open(file, *args, **kwargs):
        try:
            path = Path(file)
            path.relative_to(transcript_root)
        except (TypeError, ValueError):
            pass
        else:
            opened.append(path)
        return original(file, *args, **kwargs)

    monkeypatch.setattr(io, "open", tracking_open)
    return opened


def test_session_end_last_row_closes_hook_session(store: Path) -> None:
    _record_hook_session(store, "ended", [
        {"kind": "prompt", "text": "work"},
        {"kind": "session-end"},
    ])
    ref = list_sessions(store)[0]

    assert hook_session_closed(store, ref) is True


def test_session_end_before_blank_tail_closes_hook_session(store: Path) -> None:
    path = _record_hook_session(store, "ended-blank", [
        {"kind": "prompt", "text": "work"},
        {"kind": "session-end"},
    ])
    with path.open("a", encoding="utf-8") as stream:
        stream.write("  \n\n")

    assert hook_session_closed(store, list_sessions(store)[0]) is True


def test_recent_codex_session_closes_only_by_idle_time(store: Path) -> None:
    _record_hook_session(
        store,
        "codex-thread",
        [{"kind": "prompt", "text": "work"}, {"kind": "session-end"}],
        harness="codex",
    )
    ref = list_sessions(store)[0]

    assert hook_session_closed(store, ref) is False


def test_idle_close_default_override_and_invalid_fallback(
    store: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    path = _record_hook_session(store, "idle", [{"kind": "prompt", "text": "work"}])
    ref = list_sessions(store)[0]
    now_ns = time.time_ns()
    two_hours_ago = (now_ns - 2 * 60 * 60 * 1_000_000_000) / 1_000_000_000
    os.utime(path, (two_hours_ago, two_hours_ago))

    assert hook_session_closed(store, ref, now_ns=now_ns) is False
    monkeypatch.setenv("MAGNOLIA_SESSION_IDLE_CLOSE_HOURS", "1")
    assert hook_session_closed(store, ref, now_ns=now_ns) is True
    monkeypatch.setenv("MAGNOLIA_SESSION_IDLE_CLOSE_HOURS", "3")
    assert hook_session_closed(store, ref, now_ns=now_ns) is False

    twenty_five_hours_ago = (now_ns - 25 * 60 * 60 * 1_000_000_000) / 1_000_000_000
    os.utime(path, (twenty_five_hours_ago, twenty_five_hours_ago))
    monkeypatch.setenv("MAGNOLIA_SESSION_IDLE_CLOSE_HOURS", "invalid")
    assert hook_session_closed(store, ref, now_ns=now_ns) is True
    monkeypatch.setenv("MAGNOLIA_SESSION_IDLE_CLOSE_HOURS", "-1")
    assert hook_session_closed(store, ref, now_ns=now_ns) is True


@pytest.mark.parametrize("value", ["0", "nan", "inf", "-inf", "-1"])
def test_non_positive_or_non_finite_idle_override_uses_default(
    store: Path,
    monkeypatch: pytest.MonkeyPatch,
    value: str,
) -> None:
    path = _record_hook_session(store, f"invalid-{value}", [
        {"kind": "prompt", "text": "work"},
    ])
    ref = list_sessions(store)[0]
    now_ns = time.time_ns()
    two_hours_ago = (now_ns - 2 * 60 * 60 * 1_000_000_000) / 1_000_000_000
    os.utime(path, (two_hours_ago, two_hours_ago))
    monkeypatch.setenv("MAGNOLIA_SESSION_IDLE_CLOSE_HOURS", value)

    assert hook_session_closed(store, ref, now_ns=now_ns) is False


def test_many_closed_unchanged_sessions_open_no_transcripts(
    store: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    for index in range(20):
        _record_hook_session(store, f"closed-{index}", [
            {"kind": "prompt", "text": f"work {index}"},
            {"kind": "assistant", "text": f"done {index}"},
            {"kind": "session-end"},
        ])

    opencode_ingest.ingest_opencode_sessions(str(store), distiller=_candidate)
    handover.generate_handover(str(store.parent), llm=_merge_handover)

    opened = _transcript_opens(monkeypatch, store)
    assert opencode_ingest.ingest_opencode_sessions(
        str(store), distiller=_candidate,
    ) == []
    assert handover.generate_handover(str(store.parent), llm=_merge_handover) is None
    assert opened == []


def test_grown_closed_session_reopens_and_distills_new_content(store: Path) -> None:
    path = _record_hook_session(store, "grown", [
        {"kind": "prompt", "text": "old work"},
        {"kind": "assistant", "text": "old result"},
        {"kind": "session-end"},
    ])
    opencode_ingest.ingest_opencode_sessions(str(store), distiller=_candidate)

    with path.open("a", encoding="utf-8") as stream:
        stream.write(json.dumps({"kind": "prompt", "text": "new work"}) + "\n")
        stream.write(json.dumps({"kind": "assistant", "text": "new result"}) + "\n")
    seen: list[str] = []

    def capture(transcript: str) -> list[dict]:
        seen.append(transcript)
        return _candidate(transcript)

    saved = opencode_ingest.ingest_opencode_sessions(str(store), distiller=capture)

    assert saved
    assert "new work" in seen[-1] and "new result" in seen[-1]
    assert "old work" not in seen[-1]
    record = json.loads(
        (store / "opencode-distilled" / "claude-grown.json").read_text()
    )
    assert record["closed"] is False


def test_failed_distillation_stores_no_fingerprint(store: Path) -> None:
    _record_hook_session(store, "failure", [
        {"kind": "prompt", "text": "retry me"},
        {"kind": "session-end"},
    ])

    opencode_ingest.ingest_opencode_sessions(str(store), distiller=lambda transcript: None)

    record_path = store / "opencode-distilled" / "claude-failure.json"
    assert not record_path.exists() or SESSION_FINGERPRINT_KEY not in json.loads(
        record_path.read_text()
    )


def test_old_cursor_without_fingerprint_is_read_then_upgraded(
    store: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _record_hook_session(store, "legacy-cursor", [
        {"kind": "prompt", "text": "already covered"},
        {"kind": "session-end"},
    ])
    marker = store / "opencode-distilled" / "claude-legacy-cursor.json"
    marker.parent.mkdir(parents=True, exist_ok=True)
    marker.write_text(json.dumps({
        "cursor": "claude-legacy-cursor:0",
        "candidates": 1,
        "done": False,
        "updated": "old",
    }) + "\n")
    opened = _transcript_opens(monkeypatch, store)

    opencode_ingest.ingest_opencode_sessions(str(store), distiller=_candidate)

    assert opened
    assert SESSION_FINGERPRINT_KEY in json.loads(marker.read_text())


def test_manual_commit_uses_distillation_fingerprint_without_reopening(
    store: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _record_hook_session(store, "manual", [
        {"kind": "prompt", "text": "manual work"},
        {"kind": "session-end"},
    ])
    ref = list_sessions(store)[0]
    first = opencode_ingest.distill_session_transcript(
        str(store), ref, commit=True, distiller=_candidate,
    )
    assert first and first["status"] == "committed"
    opened = _transcript_opens(monkeypatch, store)

    second = opencode_ingest.distill_session_transcript(
        str(store), ref, commit=True, distiller=_candidate,
    )

    assert second == {"status": "committed", "saved": []}
    assert opened == []


def test_manual_failed_distillation_stores_no_fingerprint(store: Path) -> None:
    _record_hook_session(store, "manual-failure", [
        {"kind": "prompt", "text": "retry manual work"},
        {"kind": "session-end"},
    ])
    ref = list_sessions(store)[0]

    opencode_ingest.distill_session_transcript(
        str(store), ref, commit=True, distiller=lambda transcript: None,
    )

    record_path = store / "opencode-distilled" / "claude-manual-failure.json"
    assert not record_path.exists() or SESSION_FINGERPRINT_KEY not in json.loads(
        record_path.read_text()
    )


def test_grown_closed_session_reopens_and_reaches_handover_merge(store: Path) -> None:
    path = _record_hook_session(store, "handover-grown", [
        {"kind": "prompt", "text": "old handover work"},
        {"kind": "assistant", "text": "old handover result"},
        {"kind": "session-end"},
    ])
    handover.generate_handover(str(store.parent), llm=_merge_handover)
    with path.open("a", encoding="utf-8") as stream:
        stream.write(json.dumps({"kind": "prompt", "text": "new handover work"}) + "\n")
        stream.write(json.dumps({"kind": "assistant", "text": "new handover result"}) + "\n")
    seen: list[str] = []

    def capture(system: str, user: str, **kwargs) -> str:
        seen.append(user)
        return _merge_handover(system, user, **kwargs)

    handover.generate_handover(str(store.parent), llm=capture)

    new_transcript = seen[-1].split(
        "=== NEW SESSION TRANSCRIPT (since last handover) ===\n", 1,
    )[1]
    assert "new handover work" in new_transcript
    assert "new handover result" in new_transcript
    assert "old handover work" not in new_transcript


def test_failed_handover_merge_stores_no_fingerprint(store: Path) -> None:
    _record_hook_session(store, "handover-failure", [
        {"kind": "prompt", "text": "retry handover work"},
        {"kind": "session-end"},
    ])

    handover.generate_handover(str(store.parent), llm=lambda *args, **kwargs: None)

    record_path = store / ".handover-cursors" / "claude-handover-failure.json"
    assert not record_path.exists() or SESSION_FINGERPRINT_KEY not in json.loads(
        record_path.read_text()
    )


def test_old_handover_cursor_without_fingerprint_is_processed_and_upgraded(
    store: Path,
) -> None:
    _record_hook_session(store, "handover-legacy", [
        {"kind": "prompt", "text": "already merged"},
        {"kind": "assistant", "text": "new handover evidence"},
        {"kind": "session-end"},
    ])
    record_path = store / ".handover-cursors" / "claude-handover-legacy.json"
    record_path.parent.mkdir(parents=True, exist_ok=True)
    record_path.write_text(json.dumps({
        "cursor": "claude-handover-legacy:0",
        "updated": "old",
    }) + "\n")
    seen: list[str] = []

    def capture(system: str, user: str, **kwargs) -> str:
        seen.append(user)
        return _merge_handover(system, user, **kwargs)

    handover.generate_handover(str(store.parent), llm=capture)

    assert "new handover evidence" in seen[-1]
    assert SESSION_FINGERPRINT_KEY in json.loads(record_path.read_text())


def test_legacy_opencode_active_session_still_exports_each_sweep(store: Path) -> None:
    _write_jsonl(store / "opencode-sessions.jsonl", [{
        "ts": "2026-11-15T09:00:00Z",
        "opencode_session_id": "ses-active",
    }])
    calls: list[str] = []

    def exporter(session_id: str) -> dict:
        calls.append(session_id)
        return {"messages": [{
            "info": {"id": "m1", "role": "user"},
            "parts": [{"type": "text", "text": "legacy work"}],
        }]}

    opencode_ingest.ingest_opencode_sessions(
        str(store), exporter=exporter, distiller=_candidate,
    )
    opencode_ingest.ingest_opencode_sessions(
        str(store), exporter=exporter, distiller=_candidate,
    )

    assert calls == ["ses-active", "ses-active"]
