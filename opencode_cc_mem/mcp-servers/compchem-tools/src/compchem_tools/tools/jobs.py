"""Job management tools: submit, check, and cancel jobs on Slurm, PBS, or local."""

import json
import shlex
import subprocess
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from compchem_memory.tiers.project import ProjectManager
from compchem_tools.tools._resources import apply_tool_memory_floor
from compchem_tools.tools.recall_gate import recall_gate

_PROJECT_MANAGER = ProjectManager(global_base=Path.home() / ".magnolia")


def _generate_run_id(tool: str) -> str:
    """Generate a unique run_id: <tool>_<YYYYMMDD_HHMMSS>_<6hex> in UTC.

    The 6-hex suffix disambiguates parallel submissions that land in the same
    second. Without it, concurrent submit_job calls produce identical run_ids.
    """
    ts = datetime.now(timezone.utc).strftime('%Y%m%d_%H%M%S')
    suffix = uuid.uuid4().hex[:6]
    return f"{tool}_{ts}_{suffix}"


# Per-tool mechanical pre-submit gates (2026-06-18 enforcement direction,
# wired 2026-09-10 — execution plan P0.3). Fail-closed: a failing gate blocks
# the submission unless the caller explicitly passes acknowledge=True (the
# same override the recall hold uses). v1 covers HADDOCK3's canonical
# precondition: chain IDs on its PDB inputs.
_PRE_SUBMIT_GATES: dict[str, tuple[str, ...]] = {
    "haddock3": ("pdb_files_have_chain_ids",),
}


def _check_pre_submit_gates(
    tool: str | None, working_dir: str, acknowledge: bool
) -> dict[str, Any] | None:
    """Run per-tool mechanical gates; return a hold dict on failure, else None.

    A failing gate blocks the submission unless ``acknowledge=True``. A gate
    that raises also blocks — a broken gate must never silently pass bad
    input. A missing registry entry is skipped (wiring drift must not block
    users).
    """
    from compchem_tools.gates import GATE_REGISTRY

    if not tool:
        return None
    for name in _PRE_SUBMIT_GATES.get(tool, ()):
        fn = GATE_REGISTRY.get(name)
        if fn is None:
            continue
        try:
            result = fn(working_dir)
        except Exception as e:
            return {
                "success": False,
                "error": f"pre-submit gate '{name}' errored: {e}",
                "gate": {"name": name, "passed": False, "error": str(e)},
            }
        if not result.get("passed"):
            if acknowledge:
                continue
            return {
                "success": False,
                "error": f"pre-submit gate failed: {name}",
                "gate": {"name": name, **result},
                "hint": (
                    "PDB input(s) are missing chain IDs (column 22). Fix with "
                    "preprocess_pdb(add_chain_id=...) or move non-input PDBs out "
                    "of the working dir; resubmit with acknowledge=True only "
                    "after review."
                ),
            }
    return None


