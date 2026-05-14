"""Tests: write tools return project_switch_blocked when targeting a non-pinned project."""

import json
from pathlib import Path

import pytest


@pytest.fixture
def two_projects(tmp_path, monkeypatch):
    from compchem_memory import server
    from compchem_memory.storage import ensure_project_store

    pinned = tmp_path / "proj_a"
    other = tmp_path / "proj_b"
    ensure_project_store(str(pinned))
    ensure_project_store(str(other))
    monkeypatch.setattr(server, "PROJECT_DIR", str(pinned))
    return pinned, other


def _record_learning(server_module):
    fn = server_module.memory_record_learning
    return getattr(fn, "fn", fn)


def test_record_learning_blocks_cross_project_write(two_projects):
    from compchem_memory import server
    pinned, other = two_projects
    record_learning = _record_learning(server)

    result = record_learning(
        title="Cross-project attempt",
        content="should not be written",
        project_dir=str(other),
    )
    payload = json.loads(result)
    assert payload["status"] == "project_switch_blocked"
    assert "new opencode session" in payload["message"].lower()

    assert list((other / ".magnolia" / "staging").glob("*.md")) == []
    assert list((pinned / ".magnolia" / "staging").glob("*.md")) == []


def test_record_learning_allows_pinned_project_write(two_projects):
    from compchem_memory import server
    pinned, other = two_projects
    record_learning = _record_learning(server)

    result = record_learning(
        title="Legit learning",
        content="this belongs to proj_a",
        project_dir=str(pinned),
    )
    payload = json.loads(result)
    assert payload["status"] in ("created", "bumped")
