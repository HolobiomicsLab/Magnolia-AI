"""Tests for audit module: compliance signals → audit-report.md."""

import json
from pathlib import Path

import pytest


@pytest.fixture
def project_dir(tmp_path):
    pd = tmp_path / "proj"
    (pd / ".magnolia" / "sessions").mkdir(parents=True)
    return pd


def _write_session(pd, name, events):
    p = pd / ".magnolia" / "sessions" / f"{name}.jsonl"
    p.write_text("\n".join(json.dumps(e) for e in events) + "\n")
    return p


def test_audit_detects_missing_memory_get_context(project_dir):
    from compchem_memory.audit import run_audit
    _write_session(project_dir, "2026-05-12_120000", [
        {"event_type": "session_start", "session_id": "2026-05-12_120000",
         "project_id": "p", "schema_version": 2},
        {"event_type": "tool_call", "source": "compchem-tools", "tool": "haddock3_run"},
        {"event_type": "tool_call", "source": "compchem-tools", "tool": "gnina_run"},
    ])
    run_audit(str(project_dir))
    report = (project_dir / ".magnolia" / "audit-report.md").read_text()
    assert "memory_get_context_called_early: false" in report.lower()


def test_audit_detects_compliance_when_get_context_called_early(project_dir):
    from compchem_memory.audit import run_audit
    _write_session(project_dir, "2026-05-12_130000", [
        {"event_type": "session_start", "session_id": "2026-05-12_130000",
         "project_id": "p", "schema_version": 2},
        {"event_type": "tool_call", "source": "compchem-memory", "tool": "memory_get_context"},
        {"event_type": "tool_call", "source": "compchem-tools", "tool": "haddock3_run"},
    ])
    run_audit(str(project_dir))
    report = (project_dir / ".magnolia" / "audit-report.md").read_text()
    assert "memory_get_context_called_early: true" in report.lower()


def test_audit_counts_unresolved_errors(project_dir):
    from compchem_memory.audit import run_audit
    _write_session(project_dir, "2026-05-12_140000", [
        {"event_type": "session_start", "session_id": "2026-05-12_140000",
         "project_id": "p", "schema_version": 2},
        {"event_type": "tool_error", "source": "compchem-tools", "tool": "haddock3_run"},
        {"event_type": "tool_error", "source": "compchem-tools", "tool": "gnina_run"},
        {"event_type": "tool_success", "source": "compchem-tools", "tool": "gnina_run"},
    ])
    run_audit(str(project_dir))
    report = (project_dir / ".magnolia" / "audit-report.md").read_text()
    assert "unresolved_errors: 1" in report.lower()


def test_audit_graceful_on_legacy_log_without_header(project_dir):
    from compchem_memory.audit import run_audit
    _write_session(project_dir, "2026-04-01_000000", [
        {"event_type": "tool_call", "tool": "x"},
    ])
    run_audit(str(project_dir))
    assert (project_dir / ".magnolia" / "audit-report.md").exists()