def submit_job(
    command: str,
    working_dir: str,
    scheduler: str = "slurm",
    job_name: str = "compchem",
    ncores: int = 4,
    memory: str = "4GB",
    time_limit: str = "24:00:00",
    partition: str | None = None,
    project_dir: str | None = None,
    cluster: str | None = None,
    account: str | None = None,
    qos: str | None = None,
    tool: str | None = None,
    restart_of: str | None = None,
    remote_precommand: str | None = None,
    acknowledge: bool = False,
    system_tags: list[str] | None = None,
) -> dict[str, Any]:
    """Submit a job to Slurm, PBS, ssh-slurm, or run locally.
    Returns job ID and submission details."""
    scheduler = scheduler.lower()

    # Enforce per-tool minimum memory (e.g., HADDOCK3 needs >=2GB/core).
    # Bumps `memory` in-place if below floor; no-op for tools without a floor.
    memory = apply_tool_memory_floor(tool, ncores, memory)

    # Pre-launch recall gate: hold once if memory has tool-scoped pitfalls,
    # unless the caller has acknowledged. Fail-open (recall_gate never raises).
    held = recall_gate(tool, command, project_dir, acknowledge, system_tags)
    if held is not None:
        return held

    # Mechanical pre-submit gates (P0.3; fail-closed, acknowledge overrides).
    gate_hold = _check_pre_submit_gates(tool, working_dir, acknowledge)
    if gate_hold is not None:
        return gate_hold

    if scheduler == "ssh-slurm":
        from compchem_tools.tools import ssh_slurm
        return ssh_slurm.submit(
            command=command,
            working_dir=working_dir,
            project_dir=project_dir,
            cluster=cluster,
            account=account,
            qos=qos,
            partition=partition,
            job_name=job_name,
            ncores=ncores,
            memory=memory,
            time_limit=time_limit,
            tool=tool,
            restart_of=restart_of,
            remote_precommand=remote_precommand,
            system_tags=system_tags,
        )

    wdir = Path(working_dir)
    if not wdir.exists():
        return {"success": False, "error": f"Working directory not found: {working_dir}"}

    if scheduler == "slurm":
        result = _submit_slurm(command, wdir, job_name, ncores, memory, time_limit, partition)
    elif scheduler == "pbs":
        result = _submit_pbs(command, wdir, job_name, ncores, memory, time_limit, partition)
    elif scheduler == "local":
        result = _submit_local(command, wdir, job_name, ncores)
    else:
        return {"success": False, "error": f"Unknown scheduler: {scheduler}. Use 'slurm', 'pbs', 'ssh-slurm', or 'local'."}

    # Record the run in .magnolia/runs/ for consistency with ssh-slurm.
    # Non-ssh-slurm schedulers don't have a remote block; the run shows up
    # with lifecycle="running" (local, process started) or "submitted"
    # (slurm/pbs, handed to scheduler) and no remote key — that distinction
    # is the differentiator. Only records when project_dir is provided
    # (required for the agent path; human ad-hoc use can skip it).
    if result.get("success") and project_dir:
        run_id = _generate_run_id(tool or "job")
        lifecycle = "running" if scheduler == "local" else "submitted"
        remote_block = None
        if scheduler == "local":
            remote_block = {
                "scheduler": "local",
                "job_id": result.get("job_id"),
                "local_run_dir": result.get("local_run_dir", str(wdir)),
                "exit_sentinel": result.get("exit_sentinel"),
            }
        try:
            _PROJECT_MANAGER.record_run(
                project_dir=str(project_dir),
                run_id=run_id,
                tool=tool or "raw",
                status=None,
                lifecycle=lifecycle,
                remote=remote_block,
                system_tags=system_tags,
            )
            result["run_id"] = run_id
        except Exception:
            pass  # never let run recording break the submission result

    return result
def check_job(
    job_id: str,
    scheduler: str = "slurm",
    # ssh-slurm-specific kwargs
    cluster: str | None = None,
    project_dir: str | None = None,
) -> dict[str, Any]:
    """Check job status on Slurm, PBS, ssh-slurm, or local.
    Returns current status information."""
    scheduler = scheduler.lower()

    if scheduler == "slurm":
        return _check_slurm(job_id)
    elif scheduler == "pbs":
        return _check_pbs(job_id)
    elif scheduler == "local":
        return _check_local(job_id)
    elif scheduler == "ssh-slurm":
        from compchem_tools.tools import ssh_slurm
        return ssh_slurm.check(
            job_id=job_id,
            cluster=cluster,
            project_dir=project_dir,
        )
    else:
        return {"success": False, "error": f"Unknown scheduler: {scheduler}. Use 'slurm', 'pbs', 'ssh-slurm', or 'local'."}


