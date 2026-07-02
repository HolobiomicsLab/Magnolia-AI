import tempfile
from pathlib import Path
import pytest
import yaml
from compchem_memory.tiers.project import ProjectManager


@pytest.fixture
def project_dir():
    with tempfile.TemporaryDirectory() as d:
        pd = Path(d) / "project"
        pd.mkdir()
        yield pd


def test_record_run_persists_system_tags(project_dir):
    mgr = ProjectManager(project_dir)
    pd = str(project_dir)
    path = mgr.record_run(pd, run_id="r1", tool="haddock3", status="pass",
                          system_tags=["peptide", "6mer"])
    rec = yaml.safe_load(Path(path).read_text())
    assert rec["system_tags"] == ["peptide", "6mer"]


def test_record_run_system_tags_defaults_empty(project_dir):
    mgr = ProjectManager(project_dir)
    pd = str(project_dir)
    path = mgr.record_run(pd, run_id="r2", tool="haddock3", status="pass")
    rec = yaml.safe_load(Path(path).read_text())
    assert rec["system_tags"] == []


def _seed(mgr, pd, run_id, tool, status, tags, local_run_dir, date):
    mgr.record_run(pd, run_id=run_id, tool=tool, status=status,
                   system_tags=tags,
                   remote={"local_run_dir": local_run_dir})
    # normalize the date so ordering is deterministic
    import yaml as _y
    p = mgr._runs_dir(pd)
    for f in p.glob(f"*{run_id}.yaml"):
        rec = _y.safe_load(f.read_text()); rec["date"] = date
        f.write_text(_y.dump(rec, sort_keys=False))


def test_similar_runs_ranks_by_tag_overlap(project_dir):
    mgr = ProjectManager(project_dir); pd = str(project_dir)
    _seed(mgr, pd, "a", "haddock3", "pass", ["peptide", "6mer", "hsc70"], "/runs/a", "2026-06-01")
    _seed(mgr, pd, "b", "haddock3", "pass", ["peptide", "14mer"], "/runs/b", "2026-06-02")
    _seed(mgr, pd, "c", "gromacs", "pass", ["peptide", "6mer"], "/runs/c", "2026-06-03")

    hits = mgr.similar_runs(pd, "haddock3", ["peptide", "6mer", "hsc70"], limit=3)

    assert [h["run_id"] for h in hits] == ["a", "b"]  # a overlap 3, b overlap 1; gromacs excluded
    assert hits[0]["run_dir"] == "/runs/a"
    assert hits[0]["score"] == 3


def test_similar_runs_excludes_failed_and_zero_overlap(project_dir):
    mgr = ProjectManager(project_dir); pd = str(project_dir)
    _seed(mgr, pd, "f", "haddock3", "fail", ["peptide", "6mer"], "/runs/f", "2026-06-01")
    _seed(mgr, pd, "z", "haddock3", "pass", ["protein", "dimer"], "/runs/z", "2026-06-02")
    _seed(mgr, pd, "n", "haddock3", None, ["peptide", "6mer"], "/runs/n", "2026-06-03")
    assert mgr.similar_runs(pd, "haddock3", ["peptide", "6mer"]) == []


def test_similar_runs_limit_and_falsy(project_dir):
    mgr = ProjectManager(project_dir); pd = str(project_dir)
    for i in range(5):
        _seed(mgr, pd, f"r{i}", "xtb", "pass", ["qm", "opt"], f"/runs/r{i}", f"2026-06-0{i+1}")
    assert len(mgr.similar_runs(pd, "xtb", ["qm", "opt"], limit=2)) == 2
    assert mgr.similar_runs(pd, "xtb", []) == []
    assert mgr.similar_runs(pd, "", ["qm"]) == []


def test_module_wrapper_similar_runs(project_dir, monkeypatch):
    from compchem_memory import recall
    monkeypatch.setattr(recall, "GLOBAL_BASE", project_dir)
    monkeypatch.setattr(recall, "resolve_project_dir", lambda pd, default=".": str(project_dir))
    monkeypatch.setattr(recall, "ensure_project_store", lambda pd: None)
    _seed(ProjectManager(project_dir), str(project_dir), "a", "haddock3", "pass",
          ["peptide", "6mer"], "/runs/a", "2026-06-01")
    out = recall.similar_runs("haddock3", ["peptide", "6mer"], str(project_dir))
    assert len(out) == 1 and out[0]["run_dir"] == "/runs/a"
    assert recall.similar_runs("haddock3", []) == []


def test_similar_runs_skips_corrupt_run_file(project_dir):
    """Corrupt YAML should be skipped, not raise. similar_runs should still return valid runs."""
    mgr = ProjectManager(project_dir)
    pd = str(project_dir)
    # Seed a valid run
    _seed(mgr, pd, "valid", "haddock3", "pass", ["peptide", "6mer"], "/runs/valid", "2026-06-01")
    # Write a corrupt YAML file
    runs_dir = mgr._runs_dir(pd)
    corrupt_path = runs_dir / "20260601_corrupt.yaml"
    corrupt_path.write_text(":\n  bad: [unclosed")
    # similar_runs should not raise and should return the valid run
    hits = mgr.similar_runs(pd, "haddock3", ["peptide", "6mer"])
    assert len(hits) == 1
    assert hits[0]["run_id"] == "valid"


def test_similar_runs_date_desc_tiebreak(project_dir):
    """When multiple runs have equal tag overlap, sort by date descending (most recent first)."""
    mgr = ProjectManager(project_dir)
    pd = str(project_dir)
    # Create 3 runs with same tool, same tag overlap, but different dates
    _seed(mgr, pd, "a", "haddock3", "pass", ["peptide", "6mer"], "/runs/a", "2026-06-01")
    _seed(mgr, pd, "b", "haddock3", "pass", ["peptide", "6mer"], "/runs/b", "2026-06-03")
    _seed(mgr, pd, "c", "haddock3", "pass", ["peptide", "6mer"], "/runs/c", "2026-06-02")
    # similar_runs should return them ordered by date descending
    hits = mgr.similar_runs(pd, "haddock3", ["peptide", "6mer"], limit=3)
    assert len(hits) == 3
    assert [h["run_id"] for h in hits] == ["b", "c", "a"]  # dates: 06-03, 06-02, 06-01
