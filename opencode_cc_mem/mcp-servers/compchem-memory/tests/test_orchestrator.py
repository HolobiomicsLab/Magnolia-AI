"""assess_and_record: extracted body of post_run_assess for reuse by the poller."""
from pathlib import Path

from compchem_memory.learning.orchestrator import assess_and_record
from compchem_memory.tiers.project import ProjectManager


def _setup(tmp_path):
    pd = tmp_path / "proj"
    pd.mkdir()
    run_dir = pd / ".magnolia" / "runs" / "xtb_demo"
    run_dir.mkdir(parents=True)
    # Minimal "real" output so assess_run sees outputs_exist
    (run_dir / "xtbopt.xyz").write_text("1\nH 0 0 0\n")
    (run_dir / "xtb.log").write_text("normal termination\n")
    return pd, run_dir


def test_assess_and_record_writes_run_yaml(tmp_path):
    pd, run_dir = _setup(tmp_path)
    mgr = ProjectManager(global_base=tmp_path / ".magnolia")
    result = assess_and_record(
        run_dir=str(run_dir),
        tool="xtb",
        exit_code=0,
        project_dir=str(pd),
        project_mgr=mgr,
    )
    # Returns the assessment dict (not a json string)
    assert isinstance(result, dict)
    assert result["overall"] in ("pass", "warning", "fail")
    # And a run yaml was written
    runs = list((Path(str(pd)) / ".magnolia" / "runs").glob("*xtb_demo*.yaml"))
    assert len(runs) == 1


def test_assess_and_record_status_pass_on_zero_exit(tmp_path):
    pd, run_dir = _setup(tmp_path)
    mgr = ProjectManager(global_base=tmp_path / ".magnolia")
    result = assess_and_record(str(run_dir), "xtb", 0, str(pd), mgr)
    assert result["overall"] == "pass"


def test_assess_and_record_failed_on_nonzero_exit_missing_output(tmp_path):
    pd = tmp_path / "proj"
    pd.mkdir()
    run_dir = pd / ".magnolia" / "runs" / "xtb_dead"
    run_dir.mkdir(parents=True)
    # No outputs → technical.outputs_exist == False → overall == fail
    mgr = ProjectManager(global_base=tmp_path / ".magnolia")
    result = assess_and_record(str(run_dir), "xtb", 1, str(pd), mgr)
    assert result["overall"] == "fail"
