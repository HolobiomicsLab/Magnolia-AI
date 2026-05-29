"""record_run, update_run, _update_runs_index must use atomic_write_text."""
from pathlib import Path
from unittest.mock import patch

from compchem_memory.tiers.project import ProjectManager


def _mgr(tmp_path):
    return ProjectManager(global_base=tmp_path / ".magnolia")


def test_record_run_uses_atomic_write(tmp_path):
    pd = str(tmp_path / "proj")
    Path(pd).mkdir()
    mgr = _mgr(tmp_path)
    with patch("compchem_memory.tiers.project.atomic_write_text") as aw:
        mgr.record_run(pd, run_id="r1", tool="xtb", status="pass")
    # Should be called at least twice: once for the per-run YAML, once for the INDEX
    assert aw.call_count >= 2


def test_update_run_uses_atomic_write(tmp_path):
    pd = str(tmp_path / "proj")
    Path(pd).mkdir()
    mgr = _mgr(tmp_path)
    mgr.record_run(pd, run_id="r2", tool="xtb", status="pass")
    with patch("compchem_memory.tiers.project.atomic_write_text") as aw:
        mgr.update_run(pd, "r2", {"status": "warning"})
    assert aw.call_count >= 2  # YAML + INDEX rebuild


def test_record_run_end_to_end_still_works(tmp_path):
    """Real round-trip after the switch."""
    pd = str(tmp_path / "proj")
    Path(pd).mkdir()
    mgr = _mgr(tmp_path)
    fpath = mgr.record_run(pd, run_id="r3", tool="xtb", status="pass",
                            lifecycle="submitted",
                            remote={"scheduler": "ssh-slurm", "job_id": "99"})
    import yaml
    data = yaml.safe_load(Path(fpath).read_text())
    assert data["run_id"] == "r3"
    assert data["lifecycle"] == "submitted"
    assert data["remote"]["job_id"] == "99"
