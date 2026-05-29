"""SSH-driven Slurm submission backend for compchem-tools.

Public entry points (called from compchem_tools.tools.jobs dispatch):
  - submit(command, working_dir, project_dir, cluster, ...) -> dict
  - check(job_id, cluster, project_dir) -> dict
  - cancel(job_id, cluster, project_dir) -> dict
  - fetch(job_id, project_dir) -> dict

Design rationale and full data flow per tool: see the spec at
  docs/superpowers/specs/2026-05-29-hpc-azzurra-remote-submission-design.md

Restart discipline: this module is imported by the compchem-tools MCP server.
Restart opencode after merging changes to master so the LLM-facing tool
surface reflects new behavior.
"""
from __future__ import annotations
from pathlib import Path
from subprocess import CompletedProcess
import subprocess  # noqa: F401 — patched by tests via tools.ssh_slurm.subprocess.run
from typing import Any


CLUSTER_CONFIG: dict[str, dict[str, Any]] = {
    "azzurra": {
        "ssh_host": "azzurra",
        "scratch_root": "/workspace/{user}/magnolia",
        "default_user": "user",
        "default_account": "groupaccount",
        "default_qos": "qos_groupaccount",
        "default_partition": "cpucourt",
        "tunnel_script": "hpc_tunnel.sh",
        "modulefiles_use": "$HOME/modulefiles",
    },
}
