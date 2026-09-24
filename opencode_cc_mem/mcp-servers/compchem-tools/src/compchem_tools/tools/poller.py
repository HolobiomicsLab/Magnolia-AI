"""Async job lifecycle poller for ssh-slurm jobs.

Policy on top of compchem_tools.tools.ssh_slurm primitives. Pure
composition; no new I/O primitives. See spec:
  docs/superpowers/specs/2026-05-29-hpc-azzurra-async-lifecycle-design.md
"""
from __future__ import annotations
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
import logging
import threading

import yaml

from compchem_memory.tiers.project import ProjectManager
from compchem_tools.tools import ssh_slurm


log = logging.getLogger(__name__)

# Re-entrancy guard: only one sweep runs at a time per process.
_SWEEP_LOCK = threading.Lock()

# Reused ProjectManager (same global_base as ssh_slurm._PROJECT_MANAGER).
_PROJECT_MANAGER = ProjectManager(global_base=Path.home() / ".magnolia")

# State categorization (sacct strings; align with ssh_slurm._RUNNING_STATES etc.)
_SCIENCE_FAILURE_STATES = {
    "FAILED", "F",
    "TIMEOUT", "TO",
    "OUT_OF_MEMORY", "OOM",
    "REVOKED", "RV",  # slurm skill: "admin intervention needed" — not retryable
}
_INFRA_FAILURE_STATES = {
    "NODE_FAIL", "NF",
    "BOOT_FAIL", "BF",
    "PREEMPTED", "PR",
    "DEADLINE", "DL",
}
_DELIBERATE_STATES = {"CANCELLED", "CA", "CANCELLED+"}


_FAILURE_TAIL_LINES = 50


def _tail(path: Path, n: int = _FAILURE_TAIL_LINES) -> str:
    """Last n lines of a text file, or '' if absent/unreadable."""
    if not path.exists():
        return ""
    try:
        lines = path.read_text(errors="replace").splitlines()
    except OSError:
        return ""
    return "\n".join(lines[-n:])


def capture_failure(
    *,
    project_dir: str,
    run_id: str,
    tool: str,
    local_run_dir: Path,
    state: str,
    exit_code: str,
    project_mgr: Any,
) -> None:
    """Record a science-failure: write log tails + state into the run YAML,
    plus a staging memory entry. No LLM, no interpretation."""
    err_tail = ""
    out_tail = ""
    for p in local_run_dir.glob("*.err"):
        err_tail = _tail(p)
        break
    for p in local_run_dir.glob("*.out"):
        out_tail = _tail(p)
        break
    captured_at = datetime.now(timezone.utc).isoformat()
    project_mgr.update_run(
        project_dir,
        run_id,
        {
            "lifecycle": "fetched",
            "remote": {
                "failure": {
                    "state": state,
                    "exit_code": exit_code,
                    "err_tail": err_tail,
                    "out_tail": out_tail,
                    "captured_at": captured_at,
                },
            },
        },
    )
    # The failure is captured in the run record (remote.failure) above — its
    # authoritative home, surfaced by check_run_status / memory_get_run_history.
    # Do NOT also write it to staging as a "learning": a raw job-failure status
    # dump is run-lifecycle data, not a distilled lesson, so it only pollutes the
    # learning tier with machine-generated noise (and is never read back). The
    # genuine lesson from a failure comes from the distillation / assess paths.


from compchem_memory.learning.orchestrator import assess_and_record  # noqa: E402
from compchem_memory.job_notices import push_job_notice  # noqa: E402


def _category(state: str) -> str:
    """Map a sacct state string to a dispatch category.

    Unknown states fall to 'science_failure' (conservative: fetch + capture
    so the logs are not lost)."""
    s = state.upper()
    if s in ("COMPLETED", "CD"):
        return "success"
    if s in _SCIENCE_FAILURE_STATES:
        return "science_failure"
    if s in _INFRA_FAILURE_STATES:
        return "infra_failure"
    if s in _DELIBERATE_STATES or s.startswith("CANCELLED"):
        return "deliberate"
    return "science_failure"


def _parse_exit_code(raw: str) -> int:
    """sacct ExitCode is 'N:M' (process exit : signal). Return N as int.
    Returns 0 on unparseable input (assess_run treats this as a hint, not law)."""
    if not raw:
        return 0
    head = raw.split(":", 1)[0]
    try:
        return int(head)
    except ValueError:
        return 0


