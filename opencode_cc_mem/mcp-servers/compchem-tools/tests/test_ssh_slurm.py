"""Unit tests for compchem_tools.tools.ssh_slurm.

All tests use the `fake_subprocess` fixture (see conftest.py) to avoid
touching the real network. Integration tests against the live Azzurra
cluster live in test_ssh_slurm_integration.py and are gated by the
MAGNOLIA_INTEGRATION_AZZURRA env var.
"""
from __future__ import annotations
from pathlib import Path
from subprocess import CompletedProcess
import json
import pytest
from compchem_tools.tools import ssh_slurm


def test_cluster_config_has_azzurra():
    assert "azzurra" in ssh_slurm.CLUSTER_CONFIG
    cfg = ssh_slurm.CLUSTER_CONFIG["azzurra"]
    assert cfg["ssh_host"] == "azzurra"
    assert cfg["default_account"] == "groupaccount"
    assert cfg["default_qos"] == "qos_groupaccount"
    assert cfg["default_partition"] == "cpucourt"
    assert cfg["tunnel_script"] == "hpc_tunnel.sh"


def test_ssh_builds_argv_with_batchmode_and_alias(fake_subprocess):
    fake_subprocess.canned["echo hello"] = CompletedProcess(
        args=[], returncode=0, stdout="hello\n", stderr=""
    )
    result = ssh_slurm._ssh("azzurra", "echo hello")
    assert result.returncode == 0
    assert result.stdout == "hello\n"
    assert len(fake_subprocess.calls) == 1
    call = fake_subprocess.calls[0]
    assert call[0] == "ssh"
    assert "-o" in call and "BatchMode=yes" in call
    assert "azzurra" in call
    assert call[-1] == "echo hello"


def test_rsync_push_builds_argv_with_mkpath(fake_subprocess, tmp_path):
    local = tmp_path / "myrun"
    local.mkdir()
    ssh_slurm._rsync_push(local, "azzurra", "/workspace/user/test")
    call = fake_subprocess.calls[0]
    assert call[0] == "rsync"
    assert "-az" in call
    assert "--mkpath" in call
    assert call[-2] == f"{local}/"
    assert call[-1] == "azzurra:/workspace/user/test/"


def test_rsync_pull_builds_argv_with_stats(fake_subprocess, tmp_path):
    local = tmp_path / "myrun"
    ssh_slurm._rsync_pull("azzurra", "/workspace/user/test", local)
    call = fake_subprocess.calls[0]
    assert call[0] == "rsync"
    assert "--stats" in call
    assert call[-2] == "azzurra:/workspace/user/test/"
    assert call[-1] == f"{local}/"


def test_ensure_tunnel_success(fake_subprocess):
    fake_subprocess.canned["hpc_tunnel.sh"] = CompletedProcess(
        args=[], returncode=0, stdout="", stderr="[hpc_tunnel] tunnel already up\n"
    )
    # Should not raise
    ssh_slurm._ensure_tunnel()
    call = fake_subprocess.calls[0]
    assert call[0] == "hpc_tunnel.sh"


def test_ensure_tunnel_failure_raises(fake_subprocess):
    fake_subprocess.canned["hpc_tunnel.sh"] = CompletedProcess(
        args=[], returncode=4, stdout="", stderr="[hpc_tunnel] ERROR: timeout\n"
    )
    with pytest.raises(RuntimeError, match="tunnel.*exit.*4"):
        ssh_slurm._ensure_tunnel()


def test_write_sbatch_script_generates_expected_directives(tmp_path):
    local_run_dir = tmp_path / "myrun"
    local_run_dir.mkdir()
    ssh_slurm._write_sbatch_script(
        local_run_dir,
        job_name="test_job",
        account="groupaccount",
        qos="qos_groupaccount",
        partition="cpucourt",
        time_limit="00:30:00",
        ncores=4,
        memory="4GB",
        modulefiles_use="$HOME/modulefiles",
        tool="xtb",
        command="xtb input.xyz --opt",
    )
    script = (local_run_dir / "job.slurm").read_text()
    for line in [
        "#SBATCH --job-name=test_job",
        "#SBATCH --account=groupaccount",
        "#SBATCH --qos=qos_groupaccount",
        "#SBATCH --partition=cpucourt",
        "#SBATCH --time=00:30:00",
        "#SBATCH --cpus-per-task=4",
        "#SBATCH --mem=4GB",
        "set -euo pipefail",
        "module purge",
        "module use $HOME/modulefiles",
        "module load xtb/local",
        'cd "$SLURM_SUBMIT_DIR"',
        "xtb input.xyz --opt",
    ]:
        assert line in script, f"missing {line!r}"


def test_write_sbatch_script_without_tool_omits_module_load(tmp_path):
    local_run_dir = tmp_path / "myrun"
    local_run_dir.mkdir()
    ssh_slurm._write_sbatch_script(
        local_run_dir,
        job_name="raw_cmd",
        account="groupaccount",
        qos="qos_groupaccount",
        partition="cpucourt",
        time_limit="00:30:00",
        ncores=1,
        memory="1GB",
        modulefiles_use="$HOME/modulefiles",
        tool=None,
        command="echo hello",
    )
    script = (local_run_dir / "job.slurm").read_text()
    assert "module load" not in script
    assert "echo hello" in script


def test_parse_sbatch_jobid_extracts_number():
    assert ssh_slurm._parse_sbatch_jobid("Submitted batch job 11331448\n") == "11331448"


def test_parse_sbatch_jobid_handles_trailing_whitespace():
    assert ssh_slurm._parse_sbatch_jobid("Submitted batch job 999  \n\n") == "999"


