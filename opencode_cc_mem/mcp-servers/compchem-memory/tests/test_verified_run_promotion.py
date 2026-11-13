# tests/test_verified_run_promotion.py
"""One-shot results tied to a verified run record can auto-promote; ordinary
notes and unverified results still need the repetition gate."""
import json
from pathlib import Path

import yaml

from compchem_memory.tiers.project import ProjectManager


def _store(tmp_path):
    pd = tmp_path / "proj"
    (pd / ".magnolia" / "staging").mkdir(parents=True)
    (pd / ".magnolia" / "runs").mkdir(parents=True)
    return pd


def _run_record(pd, run_id="run_ok", status="pass", exit_code=None, slurm=None):
    rec = {"run_id": run_id, "tool": "haddock3", "status": status,
           "metrics": {}, "lifecycle": "completed"}
    if exit_code is not None:
        rec["exit_code"] = exit_code
    if slurm:
        rec["remote"] = {"slurm": slurm}
    (pd / ".magnolia" / "runs" / f"20261110_{run_id}.yaml").write_text(
        yaml.dump(rec))


def _entry(pd, name="result.md", entry_type="success_pattern", conf=0.7,
           run_ids=None, outcome_status=None):
    fm = {"title": "One-shot docking result", "type": entry_type,
          "observation_count": 1, "confidence": conf,
          "observed_in_sessions": ["s1"]}
    if run_ids:
        fm["run_ids"] = run_ids
    if outcome_status:
        fm["outcome_status"] = outcome_status
    (pd / ".magnolia" / "staging" / name).write_text(
        "---\n" + yaml.dump(fm) + "---\n\nbody\n")


def _promote(pd, monkeypatch):
    monkeypatch.setattr("compchem_memory.canary.is_frozen", lambda pd: False)
    pm = ProjectManager(Path.home() / ".magnolia")
    return pm.auto_promote_staging(str(pd))


def test_verified_one_shot_result_promotes(tmp_path, monkeypatch):
    pd = _store(tmp_path)
    _run_record(pd, status="pass")
    _entry(pd, run_ids=["run_ok"])

    assert _promote(pd, monkeypatch) == ["result.md"]
    assert (pd / ".magnolia" / "entries" / "result.md").exists()


def test_plain_note_does_not_promote(tmp_path, monkeypatch):
    pd = _store(tmp_path)
    _run_record(pd)
    _entry(pd, entry_type="note", run_ids=["run_ok"])

    assert _promote(pd, monkeypatch) == []


def test_intermediate_does_not_promote(tmp_path, monkeypatch):
    pd = _store(tmp_path)
    _run_record(pd)
    _entry(pd, run_ids=["run_ok"], outcome_status="intermediate")

    assert _promote(pd, monkeypatch) == []


def test_missing_run_record_does_not_promote(tmp_path, monkeypatch):
    pd = _store(tmp_path)
    _entry(pd, run_ids=["run_ok"])

    assert _promote(pd, monkeypatch) == []


def test_null_status_does_not_promote(tmp_path, monkeypatch):
    pd = _store(tmp_path)
    _run_record(pd, status=None)
    _entry(pd, run_ids=["run_ok"])

    assert _promote(pd, monkeypatch) == []


def test_nonzero_exit_does_not_promote(tmp_path, monkeypatch):
    pd = _store(tmp_path)
    _run_record(pd, exit_code=1)
    _entry(pd, run_ids=["run_ok"])

    assert _promote(pd, monkeypatch) == []


def test_failed_slurm_does_not_promote(tmp_path, monkeypatch):
    pd = _store(tmp_path)
    _run_record(pd, slurm={"state": "FAILED", "exit_code": "1:0"})
    _entry(pd, run_ids=["run_ok"])

    assert _promote(pd, monkeypatch) == []


def test_repetition_path_unchanged(tmp_path, monkeypatch):
    pd = _store(tmp_path)
    fm = {"title": "Repeated lesson", "type": "note", "observation_count": 2,
          "confidence": 0.9, "observed_in_sessions": ["s1", "s2"]}
    (pd / ".magnolia" / "staging" / "lesson.md").write_text(
        "---\n" + yaml.dump(fm) + "---\n\nbody\n")

    assert _promote(pd, monkeypatch) == ["lesson.md"]


def test_conflict_candidate_is_held(tmp_path, monkeypatch):
    pd = _store(tmp_path)
    _run_record(pd)
    _entry(pd, run_ids=["run_ok"])
    pm = ProjectManager(Path.home() / ".magnolia")
    from compchem_memory.tiers.project import ConflictCandidatesError
    monkeypatch.setattr(pm, "confirm_staging",
                        lambda *a, **k: (_ for _ in ()).throw(
                            ConflictCandidatesError("conflict", [])))
    monkeypatch.setattr("compchem_memory.canary.is_frozen", lambda pd: False)

    assert pm.auto_promote_staging(str(pd)) == []
    assert (pd / ".magnolia" / "staging" / "result.md").exists()


def test_extraction_stamps_session_and_links_run_ids(tmp_path, monkeypatch):
    from compchem_memory.extraction import AutomaticMemoryExtractor
    pd = _store(tmp_path)
    run_id = "haddock3_20261110_212111_d757f9"
    _run_record(pd, run_id=run_id)
    session = pd / "2026-11-13_test.jsonl"
    session.write_text(json.dumps({"tool": "shell", "result": "ok"}) + "\n")

    extractor = AutomaticMemoryExtractor(str(pd))
    monkeypatch.setattr(
        extractor, "_generate_candidates",
        lambda events: [{
            "type": "success_pattern",
            "title": f"Result from {run_id}",
            "content": f"Run {run_id} produced clusters.",
            "tags": [], "tools": ["haddock3"], "confidence": 0.8,
        }],
    )

    saved = extractor.commit(session, str(pd))

    assert len(saved) == 1
    text = Path(saved[0]).read_text()
    meta = yaml.safe_load(text.split("---")[1])
    assert meta["observed_in_sessions"] == ["2026-11-13_test"]
    assert meta["run_ids"] == [run_id]
