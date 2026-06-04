"""Job management tools: submit, check, and cancel jobs on Slurm, PBS, or local."""

import json
import subprocess
from pathlib import Path
from typing import Any


def submit_job(
    command: str,
    working_dir: str,
    scheduler: str = "slurm",
    job_name: str = "compchem",
    ncores: int = 4,
    memory: str = "4GB",
    time_limit: str = "24:00:00",
    partition: str | None = None,
    # ssh-slurm-specific kwargs (ignored by slurm/pbs/local branches)
    project_dir: str | None = None,
    cluster: str = "azzurra",
    account: str | None = None,
    qos: str | None = None,
    tool: str | None = None,
    restart_of: str | None = None,
    remote_precommand: str | None = None,
) -> dict[str, Any]:
    """Submit a job to Slurm, PBS, ssh-slurm, or run locally.
    Returns job ID and submission details."""
    scheduler = scheduler.lower()

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
        )

    wdir = Path(working_dir)
    if not wdir.exists():
        return {"success": False, "error": f"Working directory not found: {working_dir}"}

    if scheduler == "slurm":
        return _submit_slurm(command, wdir, job_name, ncores, memory, time_limit, partition)
    elif scheduler == "pbs":
        return _submit_pbs(command, wdir, job_name, ncores, memory, time_limit, partition)
    elif scheduler == "local":
        return _submit_local(command, wdir, job_name, ncores)
    else:
        return {"success": False, "error": f"Unknown scheduler: {scheduler}. Use 'slurm', 'pbs', 'ssh-slurm', or 'local'."}


def check_job(
    job_id: str,
    scheduler: str = "slurm",
    # ssh-slurm-specific kwargs
    cluster: str = "azzurra",
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
    cluster: str = "azzurra",
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
        result["note"] = (
            "Job completed on the cluster but results are not fetched locally "
            "yet; call fetch_job_results to pull them."
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
    """Run command locally in background."""
    import os
    import uuid

    try:
        log_out = wdir / f"{job_name}.out"
        log_err = wdir / f"{job_name}.err"

        with open(log_out, "w") as out_f, open(log_err, "w") as err_f:
            proc = subprocess.Popen(
                command,
                shell=True,
                cwd=str(wdir),
                stdout=out_f,
                stderr=err_f,
                env={**os.environ, "OMP_NUM_THREADS": str(ncores)},
            )

        job_id = f"local_{proc.pid}_{uuid.uuid4().hex[:6]}"
        return {
            "success": True,
            "job_id": job_id,
            "pid": proc.pid,
            "scheduler": "local",
            "working_dir": str(wdir),
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


def _cancel_local(job_id: str) -> dict[str, Any]:
    """Cancel local job by sending SIGTERM."""
    import os
    import signal

    try:
        parts = job_id.split("_")
        if len(parts) < 2 or parts[0] != "local":
            return {"success": False, "error": f"Invalid local job ID format: {job_id}"}

        pid = int(parts[1])
        try:
            os.kill(pid, signal.SIGTERM)
            return {
                "success": True,
                "job_id": job_id,
                "scheduler": "local",
                "pid": pid,
            }
        except ProcessLookupError:
            return {
                "success": True,
                "job_id": job_id,
                "scheduler": "local",
                "note": "Process already terminated",
            }
        except PermissionError:
            return {
                "success": False,
                "error": f"Permission denied to kill process {pid}",
            }
    except (ValueError, IndexError):
        return {"success": False, "error": f"Could not parse PID from job ID: {job_id}"}
    except Exception as e:
        return {"success": False, "error": str(e)}
