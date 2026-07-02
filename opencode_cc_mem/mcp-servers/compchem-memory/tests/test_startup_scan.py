"""Tests for startup scan: distillation, idempotency, error tolerance."""

import json
from pathlib import Path

import pytest

from compchem_memory.startup_scan import scan_and_distill


@pytest.fixture
def project_dir(tmp_path):
    pd = tmp_path / "proj"
    (pd / ".magnolia" / "sessions").mkdir(parents=True)
    (pd / ".magnolia" / "staging").mkdir(parents=True)
    (pd / ".magnolia" / "entries").mkdir(parents=True)
    return pd


def _write_session(pd, name, events):
    p = pd / ".magnolia" / "sessions" / f"{name}.jsonl"
    p.write_text("\n".join(json.dumps(e) for e in events) + "\n")
    return p


def test_scan_processes_undistilled_and_writes_marker(project_dir, monkeypatch):
    from compchem_memory import extraction
    monkeypatch.setattr(
        extraction.AutomaticMemoryExtractor,
        "_llm_distill",
        lambda self, events: [],
    )

    _write_session(project_dir, "2026-05-10_000000", [
        {"event_type": "tool_error", "tool": "x"},
        {"event_type": "tool_success", "tool": "x"},
    ])

    result = scan_and_distill(str(project_dir))
    assert result["scanned"] == 1
    assert result["distilled"] == 1
    assert result["skipped"] == 0

    marker = project_dir / ".magnolia" / "sessions" / "2026-05-10_000000.distilled"
    assert marker.exists()


def test_scan_is_idempotent(project_dir, monkeypatch):
    from compchem_memory import extraction
    monkeypatch.setattr(
        extraction.AutomaticMemoryExtractor,
        "_llm_distill",
        lambda self, events: [],
    )
    _write_session(project_dir, "2026-05-10_000000", [
        {"event_type": "tool_call", "tool": "x"},
    ])

    first = scan_and_distill(str(project_dir))
    second = scan_and_distill(str(project_dir))
    assert first["distilled"] == 1
    assert second["distilled"] == 0
    assert second["skipped"] == 1


def test_scan_tolerates_one_broken_file(project_dir, monkeypatch):
    from compchem_memory import extraction

    def explode(self, *a, **k):
        raise RuntimeError("synthetic distiller failure")

    monkeypatch.setattr(extraction.AutomaticMemoryExtractor, "commit", explode)

    _write_session(project_dir, "2026-05-10_000000", [{"event_type": "tool_call"}])
    _write_session(project_dir, "2026-05-11_000000", [{"event_type": "tool_call"}])

    result = scan_and_distill(str(project_dir))
    assert result["scanned"] == 2
    assert result["distilled"] == 0