def cancel_job(
    job_id: str,
    scheduler: str = "slurm",
    # ssh-slurm-specific kwargs
    cluster: str | None = None,
    project_dir: str | None = None,
) -> dict[str, Any]:
    """Cancel a running job on Slurm, PBS, ssh-slurm, or local.
    Returns cancellation status."""
    scheduler = scheduler.lower()

    if scheduler == "slurm":
        return _cancel_slurm(job_id)
    elif scheduler == "pbs":
        return _cancel_pbs(job_id)
    elif scheduler == "local":
        return _cancel_local(job_id)
    elif scheduler == "ssh-slurm":
        from compchem_tools.tools import ssh_slurm
        return ssh_slurm.cancel(
            job_id=job_id,
            cluster=cluster,
            project_dir=project_dir,
        )
    else:
        return {"success": False, "error": f"Unknown scheduler: {scheduler}. Use 'slurm', 'pbs', 'ssh-slurm', or 'local'."}


# ── Run status (authoritative-first) ──────────────────────────────────────────


def check_run_status(run_dir: str) -> dict[str, Any]:
    """Report whether a computation run has finished.

    Authoritative-first: if this run dir belongs to a tracked (ssh-slurm) run,
    trust its record's ``lifecycle`` / ``remote.slurm.state`` rather than the
    local output files. Local files are only complete and atomic once
    ``lifecycle == fetched`` (the rsync pull is non-atomic), so inspecting them
    during the completed-but-unfetched window or mid-fetch misreports a job that
    COMPLETED on the cluster as not-completed/failed. Purely-local runs (no
    remote record) fall back to local-file inspection."""
    rdir = Path(run_dir)
    record = _find_run_record(rdir)
    if record and (record.get("remote") or {}).get("scheduler"):
        return _status_from_record(record, rdir)
    return _status_from_local_files(rdir)


def _find_run_record(rdir: Path) -> dict[str, Any] | None:
    """Locate the run record for ``rdir`` via ``remote.local_run_dir``.

    A run dir lives at ``<project_dir>/runs/<name>``, so the project dir is two
    levels up. Best-effort: any failure (no project, import error) yields None
    and the caller degrades to local-file inspection."""
    try:
        from compchem_memory.tiers.project import ProjectManager
    except Exception:
        return None
    project_dir = rdir.parent.parent
    try:
        pm = ProjectManager(global_base=Path.home() / ".magnolia")
        return pm.find_run_by_local_dir(str(project_dir), str(rdir))
    except Exception:
        return None


def _status_from_record(record: dict[str, Any], rdir: Path) -> dict[str, Any]:
    remote = record.get("remote") or {}
    slurm = remote.get("slurm") or {}
    lifecycle = record.get("lifecycle")
    result: dict[str, Any] = {
        "run_dir": str(rdir),
        "exists": rdir.exists(),
        "source": "run_record",
        "lifecycle": lifecycle,
        "slurm_state": slurm.get("state"),
        "job_id": remote.get("job_id"),
        "cluster": remote.get("cluster"),
        "completed": False,
        "results_local": False,
    }
    if lifecycle == "fetched":
        # Results are local and atomic now — safe to read the output tree.
        local = _status_from_local_files(rdir)
        result["modules"] = local.get("modules", [])
        if "log_last_line" in local:
            result["log_last_line"] = local["log_last_line"]
        result["completed"] = True
        result["results_local"] = True
    elif lifecycle == "completed":
        result["completed"] = True
        if remote.get("scheduler") == "local":
            # A local run's results are already on disk — there is no fetch
            # step (unlike ssh-slurm), so treat this like "fetched".
            local = _status_from_local_files(rdir)
            result["modules"] = local.get("modules", [])
            if "log_last_line" in local:
                result["log_last_line"] = local["log_last_line"]
            result["results_local"] = True
        else:
            result["note"] = (
                "Job completed on the cluster but results are not fetched "
                "locally yet; call fetch_job_results to pull them."
            )
    elif lifecycle == "cancelled":
        result["cancelled"] = True
        result["note"] = "Job was cancelled on the cluster."
    elif lifecycle == "failed":
        result["failed"] = True
        result["note"] = (
            f"Job failed on the cluster (slurm state: {slurm.get('state') or 'unknown'})."
        )
    else:
        # submitting / submitted / running / pending / unknown — still in flight.
        result["running"] = True
        result["note"] = (
            f"Job is {lifecycle or 'in flight'} on the cluster; not finished yet."
        )
    return result


