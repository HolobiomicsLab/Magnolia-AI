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
mkdir -p .magnolia
echo "$SLURM_JOB_ID" > .magnolia/jobid
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

    # Invoke sbatch from inside the run dir so SLURM_SUBMIT_DIR resolves to it.
    # Otherwise sbatch runs from the ssh login dir ($HOME), Slurm sets
    # SLURM_SUBMIT_DIR to $HOME, the script's `cd "$SLURM_SUBMIT_DIR"` lands
    # in $HOME, relative-path input files (rsynced into the run dir) become
    # unreachable, and `--output=%x_%j.out` / `--error=%x_%j.err` land in $HOME.
    sb = _ssh(cluster, f"cd {remote_run_dir} && sbatch job.slurm")
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


def _parse_sacct(line: str) -> dict[str, str] | None:
    """Parse a single sacct row produced with -P -X -n and the
    --format=JobID,State,ExitCode,Elapsed,MaxRSS,AveCPU,Start,End,NodeList
    field order. Returns None if the line is empty or malformed.
    """
    if not line or not line.strip():
        return None
    fields = line.strip().split("|")
    if len(fields) < 9:
        return None
    return {
        "job_id": fields[0],
        "state": fields[1],
        "exit_code": fields[2],
        "elapsed": fields[3],
        "max_rss": fields[4],
        "ave_cpu": fields[5],
        "start": fields[6],
        "end": fields[7],
        "node_list": fields[8],
    }


# State sets per rules/slurm.md
_RUNNING_STATES = {"PD", "PENDING", "CF", "CONFIGURING", "R", "RUNNING", "S", "SUSPENDED", "CG", "COMPLETING"}
_COMPLETED_STATES = {"CD", "COMPLETED"}
_CANCELLED_STATES = {"CA", "CANCELLED", "CANCELLED+"}


def _state_to_lifecycle(state: str) -> str:
    if state in _RUNNING_STATES:
        return "running"
    if state in _COMPLETED_STATES:
        return "completed"
    if state in _CANCELLED_STATES or state.startswith("CANCELLED"):
        return "cancelled"
    return "failed"


def _is_terminal(state: str) -> bool:
    return _state_to_lifecycle(state) in ("completed", "failed", "cancelled")


_SACCT_FORMAT = "JobID,State,ExitCode,Elapsed,MaxRSS,AveCPU,Start,End,NodeList"


def _find_run_by_job_id(project_dir: str, job_id: str) -> tuple[str, dict] | None:
    """Scan runs/*.yaml; return (run_id, record) for the matching job_id."""
    import yaml as yamlpkg
    runs_dir = Path(project_dir) / ".magnolia" / "runs"
    if not runs_dir.exists():
        return None
    for f in runs_dir.glob("*.yaml"):
        if f.name == "INDEX.yaml":
            continue
        try:
            data = yamlpkg.safe_load(f.read_text()) or {}
        except yamlpkg.YAMLError:
            continue
        if (data.get("remote") or {}).get("job_id") == job_id:
            return data["run_id"], data
    return None


def check(
    *,
    job_id: str,
    cluster: str = "azzurra",
    project_dir: str | None = None,
) -> dict[str, Any]:
    """Check Slurm state for a job. Returns lifecycle + sacct resource fields.

    Tunnel up → sacct first (covers historical + most live jobs) → falls
    back to squeue for in-queue jobs that haven't entered accounting yet.
    If project_dir given, persists slurm.* fields and last_polled_at via
    ProjectManager.update_run.
    """
    if cluster not in CLUSTER_CONFIG:
        return {"success": False, "error_kind": "unknown_cluster", "error": cluster}
    try:
        _ensure_tunnel(CLUSTER_CONFIG[cluster]["tunnel_script"])
    except RuntimeError as e:
        return {"success": False, "error_kind": "tunnel_failed", "error": str(e)}

    sa = _ssh(cluster, f"sacct -j {job_id} -X -P -n --format={_SACCT_FORMAT}")
    sacct = _parse_sacct(sa.stdout)

    if sacct is not None:
        state = sacct["state"]
        lifecycle = _state_to_lifecycle(state)
        result = {
            "success": True,
            "state": state,
            "lifecycle": lifecycle,
            "terminal": _is_terminal(state),
            "exit_code": sacct["exit_code"],
            "elapsed": sacct["elapsed"],
            "max_rss": sacct["max_rss"],
            "ave_cpu": sacct["ave_cpu"],
            "start": sacct["start"],
            "end": sacct["end"],
            "node_list": sacct["node_list"],
        }
        slurm_record = {
            "state": state,
            "exit_code": sacct["exit_code"],
            "elapsed": sacct["elapsed"],
            "max_rss": sacct["max_rss"],
            "ave_cpu": sacct["ave_cpu"],
            "start": sacct["start"],
            "end": sacct["end"],
            "node_list": sacct["node_list"],
        }
    else:
        sq = _ssh(cluster, f"squeue -j {job_id} -h -o '%T'")
        state = sq.stdout.strip()
        if not state:
            return {"success": False, "error_kind": "job_not_found",
                    "error": f"no sacct or squeue record for {job_id}"}
        lifecycle = _state_to_lifecycle(state)
        result = {
            "success": True,
            "state": state,
            "lifecycle": lifecycle,
            "terminal": _is_terminal(state),
        }
        slurm_record = {"state": state}

    if project_dir:
        found = _find_run_by_job_id(project_dir, job_id)
        if found:
            run_id, _ = found
            _PROJECT_MANAGER.update_run(
                project_dir=project_dir,
                run_id=run_id,
                patch={
                    "lifecycle": lifecycle,
                    "remote": {
                        "slurm": slurm_record,
                        "last_polled_at": datetime.now(timezone.utc).isoformat(),
                    },
                },
            )

    return result


