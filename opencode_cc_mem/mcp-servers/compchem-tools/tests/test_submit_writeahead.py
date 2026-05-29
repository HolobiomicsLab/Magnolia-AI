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


def test_manifest_is_written_before_rsync_push(fake_subprocess, tmp_path, monkeypatch):
    """If the manifest write happens AFTER push, the remote dir won't have it."""
    pd = tmp_path / "proj"
    pd.mkdir()
    work = tmp_path / "work"
    monkeypatch.setattr(ssh_slurm, "_PROJECT_MANAGER",
                        ssh_slurm.ProjectManager(global_base=tmp_path / ".magnolia"))
    ssh_slurm.submit(
        command="xtb x.xyz",
        working_dir=str(work),
        project_dir=str(pd),
        tool="xtb",
    )
    # Manifest must exist on disk by the time rsync runs
    rsync_idx = next(i for i, c in enumerate(fake_subprocess) if c[0] == "rsync")
    assert rsync_idx >= 0
    assert (work / ".magnolia" / "manifest.json").exists()