def _status_from_local_files(rdir: Path) -> dict[str, Any]:
    """Inspect a local HADDOCK-style run dir: output modules + io.json finished
    flag + last log line. Correct for purely-local runs and for fetched remote
    runs."""
    output_dir = rdir / "output"
    result: dict[str, Any] = {
        "run_dir": str(rdir),
        "exists": rdir.exists(),
        "output_dir_exists": output_dir.exists(),
        "source": "local_files",
        "completed": False,
        "modules": [],
    }
    if output_dir.exists():
        result["modules"] = sorted(
            d.name for d in output_dir.iterdir() if d.is_dir()
        )
        io_jsons = list(output_dir.glob("*/io.json"))
        if io_jsons:
            try:
                io_data = json.loads(sorted(io_jsons)[-1].read_text())
                if io_data.get("finished"):
                    result["completed"] = True
            except Exception:
                pass
        log_file = rdir / "log"
        if log_file.exists():
            result["log_last_line"] = log_file.read_text().strip().split("\n")[-1]
    return result


# ── Slurm ────────────────────────────────────────────────────────────────────


def _submit_slurm(
    command: str,
    wdir: Path,
    job_name: str,
    ncores: int,
    memory: str,
    time_limit: str,
    partition: str | None,
) -> dict[str, Any]:
    """Submit job via sbatch."""
    script_lines = [
        "#!/bin/bash",
        f"#SBATCH --job-name={job_name}",
        f"#SBATCH --chdir={wdir}",
        f"#SBATCH --ntasks=1",
        f"#SBATCH --cpus-per-task={ncores}",
        f"#SBATCH --mem={memory}",
        f"#SBATCH --time={time_limit}",
        f"#SBATCH --output=slurm-%j.out",
        f"#SBATCH --error=slurm-%j.err",
    ]
    if partition:
        script_lines.append(f"#SBATCH --partition={partition}")
    script_lines.append("")
    script_lines.append(command)

    script_path = wdir / "submit_slurm.sh"
    script_path.write_text("\n".join(script_lines) + "\n")

    try:
        proc = subprocess.run(
            ["sbatch", str(script_path)],
            capture_output=True,
            text=True,
            timeout=30,
        )
        if proc.returncode == 0:
            # Parse job ID from "Submitted batch job 12345"
            job_id = proc.stdout.strip().split()[-1]
            return {
                "success": True,
                "job_id": job_id,
                "scheduler": "slurm",
                "working_dir": str(wdir),
            }
        else:
            return {
                "success": False,
                "error": proc.stderr.strip() or "sbatch submission failed",
            }
    except FileNotFoundError:
        return {"success": False, "error": "sbatch binary not found on PATH"}
    except subprocess.TimeoutExpired:
        return {"success": False, "error": "sbatch timed out"}
    except Exception as e:
        return {"success": False, "error": str(e)}


def _check_slurm(job_id: str) -> dict[str, Any]:
    """Check Slurm job status via squeue."""
    try:
        proc = subprocess.run(
            ["squeue", "-j", job_id, "--format=%T,%j,%M,%D", "--noheader"],
            capture_output=True,
            text=True,
            timeout=10,
        )
        if proc.returncode == 0 and proc.stdout.strip():
            parts = proc.stdout.strip().split(",")
            status = parts[0].strip() if parts else "UNKNOWN"
            return {
                "success": True,
                "job_id": job_id,
                "scheduler": "slurm",
                "status": status,
                "running": status == "RUNNING",
                "pending": status == "PENDING",
                "completed": status not in ("RUNNING", "PENDING"),
            }
        else:
            # Job may have finished — check sacct
            proc2 = subprocess.run(
                ["sacct", "-j", job_id, "--format=State", "--noheader", "--parsable2"],
                capture_output=True,
                text=True,
                timeout=10,
            )
            if proc2.returncode == 0 and proc2.stdout.strip():
                state = proc2.stdout.strip().split("\n")[0].strip()
                return {
                    "success": True,
                    "job_id": job_id,
                    "scheduler": "slurm",
                    "status": state,
                    "completed": state == "COMPLETED",
                    "failed": state == "FAILED",
                }
            return {
                "success": True,
                "job_id": job_id,
                "scheduler": "slurm",
                "status": "UNKNOWN",
                "note": "Job not found in squeue or sacct",
            }
    except FileNotFoundError:
        return {"success": False, "error": "squeue/sacct binary not found on PATH"}
    except subprocess.TimeoutExpired:
        return {"success": False, "error": "squeue timed out"}
    except Exception as e:
        return {"success": False, "error": str(e)}


