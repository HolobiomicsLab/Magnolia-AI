"""Tests for request-scoped project resolution (stale-server fix).

Run from mcp-servers/compchem-memory/:
    python3 -m pytest tests/test_project_context.py -q
"""

from __future__ import annotations

from pathlib import Path

import pytest

from compchem_memory import project_context as pc
from compchem_memory.project_guard import check_project


def test_explicit_arg_always_wins(monkeypatch):
    monkeypatch.setattr(pc, "_STARTUP_ENV", "projects/proj_a")
    assert pc.effective_project_dir("projects/proj_b") == "projects/proj_b"


def test_env_fallback_without_hint(monkeypatch):
    monkeypatch.setattr(pc, "_STARTUP_ENV", "projects/proj_a")
    monkeypatch.setattr(pc, "_request_hint", lambda: None)
    monkeypatch.setattr(pc, "_active_project_hint", lambda env: None)
    assert pc.effective_project_dir() == "projects/proj_a"


def test_request_hint_overrides_env(monkeypatch):
    monkeypatch.setattr(pc, "_STARTUP_ENV", "projects/proj_a")
    monkeypatch.setattr(pc, "_request_hint", lambda: "projects/proj_b")
    assert pc.effective_project_dir() == "projects/proj_b"


def test_stale_active_project_overrides_env_when_newer(monkeypatch, tmp_path):
    monkeypatch.setenv("MAGNOLIA_OPENCODE_DIR", str(tmp_path))
    active = tmp_path / ".magnolia" / ".active-project"
    active.parent.mkdir(parents=True)
    active.write_text("proj_b")
    (tmp_path / "proj_b").mkdir()  # the name must resolve to an existing dir
    import os, time
    os.utime(active, (time.time() + 60, time.time() + 60))
    monkeypatch.setattr(pc, "_STARTUP_ENV", "projects/proj_c")
    monkeypatch.setattr(pc, "_request_hint", lambda: None)
    assert pc.effective_project_dir() == str((tmp_path / "proj_b").resolve())


def test_active_project_ignored_when_older(monkeypatch, tmp_path):
    monkeypatch.setenv("MAGNOLIA_OPENCODE_DIR", str(tmp_path))
    active = tmp_path / ".magnolia" / ".active-project"
    active.parent.mkdir(parents=True)
    active.write_text("proj_b")
    import os
    os.utime(active, (1, 1))  # very old
    monkeypatch.setattr(pc, "_STARTUP_ENV", "projects/proj_a")
    monkeypatch.setattr(pc, "_request_hint", lambda: None)
    assert pc.effective_project_dir() == "projects/proj_a"


def test_guard_uses_request_pin(monkeypatch):
    monkeypatch.setattr(pc, "_STARTUP_ENV", "projects/proj_c")
    monkeypatch.setattr(pc, "_request_hint", lambda: "projects/proj_a")
    pin = pc.effective_project_dir()
    assert check_project("projects/proj_a", pinned_dir=pin, is_write=True).kind == "match"
    assert check_project("projects/proj_b", pinned_dir=pin, is_write=True).kind == "cross_write"


def test_guard_blocks_cross_project_even_with_stale_env(monkeypatch):
    monkeypatch.setattr(pc, "_STARTUP_ENV", "projects/proj_c")
    monkeypatch.setattr(pc, "_request_hint", lambda: None)
    monkeypatch.setattr(pc, "_active_project_hint", lambda env: None)
    pin = pc.effective_project_dir()
    assert check_project("projects/proj_b", pinned_dir=pin, is_write=True).kind == "cross_write"


def _workspace(tmp_path, name="proj_a"):
    """Project-root launch layout: <root>/projects/<name> with a store."""
    ws = tmp_path / "projects" / name
    (ws / ".magnolia").mkdir(parents=True)
    return ws


def test_project_root_launch_name_resolves_to_workspace_not_nested(monkeypatch, tmp_path):
    """cwd is already projects/<name>; the name marker must NOT resolve to
    projects/<name>/<name> (the stray empty store seen in production)."""
    ws = _workspace(tmp_path)
    monkeypatch.setenv("MAGNOLIA_OPENCODE_DIR", str(ws))
    (ws / ".magnolia" / ".active-project").write_text("proj_a")
    monkeypatch.setattr(pc, "_STARTUP_ENV", str(ws))
    monkeypatch.setattr(pc, "_request_hint", lambda: None)
    monkeypatch.setattr(pc, "_process_start_time", lambda: 0.0)

    assert pc.effective_project_dir() == str(ws.resolve())


def test_absolute_dir_marker_wins_over_name_marker(monkeypatch, tmp_path):
    ws = _workspace(tmp_path)
    other = _workspace(tmp_path, "proj_b")
    (ws / ".magnolia" / ".active-project").write_text("proj_a")
    (ws / ".magnolia" / ".active-project-dir").write_text(str(other))
    monkeypatch.setenv("MAGNOLIA_OPENCODE_DIR", str(ws))
    monkeypatch.setattr(pc, "_STARTUP_ENV", str(ws))
    monkeypatch.setattr(pc, "_request_hint", lambda: None)
    monkeypatch.setattr(pc, "_process_start_time", lambda: 0.0)

    assert pc.effective_project_dir() == str(other.resolve())


def test_name_resolves_under_projects_root_when_cwd_is_repo_root(monkeypatch, tmp_path):
    ws = _workspace(tmp_path)
    root = tmp_path
    (root / ".magnolia").mkdir()
    (root / ".magnolia" / ".active-project").write_text("proj_a")
    monkeypatch.setenv("MAGNOLIA_OPENCODE_DIR", str(root))
    monkeypatch.setattr(pc, "_STARTUP_ENV", "projects/proj_c")
    monkeypatch.setattr(pc, "_request_hint", lambda: None)
    monkeypatch.setattr(pc, "_process_start_time", lambda: 0.0)

    assert pc.effective_project_dir() == str(ws.resolve())


def test_missing_project_name_falls_back_to_startup_pin(monkeypatch, tmp_path):
    ws = _workspace(tmp_path)
    (ws / ".magnolia" / ".active-project").write_text("proj_missing")
    monkeypatch.setenv("MAGNOLIA_OPENCODE_DIR", str(ws))
    monkeypatch.setattr(pc, "_STARTUP_ENV", str(ws))
    monkeypatch.setattr(pc, "_request_hint", lambda: None)
    monkeypatch.setattr(pc, "_process_start_time", lambda: 0.0)

    assert pc.effective_project_dir() == str(ws)


def test_absolute_legacy_marker_still_works(monkeypatch, tmp_path):
    ws = _workspace(tmp_path)
    other = _workspace(tmp_path, "proj_b")
    (ws / ".magnolia" / ".active-project").write_text(str(other))
    monkeypatch.setenv("MAGNOLIA_OPENCODE_DIR", str(ws))
    monkeypatch.setattr(pc, "_STARTUP_ENV", str(ws))
    monkeypatch.setattr(pc, "_request_hint", lambda: None)
    monkeypatch.setattr(pc, "_process_start_time", lambda: 0.0)

    assert pc.effective_project_dir() == str(other.resolve())
