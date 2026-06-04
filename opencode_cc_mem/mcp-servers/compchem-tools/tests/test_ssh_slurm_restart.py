"""Restart/resume an ssh-slurm run IN PLACE.

The gap: ssh_slurm.submit always mints a fresh run_id -> a brand-new empty remote
dir, so a job's partial output (needed by e.g. `haddock3 --restart N`, gromacs
`-cpi`, QM restart files) is stranded in the OLD dir. `restart_of=<run_id>`
reuses the prior run's remote dir + record (one record per logical run), rsyncs
inputs without deleting remote progress, optionally runs a remote cleanup
command, and sbatches in place. Tool-agnostic: the resume command + cleanup are
caller-supplied (just like `command`).
"""

from subprocess import CompletedProcess

import yaml

from compchem_tools.tools import ssh_slurm


OLD_REMOTE = "/workspace/user/magnolia/myproject/runs/haddock3_OLD"


def _seed_prior_run(project_dir):
    ssh_slurm._PROJECT_MANAGER.record_run(
        project_dir=str(project_dir),
        run_id="haddock3_OLD",
        tool="haddock3",
        status="fail",          # prior attempt timed out
        lifecycle="fetched",
        remote={
            "scheduler": "ssh-slurm", "cluster": "azzurra",
            "remote_run_dir": OLD_REMOTE,
            "local_run_dir": str(project_dir / "runs" / "x"),
            "job_id": "11111111",
            "slurm": {"state": "TIMEOUT"},
            "fetched_at": "2026-06-03T10:00:00+00:00",
        },
    )


def _setup(tmp_path):
    project_dir = tmp_path / "myproject"
    (project_dir / ".magnolia" / "runs").mkdir(parents=True)
    local = project_dir / "runs" / "x"
    local.mkdir(parents=True)
    (local / "config.cfg").write_text("# cfg\n")
    return project_dir, local


def test_restart_reuses_remote_dir_run_id_and_upserts(fake_subprocess, tmp_path):
    project_dir, local = _setup(tmp_path)
    _seed_prior_run(project_dir)
    fake_subprocess.canned["hpc_tunnel.sh"] = CompletedProcess([], 0, "", "")
    fake_subprocess.canned["sbatch"] = CompletedProcess([], 0, "Submitted batch job 22222222\n", "")

    result = ssh_slurm.submit(
        command="haddock3 config.cfg --restart 4",
        working_dir=str(local), project_dir=str(project_dir),
        cluster="azzurra", tool="haddock3",
        time_limit="08:00:00",
        restart_of="haddock3_OLD",
        remote_precommand="rm -rf output/04_flexref",
    )

    assert result["success"] is True
    assert result["run_id"] == "haddock3_OLD"          # reused, not a new id
    cmds = [" ".join(c) for c in fake_subprocess.calls]
    # rsync targets the EXISTING remote dir, and never deletes (preserves progress)
    assert any("rsync -az --mkpath" in c and OLD_REMOTE in c for c in cmds)
    assert not any("--delete" in c for c in cmds)
    # sbatch runs in the existing dir, AFTER the cleanup precommand
    sb = next(c for c in cmds if "sbatch job.slurm" in c)
    assert f"cd {OLD_REMOTE}" in sb
    assert sb.index("rm -rf output/04_flexref") < sb.index("sbatch job.slurm")

    # still ONE record for the logical run; updated in place
    runs = list((project_dir / ".magnolia" / "runs").glob("*_haddock3_OLD.yaml"))
    assert len(runs) == 1
    rec = yaml.safe_load(runs[0].read_text())
    assert rec["lifecycle"] == "submitted"
    assert rec["remote"]["job_id"] == "22222222"        # new job id
    assert rec["remote"]["remote_run_dir"] == OLD_REMOTE  # unchanged
    assert rec["remote"]["restart_count"] == 1
    # lifecycle correctness: prior terminal markers CLEARED so the record isn't
    # internally inconsistent and the poller sees a clean in-flight job
    assert rec["status"] is None                         # prior 'fail' reset
    assert "slurm" not in rec["remote"]                  # stale TIMEOUT cleared
    assert "fetched_at" not in rec["remote"]             # stale fetch marker cleared

    # and the poller re-tracks it: lifecycle 'submitted' + ssh-slurm + job_id
    from compchem_tools.tools import poller
    active_ids = [r["run_id"] for r in poller._scan_active_runs(str(project_dir))]
    assert "haddock3_OLD" in active_ids


def test_restart_missing_prior_run_errors(fake_subprocess, tmp_path):
    project_dir, local = _setup(tmp_path)
    fake_subprocess.canned["hpc_tunnel.sh"] = CompletedProcess([], 0, "", "")
    result = ssh_slurm.submit(
        command="haddock3 config.cfg --restart 4",
        working_dir=str(local), project_dir=str(project_dir),
        cluster="azzurra", tool="haddock3", restart_of="does_not_exist",
    )
    assert result["success"] is False
    assert result["error_kind"] == "run_not_found"
    # never reached sbatch
    assert not any("sbatch" in " ".join(c) for c in fake_subprocess.calls)


# Normal-submit (restart_of=None mints a fresh run_id + remote dir) is covered by
# test_ssh_slurm.py::test_submit_writes_sbatch_rsyncs_calls_sbatch_writes_yaml.
