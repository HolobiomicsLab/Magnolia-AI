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
