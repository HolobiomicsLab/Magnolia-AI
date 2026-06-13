# tests/test_versioning_wiring.py
import json
import subprocess
from pathlib import Path

import pytest

from compchem_memory.startup_scan import scan_and_distill
from compchem_memory import versioning


def _git(store, *args):
    return subprocess.run(["git", "-C", str(store), *args],
                          capture_output=True, text=True)


@pytest.fixture
def project_dir(tmp_path):
    pd = tmp_path / "proj"
    for sub in ["sessions", "staging", "entries"]:
        (pd / ".magnolia" / sub).mkdir(parents=True)
    return pd


def test_sweep_commits_new_staging_entry(project_dir, monkeypatch):
    # No mapping -> tool-event fallback mode. Force the heuristic extractor so a
    # staging entry is written deterministically (no LLM/opencode needed).
    from compchem_memory import extraction
    monkeypatch.setattr(extraction.AutomaticMemoryExtractor, "_llm_distill",
                        lambda self, events: [])

    (project_dir / ".magnolia" / "sessions" / "2026-05-10_000000.jsonl").write_text(
        "\n".join(json.dumps(e) for e in [
            {"event_type": "tool_error", "tool": "haddock3", "error": "missing topology"},
            {"event_type": "tool_success", "tool": "haddock3", "result_summary": "ran ok"},
        ]) + "\n"
    )

    scan_and_distill(str(project_dir))

    store = project_dir / ".magnolia"
    assert versioning.is_repo(store)
    log = _git(store, "log", "--oneline").stdout
    assert "distill:" in log, "the sweep must produce a distill commit"
    tracked = _git(store, "ls-files", "staging").stdout
    assert tracked.strip(), "the new staging entry must be tracked"


def test_sweep_survives_versioning_failure(project_dir, monkeypatch):
    """The stated contract: a versioning failure must never break the sweep."""
    from compchem_memory import extraction

    monkeypatch.setattr(extraction.AutomaticMemoryExtractor, "_llm_distill",
                        lambda self, events: [])

    def boom(*args, **kwargs):
        raise RuntimeError("git exploded")

    monkeypatch.setattr(versioning, "commit_all", boom)

    (project_dir / ".magnolia" / "sessions" / "2026-05-10_000000.jsonl").write_text(
        json.dumps({"event_type": "tool_call", "tool": "x"}) + "\n")

    result = scan_and_distill(str(project_dir))  # must not raise

    assert result["mode"] == "tool_event"
