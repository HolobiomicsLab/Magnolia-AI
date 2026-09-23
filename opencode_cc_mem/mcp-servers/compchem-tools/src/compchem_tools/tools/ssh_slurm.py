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
import json
import re
import subprocess  # noqa: F401 — patched by tests via tools.ssh_slurm.subprocess.run
from typing import Any

# Shared with jobs.py and the memory CLI so one run has one id everywhere.
from compchem_memory.runid import generate_run_id as _generate_run_id
from compchem_memory.tiers.project import ProjectManager

from compchem_tools.tools import clusters
from compchem_tools.tools.clusters import ClusterError

# Site profiles, loaded once at import from clusters.yaml plus the per-user
# overrides. Under the historical name because callers and tests reach for it;
# call reload_clusters() after editing a config file in a live process.
CLUSTER_CONFIG: dict[str, dict[str, Any]] = clusters.load()


def reload_clusters() -> None:
    """Re-read the cluster files. Cheap, and the only way to pick up an edit."""
    global CLUSTER_CONFIG
    CLUSTER_CONFIG = clusters.load()


class PreflightError(RuntimeError):
    """A cluster could not be resolved or reached. Carries the caller's error_kind."""

    def __init__(self, kind: str, message: str) -> None:
        super().__init__(message)
        self.kind = kind


def _prepare(cluster: str | None) -> tuple[str, dict[str, Any]]:
    """Resolve the cluster and make it reachable, returning ``(name, settings)``.

    Steps a site does not have are skipped rather than faked: no tunnel_script
    means no tunnel, and requires_control_master=false means the ssh calls stand
    on their own — a key and an agent, on a site that does not enforce 2FA. Only
    Azzurra needs both, which is why both are settings and not code paths.
    """
    try:
        name, cfg = clusters.get(cluster, CLUSTER_CONFIG)
    except ClusterError as e:
        raise PreflightError("unknown_cluster", str(e)) from e
    if cfg.get("tunnel_script"):
        try:
            _ensure_tunnel(cfg["tunnel_script"])
        except RuntimeError as e:
            raise PreflightError("tunnel_failed", str(e)) from e
    if cfg.get("requires_control_master", True):
        try:
            _ensure_master(name)
        except RuntimeError as e:
            raise PreflightError("master_down", str(e)) from e
    return name, cfg


def _cfg(cluster: str) -> dict[str, Any]:
    """Settings for an already-resolved cluster."""
    try:
        return CLUSTER_CONFIG[cluster]
    except KeyError:
        raise ClusterError(
            f"unknown cluster: {cluster} (configured: {', '.join(sorted(CLUSTER_CONFIG))})"
        ) from None


def _ssh(cluster: str, command: str, *, timeout: int = 60) -> CompletedProcess:
    """Run a single command on the cluster's login node via ssh.

    Uses BatchMode=yes so failures (no auth, host-key change) surface
    instead of hanging on a password prompt.
    """
    cfg = _cfg(cluster)
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
    cfg = _cfg(cluster)
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
    cfg = _cfg(cluster)
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