def dispatch_terminal(
    run_record: dict[str, Any],
    check_result: dict[str, Any],
    *,
    project_dir: str,
    project_mgr: Any,
) -> str:
    """Branch on terminal state. Returns the chosen category."""
    state = check_result.get("state", "")
    category = _category(state)
    run_id = run_record["run_id"]
    tool = run_record.get("tool", "raw")
    remote = run_record.get("remote") or {}
    job_id = remote.get("job_id")
    local_run_dir = Path(remote.get("local_run_dir", ""))
    is_local = remote.get("scheduler") == "local"

    if category == "success":
        if not is_local:
            ssh_slurm.fetch(job_id=job_id, project_dir=project_dir)
        if is_local:
            # Set the terminal lifecycle BEFORE assessing: if assess_and_record
            # raises, the run is still marked completed and _scan_active_runs
            # will not keep re-picking it up (which would re-assess forever).
            project_mgr.update_run(
                project_dir, run_id, {"lifecycle": "completed"})
        assess_and_record(
            run_dir=str(local_run_dir),
            tool=tool,
            exit_code=_parse_exit_code(check_result.get("exit_code", "0:0")),
            project_dir=project_dir,
            project_mgr=project_mgr,
            run_id=run_id,
        )
    elif category == "science_failure":
        if not is_local:
            ssh_slurm.fetch(job_id=job_id, project_dir=project_dir)
        capture_failure(
            project_dir=project_dir, run_id=run_id, tool=tool,
            local_run_dir=local_run_dir,
            state=state, exit_code=check_result.get("exit_code", ""),
            project_mgr=project_mgr,
        )
    elif category == "infra_failure":
        # P2 bounded auto-retry: ONE in-place resubmission per run (counting
        # manual restarts via restart_count — a run that already had a second
        # chance does not get a third without a human). Conservative by
        # design: safe before the full governor exists. If the retry cannot
        # be issued, fall back to the old flag-and-fail behavior.
        if _auto_retry_enabled() and int(remote.get("restart_count") or 0) < 1:
            result = _auto_retry_slurm(run_record, project_dir=project_dir)
            if result.get("success"):
                try:
                    push_job_notice(project_dir, run_id=run_id, tool=tool,
                                    state=state, category="infra_retry",
                                    job_id=job_id)
                except Exception:
                    pass
                log.info("dispatch_terminal: auto-retried %s after %s "
                         "(new job_id=%s)", run_id, state,
                         result.get("job_id", "?"))
                return category
        project_mgr.update_run(
            project_dir, run_id,
            {"lifecycle": "failed",
             "remote": {"retry_recommended": True,
                         "retry_reason": state}},
        )
    elif category == "deliberate":
        # check() already set lifecycle=cancelled; nothing to do.
        pass

    # Notify the user's session (toast via magnolia-job-notify.ts). A cancel
    # was user-initiated, so deliberate is the one silent terminal state.
    if category != "deliberate":
        try:
            push_job_notice(project_dir, run_id=run_id, tool=tool,
                            state=state, category=category, job_id=job_id)
        except Exception:
            pass  # push_job_notice never raises; belt-and-braces for dispatch
    return category


def poll_jobs(project_dir: str) -> dict[str, Any]:
    """One sweep: scan active runs, check each, dispatch terminals.

    Returns a JSON-able summary dict. Holds _SWEEP_LOCK non-blocking; if a
    previous sweep is still running, returns {"skipped": "busy"} without
    doing work.

    Never raises — one bad run is logged and counted in errors; one bad
    sweep is wrapped by the timer runner."""
    if not _SWEEP_LOCK.acquire(blocking=False):
        log.info("poll_jobs: previous sweep still running; skipping this tick")
        return {"skipped": "busy"}
    try:
        polled = transitioned = fetched = assessed = failures_captured = errors = 0
        for rec in _scan_active_runs(project_dir):
            run_id = rec["run_id"]
            remote = rec.get("remote") or {}
            job_id = remote.get("job_id")
            # No fallback: a record without a cluster is resolved from config
            # downstream. Guessing here would poll the wrong machine silently.
            cluster = remote.get("cluster")
            try:
                if remote.get("scheduler") == "local":
                    check_result = _check_local_terminal(rec)
                else:
                    check_result = ssh_slurm.check(
                        job_id=job_id, cluster=cluster, project_dir=project_dir,
                    )
            except Exception as e:
                log.warning("poll_jobs: check failed for %s (job %s): %s",
                            run_id, job_id, e)
                errors += 1
                continue
            if not check_result.get("success", True):
                log.warning("poll_jobs: check non-success for %s: %s",
                            run_id, check_result.get("error"))
                errors += 1
                continue
            polled += 1
            if not check_result.get("terminal"):
                _health_peek(rec, project_dir, _PROJECT_MANAGER)
                continue
            transitioned += 1
            try:
                category = dispatch_terminal(
                    rec, check_result,
                    project_dir=project_dir, project_mgr=_PROJECT_MANAGER,
                )
            except Exception as e:
                log.warning("poll_jobs: dispatch failed for %s: %s", run_id, e)
                errors += 1
                continue
            if category == "success":
                fetched += 1
                assessed += 1
            elif category == "science_failure":
                fetched += 1
                failures_captured += 1
        return {
            "polled": polled,
            "transitioned": transitioned,
            "fetched": fetched,
            "assessed": assessed,
            "failures_captured": failures_captured,
            "errors": errors,
        }
    finally:
        _SWEEP_LOCK.release()