def _cancel_slurm(job_id: str) -> dict[str, Any]:
    """Cancel Slurm job via scancel."""
    try:
        proc = subprocess.run(
            ["scancel", job_id],
            capture_output=True,
            text=True,
            timeout=10,
        )
        return {
            "success": proc.returncode == 0,
            "job_id": job_id,
            "scheduler": "slurm",
            "error": proc.stderr.strip() if proc.returncode != 0 else None,
        }
    except FileNotFoundError:
        return {"success": False, "error": "scancel binary not found on PATH"}
    except Exception as e:
        return {"success": False, "error": str(e)}


# ── PBS ──────────────────────────────────────────────────────────────────────


def _submit_pbs(
    command: str,
    wdir: Path,
    job_name: str,
    ncores: int,
    memory: str,
    time_limit: str,
    partition: str | None,
) -> dict[str, Any]:
    """Submit job via qsub (PBS/Torque)."""
    # Convert Slurm-style walltime to PBS format (already HH:MM:SS)
    script_lines = [
        "#!/bin/bash",
        f"#PBS -N {job_name}",
        f"#PBS -d {wdir}",
        f"#PBS -l nodes=1:ppn={ncores}",
        f"#PBS -l mem={memory}",
        f"#PBS -l walltime={time_limit}",
        f"#PBS -o pbs-$PBS_JOBID.out",
        f"#PBS -e pbs-$PBS_JOBID.err",
    ]
    if partition:
        script_lines.append(f"#PBS -q {partition}")
    script_lines.append("")
    script_lines.append(command)

    script_path = wdir / "submit_pbs.sh"
    script_path.write_text("\n".join(script_lines) + "\n")

    try:
        proc = subprocess.run(
            ["qsub", str(script_path)],
            capture_output=True,
            text=True,
            timeout=30,
        )
        if proc.returncode == 0:
            job_id = proc.stdout.strip()
            return {
                "success": True,
                "job_id": job_id,
                "scheduler": "pbs",
                "working_dir": str(wdir),
            }
        else:
            return {
                "success": False,
                "error": proc.stderr.strip() or "qsub submission failed",
            }
    except FileNotFoundError:
        return {"success": False, "error": "qsub binary not found on PATH"}
    except subprocess.TimeoutExpired:
        return {"success": False, "error": "qsub timed out"}
    except Exception as e:
        return {"success": False, "error": str(e)}


def _check_pbs(job_id: str) -> dict[str, Any]:
    """Check PBS job status via qstat."""
    try:
        proc = subprocess.run(
            ["qstat", "-f", job_id],
            capture_output=True,
            text=True,
            timeout=10,
        )
        if proc.returncode == 0 and proc.stdout.strip():
            # Parse job_state from qstat output
            state = "UNKNOWN"
            for line in proc.stdout.split("\n"):
                if "job_state" in line:
                    state = line.split("=")[-1].strip()
                    break

            return {
                "success": True,
                "job_id": job_id,
                "scheduler": "pbs",
                "status": state,
                "running": state == "R",
                "pending": state == "Q",
                "completed": state == "C",
            }
        else:
            return {
                "success": True,
                "job_id": job_id,
                "scheduler": "pbs",
                "status": "UNKNOWN",
                "note": "Job not found in qstat",
            }
    except FileNotFoundError:
        return {"success": False, "error": "qstat binary not found on PATH"}
    except subprocess.TimeoutExpired:
        return {"success": False, "error": "qstat timed out"}
    except Exception as e:
        return {"success": False, "error": str(e)}


