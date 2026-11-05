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
    import os, time
    os.utime(active, (time.time() + 60, time.time() + 60))
    monkeypatch.setattr(pc, "_STARTUP_ENV", "projects/proj_c")
    monkeypatch.setattr(pc, "_request_hint", lambda: None)
    assert pc.effective_project_dir() == "proj_b"


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
