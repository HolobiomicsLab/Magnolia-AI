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