def _cancel_pbs(job_id: str) -> dict[str, Any]:
    """Cancel PBS job via qdel."""
    try:
        proc = subprocess.run(
            ["qdel", job_id],
            capture_output=True,
            text=True,
            timeout=10,
        )
        return {
            "success": proc.returncode == 0,
            "job_id": job_id,
            "scheduler": "pbs",
            "error": proc.stderr.strip() if proc.returncode != 0 else None,
        }
    except FileNotFoundError:
        return {"success": False, "error": "qdel binary not found on PATH"}
    except Exception as e:
        return {"success": False, "error": str(e)}


# ── Local ────────────────────────────────────────────────────────────────────


def _submit_local(
    command: str,
    wdir: Path,
    job_name: str,
    ncores: int,
) -> dict[str, Any]:
    """Run command locally in background; write its exit code to a sentinel so
    the poller can determine terminal state + success without the Popen object."""
    import os
    import uuid

    try:
        # Resolve wdir to absolute path so derived paths (sentinel, logs, cwd) and
        # returned exit_sentinel are all absolute and consistent. If wdir is relative,
        # the child shell would re-resolve the sentinel path against its own cwd,
        # producing a double-nested path that was never created.
        wdir = Path(wdir).resolve()

        log_out = wdir / f"{job_name}.out"
        log_err = wdir / f"{job_name}.err"
        magnolia_dir = wdir / ".magnolia"
        magnolia_dir.mkdir(parents=True, exist_ok=True)
        sentinel = magnolia_dir / "local_exit_code"

        # Run the command, capture its exit code, write it to the sentinel.
        # `rc=$?` is captured immediately after the command so a compound
        # command's own last-statement status is what gets recorded.
        wrapped = f"{command}\nrc=$?\necho $rc > {shlex.quote(str(sentinel))}"

        with open(log_out, "w") as out_f, open(log_err, "w") as err_f:
            proc = subprocess.Popen(
                wrapped,
                shell=True,
                cwd=str(wdir),
                stdout=out_f,
                stderr=err_f,
                start_new_session=True,  # detach from the MCP server's group
                env={**os.environ, "OMP_NUM_THREADS": str(ncores)},
            )

        job_id = f"local_{proc.pid}_{uuid.uuid4().hex[:6]}"
        return {
            "success": True,
            "job_id": job_id,
            "pid": proc.pid,
            "scheduler": "local",
            "working_dir": str(wdir),
            "local_run_dir": str(wdir),
            "exit_sentinel": str(sentinel),
            "stdout_log": str(log_out),
            "stderr_log": str(log_err),
        }
    except Exception as e:
        return {"success": False, "error": str(e)}


def _check_local(job_id: str) -> dict[str, Any]:
    """Check local job status via PID."""
    import os
    import signal

    try:
        # Parse PID from job_id format: local_<PID>_<random>
        parts = job_id.split("_")
        if len(parts) < 2 or parts[0] != "local":
            return {"success": False, "error": f"Invalid local job ID format: {job_id}"}

        pid = int(parts[1])
        # Check if process is still running (signal 0 does not kill)
        try:
            os.kill(pid, 0)
            return {
                "success": True,
                "job_id": job_id,
                "scheduler": "local",
                "status": "RUNNING",
                "pid": pid,
                "running": True,
            }
        except ProcessLookupError:
            return {
                "success": True,
                "job_id": job_id,
                "scheduler": "local",
                "status": "COMPLETED",
                "pid": pid,
                "running": False,
                "completed": True,
            }
        except PermissionError:
            return {
                "success": True,
                "job_id": job_id,
                "scheduler": "local",
                "status": "RUNNING",
                "pid": pid,
                "running": True,
            }
    except (ValueError, IndexError):
        return {"success": False, "error": f"Could not parse PID from job ID: {job_id}"}
    except Exception as e:
        return {"success": False, "error": str(e)}


