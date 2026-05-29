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
from datetime import datetime, timezone
from pathlib import Path
from subprocess import CompletedProcess
import re
import subprocess  # noqa: F401 — patched by tests via tools.ssh_slurm.subprocess.run
from typing import Any

from compchem_memory.tiers.project import ProjectManager


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


_SBATCH_JOBID_RE = re.compile(r"Submitted batch job (\d+)")


def _parse_sbatch_jobid(stdout: str) -> str | None:
    """Extract the job id from sbatch's stdout, or None if not found."""
    match = _SBATCH_JOBID_RE.search(stdout or "")
    return match.group(1) if match else None


def _write_sbatch_script(
    local_run_dir: Path,
    *,
    job_name: str,
    account: str,
    qos: str,
    partition: str,
    time_limit: str,
    ncores: int,
    memory: str,
    modulefiles_use: str,
    tool: str | None,
    command: str,
) -> Path:
    """Generate {local_run_dir}/job.slurm. Returns the path.

    Layout matches the template in the spec §4.1: SBATCH directives,
    set -euo pipefail, module purge + use + (optionally) load, cd to
    SLURM_SUBMIT_DIR, then the user's command.
    """
    module_load_line = f"module load {tool}/local" if tool else ""
    script = f"""\
#!/bin/bash
#SBATCH --job-name={job_name}
#SBATCH --account={account}
#SBATCH --qos={qos}
#SBATCH --partition={partition}
#SBATCH --time={time_limit}
#SBATCH --nodes=1
#SBATCH --ntasks=1
#SBATCH --cpus-per-task={ncores}
#SBATCH --mem={memory}
#SBATCH --output=%x_%j.out
#SBATCH --error=%x_%j.err

set -euo pipefail
module purge
module use {modulefiles_use}
{module_load_line}

cd "$SLURM_SUBMIT_DIR"
{command}
"""
    path = local_run_dir / "job.slurm"
    path.write_text(script)
    return path


_PROJECT_MANAGER = ProjectManager(global_base=Path.home() / ".magnolia")


def _generate_run_id(tool: str) -> str:
    """Generate a unique run_id: <tool>_<YYYYMMDD_HHMMSS> in UTC."""
    return f"{tool}_{datetime.now(timezone.utc).strftime('%Y%m%d_%H%M%S')}"


def _project_name(project_dir: str) -> str:
    """The trailing directory name, used to namespace remote paths."""
    return Path(project_dir).name


def _remote_run_dir(cluster: str, project_dir: str, run_id: str) -> str:
    """Build the canonical remote scratch path for this run."""
    cfg = CLUSTER_CONFIG[cluster]
    scratch = cfg["scratch_root"].format(user=cfg["default_user"])
    return f"{scratch}/{_project_name(project_dir)}/runs/{run_id}"


def submit(
    *,
    command: str,
    working_dir: str,
    project_dir: str,
    cluster: str = "azzurra",
    account: str | None = None,
    qos: str | None = None,
    partition: str | None = None,
    job_name: str = "compchem",
    ncores: int = 4,
    memory: str = "4GB",
    time_limit: str = "24:00:00",
    tool: str | None = None,
) -> dict[str, Any]:
    """Submit a job to the cluster via SSH-driven Slurm.

    See spec §3.4 for full data flow. Tunnel-up first, generate sbatch,
    rsync push, ssh sbatch, parse jobid, write runs/*.yaml, return
    JSON-shaped dict.
    """
    if cluster not in CLUSTER_CONFIG:
        return {"success": False, "error_kind": "unknown_cluster",
                "error": f"unknown cluster: {cluster}"}
    cfg = CLUSTER_CONFIG[cluster]
    account = account or cfg["default_account"]
    qos = qos or cfg["default_qos"]
    partition = partition or cfg["default_partition"]

    try:
        _ensure_tunnel(cfg["tunnel_script"])
    except RuntimeError as e:
        return {"success": False, "error_kind": "tunnel_failed", "error": str(e)}

    run_id = _generate_run_id(tool or "job")
    local_run_dir = Path(working_dir)
    local_run_dir.mkdir(parents=True, exist_ok=True)
    remote_run_dir = _remote_run_dir(cluster, project_dir, run_id)

    _write_sbatch_script(
        local_run_dir,
        job_name=job_name,
        account=account,
        qos=qos,
        partition=partition,
        time_limit=time_limit,
        ncores=ncores,
        memory=memory,
        modulefiles_use=cfg["modulefiles_use"],
        tool=tool,
        command=command,
    )

    push = _rsync_push(local_run_dir, cluster, remote_run_dir)
    if push.returncode != 0:
        return {"success": False, "error_kind": "rsync_push_failed",
                "error": f"rsync push exit={push.returncode}",
                "details": {"stderr": push.stderr.strip()}}

    sb = _ssh(cluster, f"sbatch {remote_run_dir}/job.slurm")
    if sb.returncode != 0:
        return {"success": False, "error_kind": "sbatch_rejected",
                "error": f"sbatch exit={sb.returncode}",
                "details": {"stderr": sb.stderr.strip()}}
    job_id = _parse_sbatch_jobid(sb.stdout)
    if not job_id:
        return {"success": False, "error_kind": "sbatch_rejected",
                "error": "sbatch returncode=0 but jobid not found",
                "details": {"stdout": sb.stdout.strip()}}

    remote_record = {
        "scheduler": "ssh-slurm",
        "cluster": cluster,
        "job_id": job_id,
        "account": account,
        "qos": qos,
        "partition": partition,
        "local_run_dir": str(local_run_dir),
        "remote_run_dir": remote_run_dir,
        "submitted_at": datetime.now(timezone.utc).isoformat(),
    }
    _PROJECT_MANAGER.record_run(
        project_dir=project_dir,
        run_id=run_id,
        tool=tool or "raw",
        status=None,
        lifecycle="submitted",
        remote=remote_record,
    )

    return {
        "success": True,
        "scheduler": "ssh-slurm",
        "job_id": job_id,
        "run_id": run_id,
        "remote_run_dir": remote_run_dir,
        "local_run_dir": str(local_run_dir),
    }
