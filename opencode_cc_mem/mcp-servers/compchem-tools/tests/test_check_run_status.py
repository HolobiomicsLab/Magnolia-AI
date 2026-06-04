"""Authoritative-first `check_run_status`.

The bug: the original implementation inspected LOCAL output files only, with no
awareness of a run's record. For ssh-slurm runs the local `output/` dir is
absent during the completed-but-not-yet-fetched window and partial mid-fetch
(rsync pull is non-atomic), so a job that COMPLETED on the cluster was reported
as `completed: False` — read by the agent as "failed."

Fix: read the run record's `lifecycle` / `remote.slurm.state` first; only trust
local files once `lifecycle == fetched`. Purely-local runs (no remote record)
fall back to local-file inspection, preserving the old behavior.
"""

import json
from pathlib import Path

import pytest
import yaml

from compchem_tools.tools import jobs
from compchem_memory.tiers.project import ProjectManager


def _make_project(tmp_path: Path, name_dir: str) -> tuple[Path, Path, ProjectManager]:
    """Create <proj>/runs/<name_dir>/ and return (project_dir, run_dir, pm)."""
    project_dir = tmp_path / "proj"
    run_dir = project_dir / "runs" / name_dir
    run_dir.mkdir(parents=True)
    (project_dir / ".magnolia" / "runs").mkdir(parents=True)
    pm = ProjectManager(global_base=tmp_path / ".global")
    return project_dir, run_dir, pm


def _write_record(project_dir: Path, fname: str, record: dict) -> None:
    (project_dir / ".magnolia" / "runs" / fname).write_text(
        yaml.dump(record, default_flow_style=False, sort_keys=False)
    )


def _finished_output(run_dir: Path) -> None:
    """Lay down a HADDOCK-style completed local output tree."""
    mod = run_dir / "output" / "01_topoaa"
    mod.mkdir(parents=True)
    (mod / "io.json").write_text(json.dumps({"finished": True}))
    (run_dir / "log").write_text("done\n")


# ── find_run_by_local_dir ────────────────────────────────────────────────────


def test_find_run_by_local_dir_matches_resolved_path(tmp_path):
    project_dir, run_dir, pm = _make_project(tmp_path, "2026-06-01_ABC_s5000")
    _write_record(
        project_dir,
        "20260601_haddock3_x.yaml",
        {"run_id": "haddock3_x", "remote": {"local_run_dir": str(run_dir)}},
    )
    rec = pm.find_run_by_local_dir(str(project_dir), str(run_dir))
    assert rec is not None
    assert rec["run_id"] == "haddock3_x"


def test_find_run_by_local_dir_returns_none_when_no_match(tmp_path):
    project_dir, run_dir, pm = _make_project(tmp_path, "2026-06-01_ABC_s5000")
    _write_record(
        project_dir,
        "20260601_haddock3_x.yaml",
        {"run_id": "haddock3_x", "remote": {"local_run_dir": "/somewhere/else"}},
    )
    assert pm.find_run_by_local_dir(str(project_dir), str(run_dir)) is None


# ── check_run_status: authoritative-first ────────────────────────────────────


def test_completed_but_unfetched_is_not_reported_as_failed(tmp_path):
    """THE regression. Job COMPLETED remotely, results not pulled: local
    output/ is absent. Old code -> completed: False (looks failed)."""
    project_dir, run_dir, pm = _make_project(tmp_path, "2026-06-03_KFQRQ_s5000")
    _write_record(
        project_dir,
        "20260603_haddock3_y.yaml",
        {
            "run_id": "haddock3_y",
            "lifecycle": "completed",
            "remote": {
                "scheduler": "ssh-slurm",
                "local_run_dir": str(run_dir),
                "job_id": "11340442",
                "slurm": {"state": "COMPLETED"},
            },
        },
    )
    res = jobs.check_run_status(str(run_dir))
    assert res["source"] == "run_record"
    assert res["completed"] is True
    assert res["results_local"] is False
    assert res.get("failed") is not True
    assert "fetch" in res.get("note", "").lower()


def test_running_is_not_reported_as_completed_even_with_partial_output(tmp_path):
    project_dir, run_dir, pm = _make_project(tmp_path, "2026-06-03_RUN_s5000")
    _finished_output(run_dir)  # stale/partial local files must NOT be trusted
    _write_record(
        project_dir,
        "20260603_haddock3_r.yaml",
        {
            "run_id": "haddock3_r",
            "lifecycle": "running",
            "remote": {
                "scheduler": "ssh-slurm",
                "local_run_dir": str(run_dir),
                "slurm": {"state": "RUNNING"},
            },
        },
    )
    res = jobs.check_run_status(str(run_dir))
    assert res["source"] == "run_record"
    assert res["completed"] is False
    assert res.get("running") is True


def test_fetched_trusts_local_files(tmp_path):
    project_dir, run_dir, pm = _make_project(tmp_path, "2026-06-01_FET_s5000")
    _finished_output(run_dir)
    _write_record(
        project_dir,
        "20260601_haddock3_f.yaml",
        {
            "run_id": "haddock3_f",
            "lifecycle": "fetched",
            "remote": {
                "scheduler": "ssh-slurm",
                "local_run_dir": str(run_dir),
                "slurm": {"state": "COMPLETED"},
            },
        },
    )
    res = jobs.check_run_status(str(run_dir))
    assert res["source"] == "run_record"
    assert res["completed"] is True
    assert res["results_local"] is True
    assert "01_topoaa" in res["modules"]


def test_failed_remote_run_reports_failed(tmp_path):
    project_dir, run_dir, pm = _make_project(tmp_path, "2026-06-03_BAD_s5000")
    _write_record(
        project_dir,
        "20260603_haddock3_b.yaml",
        {
            "run_id": "haddock3_b",
            "lifecycle": "failed",
            "remote": {
                "scheduler": "ssh-slurm",
                "local_run_dir": str(run_dir),
                "slurm": {"state": "TIMEOUT"},
            },
        },
    )
    res = jobs.check_run_status(str(run_dir))
    assert res["source"] == "run_record"
    assert res["completed"] is False
    assert res["failed"] is True


def test_local_only_run_falls_back_to_local_files(tmp_path):
    """No matching remote record -> inspect local files (old behavior)."""
    project_dir, run_dir, pm = _make_project(tmp_path, "2026-06-03_LOCAL")
    _finished_output(run_dir)
    res = jobs.check_run_status(str(run_dir))
    assert res["source"] == "local_files"
    assert res["completed"] is True
    assert "01_topoaa" in res["modules"]


def test_local_only_unfinished_run_reports_not_completed(tmp_path):
    project_dir, run_dir, pm = _make_project(tmp_path, "2026-06-03_LOCAL2")
    mod = run_dir / "output" / "01_topoaa"
    mod.mkdir(parents=True)
    (mod / "io.json").write_text(json.dumps({"finished": False}))
    res = jobs.check_run_status(str(run_dir))
    assert res["source"] == "local_files"
    assert res["completed"] is False