def test_parse_sbatch_jobid_returns_none_on_malformed_output():
    assert ssh_slurm._parse_sbatch_jobid("error: invalid partition\n") is None
    assert ssh_slurm._parse_sbatch_jobid("") is None


def test_submit_writes_sbatch_rsyncs_calls_sbatch_writes_yaml(
    fake_subprocess, tmp_path, monkeypatch
):
    project_dir = tmp_path / "myproject"
    (project_dir / ".magnolia" / "runs").mkdir(parents=True)
    local_run_dir = project_dir / "runs" / "haddock3_TEST"
    local_run_dir.mkdir(parents=True)
    (local_run_dir / "input.cfg").write_text("# dummy input\n")

    fake_subprocess.canned["hpc_tunnel.sh"] = CompletedProcess([], 0, "", "")
    fake_subprocess.canned["sbatch"] = CompletedProcess(
        [], 0, "Submitted batch job 11331448\n", ""
    )

    monkeypatch.setattr(
        "compchem_tools.tools.ssh_slurm._generate_run_id",
        lambda tool: f"{tool}_20260529_140000",
    )

    result = ssh_slurm.submit(
        command="haddock3 input.cfg",
        working_dir=str(local_run_dir),
        project_dir=str(project_dir),
        cluster="azzurra",
        account="groupaccount",
        qos="qos_groupaccount",
        partition="cpucourt",
        job_name="test_haddock",
        ncores=4,
        memory="4GB",
        time_limit="00:30:00",
        tool="haddock3",
    )

    assert result["success"] is True
    assert result["job_id"] == "11331448"
    assert result["run_id"] == "haddock3_20260529_140000"

    cmds = [" ".join(c) for c in fake_subprocess.calls]
    assert any("hpc_tunnel.sh" in c for c in cmds)
    assert any("rsync -az --mkpath" in c for c in cmds)
    assert any("ssh -o BatchMode=yes azzurra sbatch" in c for c in cmds)

    assert (local_run_dir / "job.slurm").exists()
    assert "haddock3 input.cfg" in (local_run_dir / "job.slurm").read_text()

    yaml_files = list((project_dir / ".magnolia" / "runs").glob("*_haddock3_20260529_140000.yaml"))
    assert len(yaml_files) == 1
    import yaml as yamlpkg
    record = yamlpkg.safe_load(yaml_files[0].read_text())
    assert record["lifecycle"] == "submitted"
    assert record["remote"]["cluster"] == "azzurra"
    assert record["remote"]["job_id"] == "11331448"
    assert record["remote"]["account"] == "groupaccount"


def _make_project(tmp_path):
    project_dir = tmp_path / "myproject"
    (project_dir / ".magnolia" / "runs").mkdir(parents=True)
    local_run_dir = project_dir / "runs" / "x"
    local_run_dir.mkdir(parents=True)
    return project_dir, local_run_dir


def test_submit_tunnel_failure_returns_tunnel_failed(fake_subprocess, tmp_path):
    project_dir, local_run_dir = _make_project(tmp_path)
    fake_subprocess.canned["hpc_tunnel.sh"] = CompletedProcess([], 4, "", "[hpc_tunnel] timeout\n")
    result = ssh_slurm.submit(
        command="echo hi",
        working_dir=str(local_run_dir),
        project_dir=str(project_dir),
        cluster="azzurra",
        tool=None,
    )
    assert result["success"] is False
    assert result["error_kind"] == "tunnel_failed"


def test_submit_unknown_cluster_returns_unknown_cluster(fake_subprocess, tmp_path):
    project_dir, local_run_dir = _make_project(tmp_path)
    result = ssh_slurm.submit(
        command="echo hi",
        working_dir=str(local_run_dir),
        project_dir=str(project_dir),
        cluster="some-other-cluster",
        tool=None,
    )
    assert result["success"] is False
    assert result["error_kind"] == "unknown_cluster"


def test_submit_sbatch_rejected_returns_sbatch_rejected(fake_subprocess, tmp_path):
    project_dir, local_run_dir = _make_project(tmp_path)
    fake_subprocess.canned["hpc_tunnel.sh"] = CompletedProcess([], 0, "", "")
    # NB: canned key must not collide with pytest's tmp_path basename, which
    # may contain "sbatch" (the test function name). Match the ssh argv instead.
    fake_subprocess.canned["azzurra sbatch"] = CompletedProcess(
        [], 1, "", "sbatch: error: QOSGrpCpuLimit\n"
    )
    result = ssh_slurm.submit(
        command="echo hi",
        working_dir=str(local_run_dir),
        project_dir=str(project_dir),
        cluster="azzurra",
        tool=None,
    )
    assert result["success"] is False
    assert result["error_kind"] == "sbatch_rejected"
    assert "QOSGrpCpuLimit" in result["details"]["stderr"]


def test_submit_rsync_push_failure_returns_rsync_push_failed(fake_subprocess, tmp_path):
    project_dir, local_run_dir = _make_project(tmp_path)
    fake_subprocess.canned["hpc_tunnel.sh"] = CompletedProcess([], 0, "", "")
    fake_subprocess.canned["rsync -az --mkpath"] = CompletedProcess(
        [], 23, "", "rsync error: some files could not be transferred\n"
    )
    result = ssh_slurm.submit(
        command="echo hi",
        working_dir=str(local_run_dir),
        project_dir=str(project_dir),
        cluster="azzurra",
        tool=None,
    )
    assert result["success"] is False
    assert result["error_kind"] == "rsync_push_failed"