def _ensure_master(cluster: str) -> None:
    """Verify an authenticated SSH ControlMaster is alive for the cluster.

    Called only for clusters whose profile sets requires_control_master.
    Azzurra does (verified 2026-07-08): publickey succeeds with partial
    success, then keyboard-interactive (phone TOTP) is required, so
    non-interactive ssh (the BatchMode=yes calls made by _ssh and by rsync)
    can only succeed by piggybacking on a master a human opened
    interactively. If no master is alive, raise RuntimeError carrying the
    exact command the user must run.

    `ssh -O check` talks only to the local control socket — no auth, no
    2FA prompt, returns instantly (measured: exit 0 when alive, exit 255
    in ~6 ms when no socket). Safe to call on every remote operation.
    """
    cfg = _cfg(cluster)
    result = subprocess.run(
        ["ssh", "-o", "BatchMode=yes", "-O", "check", cfg["ssh_host"]],
        capture_output=True,
        text=True,
        timeout=15,
    )
    if result.returncode != 0:
        raise RuntimeError(
            f"No live SSH ControlMaster for {cfg['ssh_host']} ({cluster} "
            f"enforces 2FA). Open one in your own terminal, then "
            f"retry — it prompts for your phone 2FA code once and the "
            f"master persists ~10h via ControlPersist:\n"
            f"    ssh {cfg['ssh_host']} hostname"
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
    # A site without a group account, a named partition or its own modulefiles
    # must emit nothing at all for it: an empty "#SBATCH --account=" is rejected
    # by sbatch, and a bare "module use" fails the script under set -e.
    qos_line = f"#SBATCH --qos={qos}\n" if qos else ""
    account_line = f"#SBATCH --account={account}\n" if account else ""
    partition_line = f"#SBATCH --partition={partition}\n" if partition else ""
    module_lines = "".join(
        line + "\n" for line in (
            f"module use {modulefiles_use}" if modulefiles_use else "",
            f"module load {tool}/local" if tool else "",
        ) if line
    )
    script = f"""\
#!/bin/bash
#SBATCH --job-name={job_name}
{account_line}\
{qos_line}\
{partition_line}\
#SBATCH --time={time_limit}
#SBATCH --nodes=1
#SBATCH --ntasks=1
#SBATCH --cpus-per-task={ncores}
#SBATCH --mem={memory}
#SBATCH --output=%x_%j.out
#SBATCH --error=%x_%j.err

set -euo pipefail
module purge
{module_lines}\

cd "$SLURM_SUBMIT_DIR"
mkdir -p .magnolia
echo "$SLURM_JOB_ID" > .magnolia/jobid
{command}
"""
    path = local_run_dir / "job.slurm"
    path.write_text(script)
    return path


_PROJECT_MANAGER = ProjectManager(global_base=Path.home() / ".magnolia")


def _project_name(project_dir: str) -> str:
    """The trailing directory name, used to namespace remote paths."""
    return Path(project_dir).name


def _remote_run_dir(cluster: str, project_dir: str, run_id: str) -> str:
    """Build the canonical remote scratch path for this run."""
    scratch = clusters.remote_scratch(_cfg(cluster))
    return f"{scratch}/{_project_name(project_dir)}/runs/{run_id}"


def submit(
    *,
    command: str,
    working_dir: str,
    project_dir: str,
    cluster: str | None = None,
    account: str | None = None,
    qos: str | None = None,
    partition: str | None = None,
    job_name: str = "compchem",
    ncores: int = 4,
    memory: str = "4GB",
    time_limit: str = "24:00:00",
    tool: str | None = None,
    restart_of: str | None = None,
    remote_precommand: str | None = None,
    system_tags: list[str] | None = None,
) -> dict[str, Any]:
    """Submit a job to the cluster via SSH-driven Slurm.

    See spec §3.4 for full data flow. Preflight first (resolve the cluster,
    tunnel and ControlMaster if the site needs them), generate sbatch, rsync
    push, ssh sbatch, parse jobid, write runs/*.yaml, return JSON-shaped dict.

    cluster=None resolves from the configuration — $MAGNOLIA_CLUSTER, the
    profile marked default, or the only one configured.
    """
    try:
        cluster, cfg = _prepare(cluster)
    except PreflightError as e:
        return {"success": False, "error_kind": e.kind, "error": str(e)}
    account = account or cfg["default_account"]
    qos = qos or cfg["default_qos"]
    partition = partition or cfg["default_partition"]
    resources = {
        "ncores": ncores,
        "memory": memory,
        "time_limit": time_limit,
        "scheduler": "ssh-slurm",
        "cluster": cluster,
        "partition": partition,
        "account": account,
        "qos": qos,
    }

    local_run_dir = Path(working_dir)
    local_run_dir.mkdir(parents=True, exist_ok=True)
    prior_remote: dict[str, Any] = {}
    if restart_of:
        # Resume in the EXISTING remote dir (where the partial output lives),
        # reusing the same logical run record. Don't mint a new dir/id.
        prior = _PROJECT_MANAGER.get_run(project_dir, restart_of)
        if not prior:
            return {"success": False, "error_kind": "run_not_found",
                    "error": f"no run record for run_id={restart_of!r} to restart"}
        prior_remote = prior.get("remote") or {}
        remote_run_dir = prior_remote.get("remote_run_dir")
        if not remote_run_dir:
            return {"success": False, "error_kind": "no_remote_dir",
                    "error": f"run {restart_of!r} has no remote_run_dir to restart in"}
        run_id = restart_of
    else:
        run_id = _generate_run_id(tool or "job")
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

    # L4: write a self-describing manifest into the local run dir so it
    # gets rsynced to the remote dir. Lets us reconstruct the run from the
    # cluster alone if the local YAML is lost or never written.
    manifest_dir = local_run_dir / ".magnolia"
    manifest_dir.mkdir(parents=True, exist_ok=True)
    manifest = {
        "run_id": run_id,
        "tool": tool or "raw",
        "project": Path(project_dir).name,
        "cluster": cluster,
        "account": account,
        "qos": qos,
        "partition": partition,
        "command": command,
        "submitted_at": datetime.now(timezone.utc).isoformat(),
    }
    (manifest_dir / "manifest.json").write_text(json.dumps(manifest, indent=2) + "\n")

    # L4: writeahead breadcrumb. If we crash between sbatch returning a
    # job_id and the post-sbatch upgrade below, this leaves a local pointer
    # to the (self-describing) remote dir so the run is recoverable.
    remote_fields = {
        "scheduler": "ssh-slurm",
        "cluster": cluster,
        "account": account,
        "qos": qos,
        "partition": partition,
        "local_run_dir": str(local_run_dir),
        "remote_run_dir": remote_run_dir,
        "submitted_at": datetime.now(timezone.utc).isoformat(),
    }
    if restart_of:
        # Reset the one record for this logical run to a clean in-flight state.
        # begin_restart REPLACES `remote`, dropping the prior slurm/failure/
        # job_id/fetched_at so the lifecycle is consistent and the poller (which
        # scans lifecycle in {submitted, running}) re-tracks the NEW job.
        remote_fields["restart_count"] = (prior_remote.get("restart_count") or 0) + 1
        _PROJECT_MANAGER.begin_restart(project_dir, run_id, remote_fields,
                                       resources=resources)
    else:
        _PROJECT_MANAGER.record_run(
            project_dir=project_dir,
            run_id=run_id,
            tool=tool or "raw",
            status=None,
            lifecycle="submitting",
            remote=remote_fields,
            resources=resources,
            system_tags=system_tags,
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
    inner = f"cd {remote_run_dir} && "
    if remote_precommand:
        inner += f"{remote_precommand} && "
    inner += "sbatch job.slurm"
    sb = _ssh(cluster, inner)
    if sb.returncode != 0:
        return {"success": False, "error_kind": "sbatch_rejected",
                "error": f"sbatch exit={sb.returncode}",
                "details": {"stderr": sb.stderr.strip()}}
    job_id = _parse_sbatch_jobid(sb.stdout)
    if not job_id:
        return {"success": False, "error_kind": "sbatch_rejected",
                "error": "sbatch returncode=0 but jobid not found",
                "details": {"stdout": sb.stdout.strip()}}

    # Upgrade the writeahead to lifecycle=submitted now that we have a job_id.
    _PROJECT_MANAGER.update_run(
        project_dir=project_dir,
        run_id=run_id,
        patch={
            "lifecycle": "submitted",
            "remote": {"job_id": job_id},
        },
    )

    return {
        "success": True,
        "scheduler": "ssh-slurm",
        # Echoed because the caller may not have named it: when cluster is
        # resolved from configuration, this is the only place the answer
        # surfaces to whoever has to reproduce the run.
        "cluster": cluster,
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


# State sets per the slurm skill (.opencode/skills/slurm/)
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
    cluster: str | None = None,
    project_dir: str | None = None,
) -> dict[str, Any]:
    """Check Slurm state for a job. Returns lifecycle + sacct resource fields.

    Tunnel up → sacct first (covers historical + most live jobs) → falls
    back to squeue for in-queue jobs that haven't entered accounting yet.
    If project_dir given, persists slurm.* fields and last_polled_at via
    ProjectManager.update_run.
    """
    try:
        cluster, _ = _prepare(cluster)
    except PreflightError as e:
        return {"success": False, "error_kind": e.kind, "error": str(e)}

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
    cluster: str | None = None,
    project_dir: str | None = None,
) -> dict[str, Any]:
    """Cancel a Slurm job via ssh scancel; update yaml to lifecycle=cancelled."""
    try:
        cluster, _ = _prepare(cluster)
    except PreflightError as e:
        return {"success": False, "error_kind": e.kind, "error": str(e)}
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
        cluster, _ = _prepare(cluster)
    except PreflightError as e:
        return {"success": False, "error_kind": e.kind, "error": str(e)}

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
