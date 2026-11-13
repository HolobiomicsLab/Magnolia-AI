"""hpc-session transport backend: optional, off by default.

The default ssh path is covered by test_ssh_slurm.py. These tests pin the
hpc-session transport and assert the backend delegates to the hpc-session CLI.
"""
from __future__ import annotations

from pathlib import Path
from subprocess import CompletedProcess

import pytest

from compchem_tools.tools import ssh_slurm


@pytest.fixture
def hpc_cfg(monkeypatch):
    cfg = dict(ssh_slurm.CLUSTER_CONFIG)
    cfg["azzurra"] = dict(cfg["azzurra"], transport="hpc-session")
    monkeypatch.setattr(ssh_slurm, "CLUSTER_CONFIG", cfg)
    return cfg


def test_ssh_delegates_to_hpc_session_run(hpc_cfg, fake_subprocess):
    fake_subprocess.canned["echo hello"] = CompletedProcess(
        args=[], returncode=0, stdout="hello\n", stderr="")

    result = ssh_slurm._ssh("azzurra", "echo hello")

    assert result.returncode == 0
    call = fake_subprocess.calls[0]
    assert call[0] == "hpc-session"
    assert call[1] == "run"
    assert call[2] == "--"
    assert call[3] == "echo hello"


def test_rsync_push_delegates_to_hpc_session_local(hpc_cfg, fake_subprocess, tmp_path):
    local = tmp_path / "myrun"
    local.mkdir()

    ssh_slurm._rsync_push(local, "azzurra", "/workspace/testuser/test")

    call = fake_subprocess.calls[0]
    assert call[0] == "hpc-session"
    assert call[1] == "local"
    assert call[2] == "rsync"
    assert "-az" in call
    assert "--mkpath" in call
    assert call[-2] == f"{local}/"
    assert call[-1] == "azzurra:/workspace/testuser/test/"


def test_rsync_pull_delegates_to_hpc_session_local(hpc_cfg, fake_subprocess, tmp_path):
    local = tmp_path / "myrun"

    ssh_slurm._rsync_pull("azzurra", "/workspace/testuser/test", local)

    call = fake_subprocess.calls[0]
    assert call[0] == "hpc-session"
    assert call[1] == "local"
    assert call[2] == "rsync"
    assert "--stats" in call
    assert call[-2] == "azzurra:/workspace/testuser/test/"
    assert call[-1] == f"{local}/"


def test_ensure_master_uses_hpc_session_status(hpc_cfg, fake_subprocess):
    fake_subprocess.canned["status"] = CompletedProcess(
        args=[], returncode=0, stdout="up\n", stderr="")

    ssh_slurm._ensure_master("azzurra")

    call = fake_subprocess.calls[0]
    assert call[0] == "hpc-session"
    assert call[1] == "status"


def test_ensure_master_failure_points_to_hpc_session_open(hpc_cfg, fake_subprocess):
    fake_subprocess.canned["status"] = CompletedProcess(
        args=[], returncode=1, stdout="", stderr="down")

    with pytest.raises(RuntimeError, match="hpc-session open"):
        ssh_slurm._ensure_master("azzurra")


def test_default_transport_is_ssh(monkeypatch, fake_subprocess):
    # Without an explicit transport, the backend stays on plain ssh.
    cfg = dict(ssh_slurm.CLUSTER_CONFIG)
    cfg["azzurra"] = {k: v for k, v in cfg["azzurra"].items() if k != "transport"}
    monkeypatch.setattr(ssh_slurm, "CLUSTER_CONFIG", cfg)
    fake_subprocess.canned["echo hello"] = CompletedProcess(
        args=[], returncode=0, stdout="hello\n", stderr="")

    ssh_slurm._ssh("azzurra", "echo hello")

    call = fake_subprocess.calls[0]
    assert call[0] == "ssh"
    assert "-o" in call and "BatchMode=yes" in call