def cancel(
    *,
    job_id: str,
    cluster: str = "azzurra",
    project_dir: str | None = None,
) -> dict[str, Any]:
    """Cancel a Slurm job via ssh scancel; update yaml to lifecycle=cancelled."""
    if cluster not in CLUSTER_CONFIG:
        return {"success": False, "error_kind": "unknown_cluster", "error": cluster}
    try:
        _ensure_tunnel(CLUSTER_CONFIG[cluster]["tunnel_script"])
    except RuntimeError as e:
        return {"success": False, "error_kind": "tunnel_failed", "error": str(e)}
    sc = _ssh(cluster, f"scancel {job_id}")
    if sc.returncode != 0:
        return {"success": False, "error_kind": "ssh_failed",
                "error": f"scancel exit={sc.returncode}",
                "details": {"stderr": sc.stderr.strip()}}
    if project_dir:
        found = _find_run_by_job_id(project_dir, job_id)
        if found:
            run_id, _ = found
            _PROJECT_MANAGER.update_run(
                project_dir=project_dir,
                run_id=run_id,
                patch={"lifecycle": "cancelled"},
            )
    return {"success": True, "lifecycle": "cancelled"}


_RSYNC_STATS_FILES_RE = re.compile(r"Number of regular files transferred:\s+(\d+)")


def _parse_rsync_files_transferred(stdout: str) -> int:
    m = _RSYNC_STATS_FILES_RE.search(stdout or "")
    return int(m.group(1)) if m else 0


def fetch(
    *,
    job_id: str,
    project_dir: str,
) -> dict[str, Any]:
    """Pull the remote run dir to its recorded local_run_dir.

    Looks up the run by job_id in runs/*.yaml; uses the cluster, remote_run_dir,
    and local_run_dir fields from the YAML's remote: block.
    """
    found = _find_run_by_job_id(project_dir, job_id)
    if not found:
        return {"success": False, "error_kind": "run_record_missing",
                "error": f"no run with job_id={job_id} under {project_dir}"}
    run_id, rec = found
    remote = rec.get("remote") or {}
    cluster = remote.get("cluster")
    remote_run_dir = remote.get("remote_run_dir")
    local_run_dir_str = remote.get("local_run_dir")
    if not cluster or not remote_run_dir or not local_run_dir_str:
        return {"success": False, "error_kind": "run_record_missing",
                "error": f"run {run_id} record is missing cluster/remote_run_dir/local_run_dir"}
    local_run_dir = Path(local_run_dir_str)
    try:
        _ensure_tunnel(CLUSTER_CONFIG[cluster]["tunnel_script"])
    except RuntimeError as e:
        return {"success": False, "error_kind": "tunnel_failed", "error": str(e)}

    pull = _rsync_pull(cluster, remote_run_dir, local_run_dir)
    if pull.returncode != 0:
        return {"success": False, "error_kind": "rsync_pull_failed",
                "error": f"rsync pull exit={pull.returncode}",
                "details": {"stderr": pull.stderr.strip()}}
    files_fetched = _parse_rsync_files_transferred(pull.stdout)
    _PROJECT_MANAGER.update_run(
        project_dir=project_dir,
        run_id=run_id,
        patch={
            "lifecycle": "fetched",
            "remote": {"fetched_at": datetime.now(timezone.utc).isoformat()},
        },
    )
    return {
        "success": True,
        "files_fetched": files_fetched,
        "local_run_dir": str(local_run_dir),
        "remote_run_dir": remote_run_dir,
    }
