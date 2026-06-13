import json
from pathlib import Path
import yaml
import pytest

from compchem_memory.startup_scan import scan_and_distill


def _write_finding(staging, name, title, ses):
    fm = {"title": title, "type": "scientific_finding", "opencode_session_id": ses,
          "observed_in_sessions": [ses], "tags": [], "tools": [], "confidence": 0.6}
    (staging / name).write_text("---\n" + yaml.dump(fm) + "---\n\nbody\n")


@pytest.fixture
def project_dir(tmp_path):
    pd = tmp_path / "proj"
    for sub in ["sessions", "staging", "entries"]:
        (pd / ".magnolia" / sub).mkdir(parents=True)
    return pd


def test_sweep_runs_consolidation_when_gate_met(project_dir, monkeypatch):
    from compchem_memory import consolidation, startup_scan
    monkeypatch.setattr(startup_scan, "_CONSOLIDATION_MIN_FINDINGS", 2)
    monkeypatch.setattr(startup_scan, "is_llm_available", lambda: True)
    monkeypatch.setattr(consolidation, "_default_clusterer",
                        lambda payload: [{"ids": [payload[0]["id"], payload[1]["id"]],
                                          "confidence": 0.95, "rationale": "same"}])
    staging = project_dir / ".magnolia" / "staging"
    _write_finding(staging, "a.md", "N-term ALA wins", "ses_1")
    _write_finding(staging, "b.md", "ALA beats C-term", "ses_2")

    scan_and_distill(str(project_dir))

    art = project_dir / ".magnolia" / "reflex" / "consolidation-proposal.json"
    assert art.exists()
    assert len(json.loads(art.read_text())["proposals"]) == 1


def test_sweep_skips_consolidation_below_gate(project_dir, monkeypatch):
    from compchem_memory import startup_scan
    monkeypatch.setattr(startup_scan, "_CONSOLIDATION_MIN_FINDINGS", 5)
    monkeypatch.setattr(startup_scan, "is_llm_available", lambda: True)
    staging = project_dir / ".magnolia" / "staging"
    _write_finding(staging, "a.md", "lonely", "ses_1")

    scan_and_distill(str(project_dir))

    art = project_dir / ".magnolia" / "reflex" / "consolidation-proposal.json"
    assert not art.exists()