import os  # noqa: E402 — late import keeps top tidy
import json  # noqa: E402


# Set by server.py at import time so the worker thread sees the right project.
PROJECT_DIR_FOR_TIMER: str = ""

# Early-fatal patterns for the health peek (P2: deliberately narrow — GROMACS
# `.log` grep only; do NOT grow this into a parser framework).
_HEALTH_TOOL = "gromacs"
_HEALTH_PATTERNS = ("Fatal error", "Segmentation fault", "MPI_ABORT")
_HEALTH_TAIL_LINES = 200


def _auto_retry_enabled() -> bool:
    """Single-retry auto-resubmission for infra failures. MAGNOLIA_AUTO_RETRY=0
    disables (mirrors the MAGNOLIA_HANDOVER_SKIP_EXPORT kill-switch pattern)."""
    return os.environ.get("MAGNOLIA_AUTO_RETRY", "") != "0"


def _auto_retry_slurm(rec: dict[str, Any], *, project_dir: str) -> dict[str, Any]:
    """Resubmit an infra-failed ssh-slurm run IN PLACE, once.

    Uses submit(restart_of=run_id): same run_id, same remote dir (partial
    output preserved), begin_restart resets the record to a clean in-flight
    state so the poller re-tracks the new job_id. The command comes from the
    L4 manifest written at submit time; resources from the run record (A0).
    """
    remote = rec.get("remote") or {}
    local_run_dir = Path(remote.get("local_run_dir", ""))
    try:
        manifest = json.loads(
            (local_run_dir / ".magnolia" / "manifest.json").read_text())
    except Exception as e:
        return {"success": False, "error_kind": "manifest_unreadable",
                "error": f"auto-retry needs the L4 manifest: {e}"}
    command = manifest.get("command")
    if not command:
        return {"success": False, "error_kind": "no_command",
                "error": "manifest has no command"}
    resources = rec.get("resources") or {}
    return ssh_slurm.submit(
        command=command,
        working_dir=str(local_run_dir),
        project_dir=project_dir,
        cluster=remote.get("cluster"),
        account=resources.get("account"),
        qos=resources.get("qos"),
        partition=resources.get("partition"),
        ncores=int(resources.get("ncores") or 4),
        memory=resources.get("memory", "4GB"),
        time_limit=resources.get("time_limit", "24:00:00"),
        tool=rec.get("tool"),
        restart_of=rec.get("run_id"),
        system_tags=rec.get("system_tags") or None,
    )


def _health_peek(rec: dict[str, Any], project_dir: str, project_mgr: Any) -> None:
    """Mid-run early-error peek for LOCAL gromacs runs (P2: start narrow).

    Scans the tail of *.log in the local run dir for fatal patterns; on a
    fresh hit, flags remote.health in the run record and emits one
    health_warning job notice. Never raises; never touches ssh-slurm runs
    (their logs live on the cluster until fetch)."""
    remote = rec.get("remote") or {}
    if remote.get("scheduler") != "local":
        return
    if str(rec.get("tool", "")).lower() != _HEALTH_TOOL:
        return
    health = remote.get("health") or {}
    if health.get("status"):
        return  # already flagged — do not spam every sweep
    local_run_dir = Path(remote.get("local_run_dir", ""))
    if not local_run_dir.is_dir():
        return
    hit_pattern = None
    hit_file = None
    for p in sorted(local_run_dir.glob("*.log")):
        tail = _tail(p, _HEALTH_TAIL_LINES)
        for pat in _HEALTH_PATTERNS:
            if pat in tail:
                hit_pattern, hit_file = pat, p.name
                break
        if hit_pattern:
            break
    if not hit_pattern:
        return
    try:
        project_mgr.update_run(
            project_dir, rec["run_id"],
            {"remote": {"health": {"status": "error_seen",
                                    "pattern": hit_pattern,
                                    "file": hit_file,
                                    "at": datetime.now(timezone.utc).isoformat()}}},
        )
    except Exception as e:
        log.warning("health_peek: could not flag %s: %s", rec.get("run_id"), e)
        return
    try:
        from compchem_memory.job_notices import push_job_notice
        push_job_notice(project_dir, run_id=rec["run_id"], tool=rec.get("tool", ""),
                        state=hit_pattern, category="health_warning",
                        job_id=remote.get("job_id"))
    except Exception:
        pass