def _local_group_alive(pid: int) -> bool:
    """True if the process group ``pid`` has any live (non-zombie) member.

    Local jobs run with ``start_new_session=True`` (see ``_submit_local``), so
    the recorded master pid IS the process-group id, and every child the
    command spawns inherits that group. Zombies are excluded: after the group
    is signalled the master can linger as a zombie until its parent (the MCP
    daemon) reaps it, and a zombie must not read as "still running".
    """
    import os

    try:
        entries = os.listdir("/proc")
    except OSError:
        # No /proc (non-Linux): fall back to a killpg probe.
        try:
            os.killpg(pid, 0)
            return True
        except ProcessLookupError:
            return False
        except PermissionError:
            return True

    for entry in entries:
        if not entry.isdigit():
            continue
        try:
            with open(f"/proc/{entry}/stat", "rb") as f:
                data = f.read().decode("utf-8", "replace")
            # /proc/<pid>/stat: "pid (comm) state ppid pgrp session ..."
            after = data.rsplit(") ", 1)[1].split()
            state, pgid = after[0], int(after[2])
        except (OSError, IndexError, ValueError):
            continue
        if pgid == pid and state != "Z":
            return True
    return False


def _cancel_local(job_id: str) -> dict[str, Any]:
    """Cancel a local job by signalling its whole process group.

    Local jobs are started with ``start_new_session=True``, so the recorded pid
    is a session/process-group leader and the command's children share its
    group. Killing only the master pid orphaned the children (the 2026-06 todo
    item), so this signals the GROUP, escalating SIGTERM -> SIGKILL after a
    bounded grace period (a stuck scientific binary can ignore SIGTERM).

    Regression: tests/test_local_lifecycle.py::test_cancel_local_kills_children.
    """
    import os
    import signal
    import time

    TERM_GRACE_S = 2.0
    POLL_S = 0.2

    try:
        parts = job_id.split("_")
        if len(parts) < 2 or parts[0] != "local":
            return {"success": False, "error": f"Invalid local job ID format: {job_id}"}

        pid = int(parts[1])

        if not _local_group_alive(pid):
            return {
                "success": True,
                "job_id": job_id,
                "scheduler": "local",
                "pid": pid,
                "note": "Process group already terminated",
            }

        signals_sent: list[str] = []
        try:
            os.killpg(pid, signal.SIGTERM)
            signals_sent.append("SIGTERM")
        except ProcessLookupError:
            return {
                "success": True,
                "job_id": job_id,
                "scheduler": "local",
                "pid": pid,
                "note": "Process group already terminated",
            }
        except PermissionError:
            return {
                "success": False,
                "error": f"Permission denied to kill process group {pid}",
            }

        deadline = time.monotonic() + TERM_GRACE_S
        while time.monotonic() < deadline and _local_group_alive(pid):
            time.sleep(POLL_S)

        if _local_group_alive(pid):
            try:
                os.killpg(pid, signal.SIGKILL)
                signals_sent.append("SIGKILL")
            except ProcessLookupError:
                pass
            except PermissionError:
                return {
                    "success": False,
                    "error": f"Permission denied to kill process group {pid}",
                    "signals_sent": signals_sent,
                }
            time.sleep(POLL_S)

        return {
            "success": True,
            "job_id": job_id,
            "scheduler": "local",
            "pid": pid,
            "signals_sent": signals_sent,
            "group_terminated": not _local_group_alive(pid),
        }
    except (ValueError, IndexError):
        return {"success": False, "error": f"Could not parse PID from job ID: {job_id}"}
    except Exception as e:
        return {"success": False, "error": str(e)}
