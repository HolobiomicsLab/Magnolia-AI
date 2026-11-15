"""Session-start retrieval directive and its append-only ledger."""

import json
import re
from pathlib import Path
from types import SimpleNamespace

import pytest

from compchem_memory.hook.directive import DIRECTIVE, retrieval_directive
from compchem_memory.hook.project import ProjectPaths


def _project(tmp_path: Path) -> ProjectPaths:
    project_dir = tmp_path / "project"
    store = project_dir / ".magnolia"
    store.mkdir(parents=True)
    return ProjectPaths(tmp_path, project_dir, store, "project", True)


def test_directive_is_injected_once_and_logged(tmp_path, monkeypatch):
    monkeypatch.delenv("MAGNOLIA_AUTORETRIEVE", raising=False)
    paths = _project(tmp_path)
    state = SimpleNamespace(data={})

    assert retrieval_directive(paths, state, "session-1") == DIRECTIVE
    assert DIRECTIVE.startswith("\n\n[Magnolia · auto-memory]")
    assert state.data["directive_sent"] is True
    assert retrieval_directive(paths, state, "session-1") is None

    lines = (paths.store / "auto-retrieval.jsonl").read_text(encoding="utf-8").splitlines()
    assert len(lines) == 1
    row = json.loads(lines[0])
    assert list(row) == ["ts", "sessionID", "action"]
    assert row["sessionID"] == "session-1" and row["action"] == "inject"
    assert re.fullmatch(r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}\.\d{3}Z", row["ts"])


@pytest.mark.parametrize("value", ["0", "off", "false", "no", "OFF", "False"])
def test_directive_kill_switch(tmp_path, monkeypatch, value):
    monkeypatch.setenv("MAGNOLIA_AUTORETRIEVE", value)
    paths = _project(tmp_path)
    state = SimpleNamespace(data={})

    assert retrieval_directive(paths, state, "session-1") is None
    assert state.data == {}
    assert not (paths.store / "auto-retrieval.jsonl").exists()

