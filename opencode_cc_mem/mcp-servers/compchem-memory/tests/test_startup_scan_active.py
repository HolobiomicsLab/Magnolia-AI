"""Tests: scan_and_distill must never mark the active session .distilled."""

import json
from pathlib import Path

import pytest

from compchem_memory.startup_scan import scan_and_distill


@pytest.fixture
def project_dir(tmp_path):
    pd = tmp_path / "proj"
    for sub in ["sessions", "staging", "entries"]:
        (pd / ".magnolia" / sub).mkdir(parents=True)
    return pd


def _write_session(pd, stem, events):
    p = pd / ".magnolia" / "sessions" / f"{stem}.jsonl"
    p.write_text("\n".join(json.dumps(e) for e in events) + "\n")
    return p


def _set_active(pd, session_id):
    (pd / ".magnolia" / ".current-session-id").write_text(session_id)


def test_active_session_gets_no_distilled_marker(project_dir, monkeypatch):
    from compchem_memory import extraction
    monkeypatch.setattr(extraction.AutomaticMemoryExtractor, "_generate_candidates",
                        lambda self, events: [])

    _write_session(project_dir, "2026-05-13_120000", [{"event_type": "tool_call", "tool": "x"}])
    _set_active(project_dir, "2026-05-13_120000")

    scan_and_distill(str(project_dir))

    marker = project_dir / ".magnolia" / "sessions" / "2026-05-13_120000.distilled"
    assert not marker.exists(), "active session must NOT be marked .distilled"


def test_closed_session_gets_distilled_marker(project_dir, monkeypatch):
    from compchem_memory import extraction
    monkeypatch.setattr(extraction.AutomaticMemoryExtractor, "_generate_candidates",
                        lambda self, events: [])

    _write_session(project_dir, "2026-05-12_090000", [{"event_type": "tool_call", "tool": "x"}])
    _set_active(project_dir, "2026-05-13_120000")  # different (newer) session is active

    scan_and_distill(str(project_dir))

    marker = project_dir / ".magnolia" / "sessions" / "2026-05-12_090000.distilled"
    assert marker.exists(), "closed session must be marked .distilled"


def test_active_session_reprocessed_after_new_events(project_dir, monkeypatch):
    """Regression: events appended to the active session after a scan are still
    picked up by a subsequent scan (because no marker sealed it)."""
    from compchem_memory import extraction
    commits = {"count": 0}

    def counting_commit(self, session_path, project_dir):
        commits["count"] += 1
        return []
    monkeypatch.setattr(extraction.AutomaticMemoryExtractor, "commit", counting_commit)

    session = _write_session(project_dir, "2026-05-13_120000", [{"event_type": "tool_call", "tool": "x"}])
    _set_active(project_dir, "2026-05-13_120000")

    scan_and_distill(str(project_dir))
    with open(session, "a") as f:
        f.write(json.dumps({"event_type": "tool_call", "tool": "y"}) + "\n")
    scan_and_distill(str(project_dir))

    assert commits["count"] == 2, "active session must be re-committed on the second scan"


def test_no_current_session_id_treats_all_as_closed(project_dir, monkeypatch):
    """If .current-session-id is absent, every session is treated as closed
    (gets a marker) — the safe default."""
    from compchem_memory import extraction
    monkeypatch.setattr(extraction.AutomaticMemoryExtractor, "_generate_candidates",
                        lambda self, events: [])

    _write_session(project_dir, "2026-05-12_090000", [{"event_type": "tool_call", "tool": "x"}])
    # no .current-session-id written

    scan_and_distill(str(project_dir))
    marker = project_dir / ".magnolia" / "sessions" / "2026-05-12_090000.distilled"
    assert marker.exists()
