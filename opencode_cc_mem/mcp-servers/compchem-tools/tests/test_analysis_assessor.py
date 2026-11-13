# tests/test_analysis_assessor.py
"""Generic assessment: exit code and output checks work for any tool."""
from pathlib import Path

from compchem_tools.analysis.assessor import assess_run
from compchem_tools.analysis.orchestrator import assess_and_record
from compchem_memory.tiers.project import ProjectManager


def test_missing_outputs_fails(tmp_path):
    result = assess_run(str(tmp_path / "empty"), "python", exit_code=0)
    assert result["overall"] == "fail"


def test_unknown_tool_with_outputs_passes(tmp_path):
    (tmp_path / "result.txt").write_text("ok")
    result = assess_run(str(tmp_path), "python", exit_code=0)
    assert result["overall"] == "pass"
    assert result["metrics"] == {}


def test_nonzero_exit_fails_even_with_outputs(tmp_path):
    (tmp_path / "result.txt").write_text("partial")
    result = assess_run(str(tmp_path), "python", exit_code=1)
    assert result["overall"] == "fail"


def test_assess_and_record_stores_exit_code(tmp_path):
    pd = tmp_path / "proj"
    run_dir = pd / "runs" / "demo"
    run_dir.mkdir(parents=True)
    (run_dir / "result.txt").write_text("ok")
    pm = ProjectManager(Path.home() / ".magnolia")

    assess_and_record(
        run_dir=str(run_dir), tool="python", exit_code=0,
        project_dir=str(pd), project_mgr=pm,
    )

    rec = pm.get_run(str(pd), "demo")
    assert rec["status"] == "pass"
    assert rec["exit_code"] == 0
