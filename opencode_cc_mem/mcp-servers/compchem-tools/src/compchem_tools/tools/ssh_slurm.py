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


def _ssh(cluster: str, command: str, *, timeout: int = 60) -> CompletedProcess:
    """Run a single command on the cluster's login node via ssh.

    Uses BatchMode=yes so failures (no auth, host-key change) surface
    instead of hanging on a password prompt.
    """
    cfg = CLUSTER_CONFIG[cluster]
    return subprocess.run(
        ["ssh", "-o", "BatchMode=yes", cfg["ssh_host"], command],
        capture_output=True,
        text=True,
        timeout=timeout,
    )


def _rsync_push(local: Path, cluster: str, remote: str, *, timeout: int = 600) -> CompletedProcess:
    """rsync the local directory to the remote path on the cluster.

    --mkpath creates the remote path components if they don't exist.
    """
    cfg = CLUSTER_CONFIG[cluster]
    return subprocess.run(
        ["rsync", "-az", "--mkpath", f"{local}/", f"{cfg['ssh_host']}:{remote}/"],
        capture_output=True,
        text=True,
        timeout=timeout,
    )


def _rsync_pull(cluster: str, remote: str, local: Path, *, timeout: int = 600) -> CompletedProcess:
    """rsync the remote directory to the local path.

    --stats yields a summary block at the end of stdout (Number of files,
    Total bytes, etc.) which fetch() parses to count files fetched.
    """
    cfg = CLUSTER_CONFIG[cluster]
    return subprocess.run(
        ["rsync", "-az", "--stats", f"{cfg['ssh_host']}:{remote}/", f"{local}/"],
        capture_output=True,
        text=True,
        timeout=timeout,
    )


def _ensure_tunnel(tunnel_script: str = "hpc_tunnel.sh") -> None:
    """Run hpc_tunnel.sh; raise RuntimeError if it exits non-zero.

    The script is idempotent — it's safe to call on every ssh-using
    operation. Cold start takes a few seconds; warm-up is <0.5s.
    """
    result = subprocess.run(
        [tunnel_script],
        capture_output=True,
        text=True,
        timeout=60,
    )
    if result.returncode != 0:
        raise RuntimeError(
            f"tunnel: hpc_tunnel.sh exit={result.returncode}; stderr={result.stderr.strip()!r}"
        )
