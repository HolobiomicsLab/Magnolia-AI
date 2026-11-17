"""Doorbell letter delivery ported from the OpenCode plugin."""

import json
import re
from datetime import datetime, timezone
from pathlib import Path

from compchem_memory.hook.doorbell import (
    DEFAULT_AGENTS,
    DoorbellResult,
    _letter,
    doorbell_disabled,
    process_prompt,
    registry_agents,
)
from compchem_memory.hook.project import ProjectPaths


def _project(tmp_path: Path, name: str = "alpha") -> ProjectPaths:
    project_dir = tmp_path / "projects" / name
    store = project_dir / ".magnolia"
    store.mkdir(parents=True)
    return ProjectPaths(tmp_path, project_dir, store, name, False)


def test_doorbell_writes_letter_and_rewrites(tmp_path, monkeypatch):
    monkeypatch.delenv("MAGNOLIA_DOORBELL", raising=False)
    paths = _project(tmp_path)

    result = process_prompt(
        "Please @literature find three recent papers on spatial metabolomics.\nthanks",
        paths,
    )

    assert isinstance(result, DoorbellResult)
    assert result.text == "Please [→ sent to @literature; the agents daemon will run it]\nthanks"
    assert result.sent == ["@literature"]
    assert len(result.letters) == 1
    letter = result.letters[0]
    assert letter.parent == paths.root / "projects" / "literature" / "inbox" / "from-alpha"
    assert re.fullmatch(r"\d{4}-\d{2}-\d{2}_find-three-recent-papers_\d{6}\.task\.md", letter.name)
    lines = letter.read_text(encoding="utf-8").splitlines()
    assert lines[:3] == ["# Task: find three recent papers on spatial metabolomics.", "", "- from: alpha"]
    assert re.fullmatch(r"- date: \d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}\.\d{3}Z", lines[3])
    assert lines[4:] == [
        "- status: open",
        "",
        "## Request",
        "",
        "find three recent papers on spatial metabolomics.",
        "",
    ]
    assert letter.read_text(encoding="utf-8").endswith("metabolomics.\n\n")


def test_doorbell_appends_colliding_letters(tmp_path, monkeypatch):
    monkeypatch.delenv("MAGNOLIA_DOORBELL", raising=False)
    paths = _project(tmp_path)
    prompt = "@literature find three recent papers on A\n@literature find three recent papers on B"

    result = process_prompt(prompt, paths)

    assert len(result.letters) == 2
    assert result.letters[0] == result.letters[1]
    letter = result.letters[0]
    contents = letter.read_bytes()
    date_line = re.search(rb"- date: ([^\n]+)", contents)
    assert date_line is not None
    now = datetime.strptime(date_line.group(1).decode("ascii"), "%Y-%m-%dT%H:%M:%S.%fZ").replace(
        tzinfo=timezone.utc
    )
    expected = (
        _letter("find three recent papers on A", "alpha", now)
        + _letter("find three recent papers on B", "alpha", now)
    ).encode("utf-8")
    assert contents == expected


def test_doorbell_bare_mention_untouched(tmp_path, monkeypatch):
    monkeypatch.delenv("MAGNOLIA_DOORBELL", raising=False)
    paths = _project(tmp_path)
    text = "hello @literature"

    result = process_prompt(text, paths)

    assert result.text == text
    assert result.sent == []
    assert result.letters == []
    assert not (paths.root / "projects" / "literature").exists()


def test_doorbell_disabled_agent_ignored(tmp_path, monkeypatch):
    monkeypatch.delenv("MAGNOLIA_DOORBELL", raising=False)
    paths = _project(tmp_path)
    for name, enabled in (("literature", False), ("beta", False), ("helper", True)):
        project = tmp_path / "projects" / name
        project.mkdir(parents=True)
        (project / "agent.json").write_text(json.dumps({"enabled": enabled}), encoding="utf-8")
    (tmp_path / "projects" / "unregistered").mkdir()

    agents = registry_agents(paths.root)
    assert "literature" in agents and "xiulian" in agents
    assert "helper" in agents
    assert "beta" not in agents and "unregistered" not in agents

    result = process_prompt("@beta please rewrite the audit report", paths)

    assert result.text == "@beta please rewrite the audit report"
    assert result.sent == []
    assert not (tmp_path / "projects" / "beta" / "inbox").exists()


def test_doorbell_kill_switch(tmp_path, monkeypatch):
    paths = _project(tmp_path)
    text = "@literature find three recent papers"

    assert DEFAULT_AGENTS == ("literature", "xiulian")
    assert doorbell_disabled({"MAGNOLIA_DOORBELL": "0"}) is True
    assert doorbell_disabled({"MAGNOLIA_DOORBELL": "1"}) is False
    assert doorbell_disabled({}) is False

    monkeypatch.setenv("MAGNOLIA_DOORBELL", "0")
    result = process_prompt(text, paths)

    assert result.text == text
    assert result.sent == []
    assert not (tmp_path / "projects" / "literature").exists()
