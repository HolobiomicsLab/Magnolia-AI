"""_update_runs_index must skip non-dict / unparseable YAMLs, not crash."""
from pathlib import Path
from compchem_memory.tiers.project import ProjectManager


def _mgr_and_pd(tmp_path):
    pd = str(tmp_path / "proj")
    Path(pd).mkdir()
    mgr = ProjectManager(global_base=tmp_path / ".magnolia")
    # one good record so runs_dir exists and INDEX rebuild has at least one row
    mgr.record_run(pd, run_id="good", tool="xtb", status="pass")
    return mgr, pd


def test_non_dict_yaml_is_skipped(tmp_path):
    mgr, pd = _mgr_and_pd(tmp_path)
    runs_dir = Path(pd) / ".magnolia" / "runs"
    # A scalar YAML (parses, but not a dict) — would .get() AttributeError today
    (runs_dir / "20990101_scalar.yaml").write_text("just-a-string\n")
    # Should not raise:
    mgr._update_runs_index(pd)
    idx = (runs_dir / "INDEX.yaml").read_text()
    assert "good" in idx        # good row preserved
    assert "scalar" not in idx  # bad row skipped


def test_corrupt_yaml_is_skipped(tmp_path):
    mgr, pd = _mgr_and_pd(tmp_path)
    runs_dir = Path(pd) / ".magnolia" / "runs"
    # Genuinely unparseable
    (runs_dir / "20990101_corrupt.yaml").write_text("a: [unterminated\n")
    mgr._update_runs_index(pd)
    idx = (runs_dir / "INDEX.yaml").read_text()
    assert "good" in idx
    assert "corrupt" not in idx


def test_list_yaml_is_skipped(tmp_path):
    mgr, pd = _mgr_and_pd(tmp_path)
    runs_dir = Path(pd) / ".magnolia" / "runs"
    (runs_dir / "20990101_list.yaml").write_text("- a\n- b\n")
    mgr._update_runs_index(pd)  # no crash
    idx = (runs_dir / "INDEX.yaml").read_text()
    assert "good" in idx
