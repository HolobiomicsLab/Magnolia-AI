"""Poller policy: scan, dispatch, capture. Pure orchestration on top of
ssh_slurm primitives and ProjectManager. All network I/O is stubbed."""
from pathlib import Path
import yaml
import pytest

from compchem_tools.tools import poller


def _write_run(runs_dir: Path, run_id: str, *, lifecycle: str | None,
                scheduler: str | None = "ssh-slurm", job_id: str | None = "100"):
    runs_dir.mkdir(parents=True, exist_ok=True)
    rec = {"run_id": run_id, "tool": "xtb", "status": None,
            "date": "2026-05-29", "metrics": {}, "quality_flags": [], "errors_solved": []}
    if lifecycle is not None:
        rec["lifecycle"] = lifecycle
    if scheduler is not None:
        rec["remote"] = {"scheduler": scheduler, "cluster": "azzurra",
                          "job_id": job_id, "local_run_dir": str(runs_dir / run_id),
                          "remote_run_dir": f"/r/{run_id}"}
    (runs_dir / f"20260529_{run_id}.yaml").write_text(yaml.dump(rec))


def test_scan_active_returns_only_ssh_slurm_submitted_or_running(tmp_path):
    pd = tmp_path / "proj"
    runs = pd / ".magnolia" / "runs"
    _write_run(runs, "r_submitted", lifecycle="submitted")
    _write_run(runs, "r_running",   lifecycle="running")
    _write_run(runs, "r_completed", lifecycle="completed")
    _write_run(runs, "r_fetched",   lifecycle="fetched")
    _write_run(runs, "r_cancelled", lifecycle="cancelled")
    _write_run(runs, "r_submitting", lifecycle="submitting")  # no job_id yet → skip
    _write_run(runs, "r_local",     lifecycle="submitted", scheduler="slurm")  # not ssh-slurm
    _write_run(runs, "r_nolife",    lifecycle=None)  # absent lifecycle (legacy local-only)

    active = poller._scan_active_runs(str(pd))
    ids = {r["run_id"] for r in active}
    assert ids == {"r_submitted", "r_running"}


def test_scan_active_skips_corrupt_yamls(tmp_path):
    pd = tmp_path / "proj"
    runs = pd / ".magnolia" / "runs"
    _write_run(runs, "r_ok", lifecycle="submitted")
    (runs / "20260529_broken.yaml").write_text("a: [unterminated\n")
    (runs / "20260529_scalar.yaml").write_text("just-a-string\n")
    active = poller._scan_active_runs(str(pd))  # no crash
    assert {r["run_id"] for r in active} == {"r_ok"}


def test_scan_active_skips_runs_missing_job_id(tmp_path):
    pd = tmp_path / "proj"
    runs = pd / ".magnolia" / "runs"
    _write_run(runs, "r_ok", lifecycle="submitted")
    _write_run(runs, "r_nojob", lifecycle="submitted", job_id=None)
    active = poller._scan_active_runs(str(pd))
    assert {r["run_id"] for r in active} == {"r_ok"}


def test_scan_active_returns_empty_when_runs_dir_absent(tmp_path):
    pd = tmp_path / "proj"
    pd.mkdir()
    assert poller._scan_active_runs(str(pd)) == []
