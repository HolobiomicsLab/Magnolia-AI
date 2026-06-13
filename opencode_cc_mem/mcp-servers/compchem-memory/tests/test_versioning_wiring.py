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


def test_manual_commit_creates_a_versioning_commit(tmp_path, monkeypatch):
    from compchem_memory import server, opencode_ingest as oi
    from compchem_memory.storage import ensure_project_store
    from compchem_memory.capture import reset_registry

    reset_registry()
    pd = tmp_path / "proj"
    ensure_project_store(str(pd))
    monkeypatch.setattr(server, "PROJECT_DIR", str(pd))
    (pd / ".magnolia" / "opencode-sessions.jsonl").write_text(
        json.dumps({"opencode_session_id": "ses_live", "ts": "t"}) + "\n")
    monkeypatch.setattr(server, "is_llm_available", lambda: True)
    monkeypatch.setattr(server, "_opencode_available", lambda: True)
    monkeypatch.setattr(oi, "export_session", lambda sid: {
        "info": {"id": sid},
        "messages": [{"info": {"id": "m1", "role": "user"},
                      "parts": [{"type": "text", "text": "a real finding worth keeping"}]}]})
    monkeypatch.setattr(oi, "_default_distiller",
                        lambda t: [{"title": "Finding", "content": t, "type": "scientific_finding"}])

    fn = getattr(server.memory_distill_session, "fn", server.memory_distill_session)
    fn(commit=True, project_dir=str(pd))

    store = pd / ".magnolia"
    log = _git(store, "log", "--oneline").stdout
    assert "manual distill" in log
    reset_registry()
