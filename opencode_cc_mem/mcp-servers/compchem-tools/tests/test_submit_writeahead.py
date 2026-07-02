"""submit() must:
   - write .magnolia/manifest.json into local_run_dir before rsync push
     (so the remote dir gets it on push), and
   - write a write-ahead lifecycle='submitting' local record BEFORE sbatch.
   (The write-ahead test is added in C3; this file starts with C2's tests.)
"""
import json
from pathlib import Path
import pytest
from compchem_tools.tools import ssh_slurm


@pytest.fixture
def fake_subprocess(monkeypatch, tmp_path):
    """Replace subprocess.run for tunnel/rsync/ssh-sbatch.

    Returns a list of (cmd, called_at_idx) so tests can assert ordering.
    """
    from subprocess import CompletedProcess
    calls: list[list[str]] = []

    def fake_run(cmd, *, capture_output=True, text=True, timeout=None, **kw):
        calls.append(list(cmd))
        cmd_str = " ".join(cmd)
        if cmd_str.endswith("hpc_tunnel.sh"):
            return CompletedProcess(cmd, 0, "", "")
        if cmd[0] == "rsync":
            return CompletedProcess(cmd, 0, "Number of regular files transferred: 3\n", "")
        if "sbatch job.slurm" in cmd_str:
            return CompletedProcess(cmd, 0, "Submitted batch job 555111\n", "")
        return CompletedProcess(cmd, 0, "", "")

    monkeypatch.setattr(ssh_slurm.subprocess, "run", fake_run)
    return calls


def test_submit_writes_manifest_into_local_run_dir(fake_subprocess, tmp_path, monkeypatch):
    pd = tmp_path / "proj"
    pd.mkdir()
    work = tmp_path / "work"
    monkeypatch.setattr(ssh_slurm, "_PROJECT_MANAGER",
                        ssh_slurm.ProjectManager(global_base=tmp_path / ".magnolia"))
    result = ssh_slurm.submit(
        command="xtb x.xyz",
        working_dir=str(work),
        project_dir=str(pd),
        cluster="azzurra",
        tool="xtb",
        job_name="t",
    )
    assert result["success"] is True
    manifest = work / ".magnolia" / "manifest.json"
    assert manifest.exists(), "manifest.json missing"
    data = json.loads(manifest.read_text())
    assert data["run_id"] == result["run_id"]
    assert data["tool"] == "xtb"
    assert data["project"] == pd.name
    assert data["command"] == "xtb x.xyz"
    assert data["account"]  # populated from CLUSTER_CONFIG defaults
    assert data["submitted_at"]  # iso timestamp


def test_manifest_is_written_before_rsync_push(monkeypatch, tmp_path):
    """The manifest must exist on disk AT THE MOMENT rsync runs, so the
    push includes it. Verified by capturing existence inside the fake
    subprocess at the rsync call site."""
    from subprocess import CompletedProcess
    pd = tmp_path / "proj"
    pd.mkdir()
    work = tmp_path / "work"
    monkeypatch.setattr(ssh_slurm, "_PROJECT_MANAGER",
                        ssh_slurm.ProjectManager(global_base=tmp_path / ".magnolia"))

    manifest_existed_at_rsync: list[bool] = []

    def fake_run(cmd, *, capture_output=True, text=True, timeout=None, **kw):
        cmd_str = " ".join(cmd)
        if cmd_str.endswith("hpc_tunnel.sh"):
            return CompletedProcess(cmd, 0, "", "")
        if cmd[0] == "rsync":
            # snapshot existence at the EXACT moment rsync would have run
            manifest_existed_at_rsync.append(
                (work / ".magnolia" / "manifest.json").exists()
            )
            return CompletedProcess(cmd, 0, "Number of regular files transferred: 3\n", "")
        if "sbatch job.slurm" in cmd_str:
            return CompletedProcess(cmd, 0, "Submitted batch job 555111\n", "")
        return CompletedProcess(cmd, 0, "", "")

    monkeypatch.setattr(ssh_slurm.subprocess, "run", fake_run)
    ssh_slurm.submit(
        command="xtb x.xyz",
        working_dir=str(work),
        project_dir=str(pd),
        tool="xtb",
    )
    assert manifest_existed_at_rsync, "rsync was never called"
    assert manifest_existed_at_rsync[0] is True, \
        "manifest.json did not exist at the moment of the rsync push"


def test_writeahead_record_exists_before_sbatch(tmp_path, monkeypatch):
    """submit() must write lifecycle='submitting' BEFORE invoking sbatch,
    so a crash between sbatch-success and the upgrade leaves a breadcrumb."""
    from subprocess import CompletedProcess
    pd = tmp_path / "proj"
    pd.mkdir()
    work = tmp_path / "work"
    mgr = ssh_slurm.ProjectManager(global_base=tmp_path / ".magnolia")
    monkeypatch.setattr(ssh_slurm, "_PROJECT_MANAGER", mgr)

    # Make sbatch fail so submit() returns BEFORE upgrading lifecycle.
    # The writeahead record must still be present.
    def fake_run(cmd, *, capture_output=True, text=True, timeout=None, **kw):
        cmd_str = " ".join(cmd)
        if cmd_str.endswith("hpc_tunnel.sh"):
            return CompletedProcess(cmd, 0, "", "")
        if cmd[0] == "rsync":
            return CompletedProcess(cmd, 0, "", "")
        if "sbatch job.slurm" in cmd_str:
            return CompletedProcess(cmd, 1, "", "sbatch: error: simulated")
        return CompletedProcess(cmd, 0, "", "")
    monkeypatch.setattr(ssh_slurm.subprocess, "run", fake_run)

    result = ssh_slurm.submit(
        command="xtb x.xyz",
        working_dir=str(work),
        project_dir=str(pd),
        tool="xtb",
    )
    assert result["success"] is False  # sbatch failed as designed
    # But a writeahead breadcrumb exists
    runs = list((pd / ".magnolia" / "runs").glob("*.yaml"))
    runs = [r for r in runs if r.name != "INDEX.yaml"]
    assert len(runs) == 1, f"expected one writeahead YAML, found {runs}"
    import yaml
    rec = yaml.safe_load(runs[0].read_text())
    assert rec["lifecycle"] == "submitting"
    assert rec["remote"]["scheduler"] == "ssh-slurm"
    assert rec["remote"]["remote_run_dir"]  # populated
    assert rec["remote"].get("job_id") in (None, "", "pending")  # no job_id yet


def test_writeahead_is_upgraded_to_submitted_on_success(fake_subprocess, tmp_path, monkeypatch):
    pd = tmp_path / "proj"
    pd.mkdir()
    work = tmp_path / "work"
    mgr = ssh_slurm.ProjectManager(global_base=tmp_path / ".magnolia")
    monkeypatch.setattr(ssh_slurm, "_PROJECT_MANAGER", mgr)
    result = ssh_slurm.submit(
        command="xtb x.xyz",
        working_dir=str(work),
        project_dir=str(pd),
        tool="xtb",
    )
    assert result["success"] is True
    runs = list((pd / ".magnolia" / "runs").glob("*.yaml"))
    runs = [r for r in runs if r.name != "INDEX.yaml"]
    assert len(runs) == 1
    import yaml
    rec = yaml.safe_load(runs[0].read_text())
    assert rec["lifecycle"] == "submitted"
    assert rec["remote"]["job_id"] == "555111"