def _resolve_poll_interval_seconds() -> int:
    """Poll interval in seconds. Default 5 min, overridable via
    MAGNOLIA_POLL_INTERVAL_MIN. Bad / zero / negative values fall back."""
    default = 5 * 60
    raw = os.environ.get("MAGNOLIA_POLL_INTERVAL_MIN")
    if not raw:
        return default
    try:
        minutes = int(raw)
        if minutes <= 0:
            return default
        return minutes * 60
    except ValueError:
        return default


def _poll_tick(project_dir: str) -> None:
    """One timer tick. Wraps poll_jobs so a timer firing NEVER raises."""
    try:
        poll_jobs(project_dir)
    except Exception as e:
        log.warning("_poll_tick error: %s", e)


def _run_poll_timer_background_worker() -> None:
    """Startup sweep + interval loop. Pulled out for testability."""
    import time
    interval = _resolve_poll_interval_seconds()
    # Startup catch-up sweep BEFORE entering the sleep loop: reconciles any
    # jobs that finished while opencode was closed.
    _poll_tick(PROJECT_DIR_FOR_TIMER)
    while True:
        time.sleep(interval)
        _poll_tick(PROJECT_DIR_FOR_TIMER)


def run_poll_timer_background(project_dir: str) -> None:
    """Spawn the daemon thread. Called once from server.py at import."""
    global PROJECT_DIR_FOR_TIMER
    PROJECT_DIR_FOR_TIMER = project_dir
    if os.environ.get("MAGNOLIA_DISABLE_BACKGROUND_POLLER"):
        log.info("background poller disabled (MAGNOLIA_DISABLE_BACKGROUND_POLLER)")
        return
    t = threading.Thread(target=_run_poll_timer_background_worker, daemon=True)
    t.start()


def _check_local_terminal(record: dict[str, Any]) -> dict[str, Any]:
    """Terminal check for a local run. Authoritative signal is the exit-code
    sentinel; PID liveness is only a secondary 'still running' hint. Never
    raises — returns {success: False, ...} on unexpected error so the poller
    counts it and moves on."""
    remote = record.get("remote") or {}
    sentinel = remote.get("exit_sentinel")
    try:
        if sentinel and Path(sentinel).exists():
            raw = Path(sentinel).read_text().strip()
            try:
                n = int(raw)
            except ValueError:
                return {"success": True, "terminal": False}  # partial write; retry
            return {"success": True, "terminal": True,
                    "state": "COMPLETED" if n == 0 else "FAILED", "exit_code": n}
        # No sentinel yet: is the process still alive?
        pid = int(str(remote.get("job_id", "")).split("_")[1])
        try:
            os.kill(pid, 0)
            return {"success": True, "terminal": False}
        except PermissionError:
            return {"success": True, "terminal": False}
        except ProcessLookupError:
            return {"success": True, "terminal": True, "state": "CRASHED", "exit_code": 1}
    except Exception as e:  # never raise into the sweep
        return {"success": False, "error": str(e)}


def _scan_active_runs(project_dir: str) -> list[dict[str, Any]]:
    """Return ssh-slurm and local runs in lifecycle ∈ {submitted, running} with
    a job_id.

    Defensive: a corrupt or non-dict YAML is skipped-and-logged. A run with
    no job_id (e.g. a 'submitting' breadcrumb) is also skipped — only
    pollable runs are returned.
    """
    runs_dir = Path(project_dir) / ".magnolia" / "runs"
    if not runs_dir.exists():
        return []
    active: list[dict[str, Any]] = []
    for f in sorted(runs_dir.glob("*.yaml")):
        if f.name == "INDEX.yaml":
            continue
        try:
            data = yaml.safe_load(f.read_text())
        except yaml.YAMLError as e:
            log.warning("poller: skip unparseable %s: %s", f.name, e)
            continue
        if not isinstance(data, dict):
            log.warning("poller: skip non-dict %s", f.name)
            continue
        remote = data.get("remote") or {}
        if remote.get("scheduler") not in ("ssh-slurm", "local"):
            continue
        if data.get("lifecycle") not in ("submitted", "running"):
            continue
        if not remote.get("job_id"):
            continue
        active.append(data)
    return active
