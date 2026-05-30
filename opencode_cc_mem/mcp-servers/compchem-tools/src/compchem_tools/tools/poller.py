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
    "REVOKED", "RV",  # rules/slurm.md: "admin intervention needed" — not retryable
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
    title = f"job-failure: {tool} {run_id} ({state})"
    head = err_tail or out_tail
    summary = "\n".join(head.splitlines()[:10]) if head else "(no log output)"
    body = (
        f"Tool: {tool}\nRun: {run_id}\nState: {state}\nExit: {exit_code}\n"
        f"Captured: {captured_at}\n\nLog head:\n```\n{summary}\n```\n"
    )
    project_mgr.create_entry(
        project_dir,
        title=title,
        content=body,
        tags=[tool, "job-failure", state.lower()],
        source="poller",
        staging=True,
        entry_type="note",
    )


from compchem_memory.learning.orchestrator import assess_and_record  # noqa: E402


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

    if category == "success":
        ssh_slurm.fetch(job_id=job_id, project_dir=project_dir)
        assess_and_record(
            run_dir=str(local_run_dir),
            tool=tool,
            exit_code=_parse_exit_code(check_result.get("exit_code", "0:0")),
            project_dir=project_dir,
            project_mgr=project_mgr,
            run_id=run_id,
        )
    elif category == "science_failure":
        ssh_slurm.fetch(job_id=job_id, project_dir=project_dir)
        capture_failure(
            project_dir=project_dir, run_id=run_id, tool=tool,
            local_run_dir=local_run_dir,
            state=state, exit_code=check_result.get("exit_code", ""),
            project_mgr=project_mgr,
        )
    elif category == "infra_failure":
        project_mgr.update_run(
            project_dir, run_id,
            {"lifecycle": "failed",
             "remote": {"retry_recommended": True,
                         "retry_reason": state}},
        )
    elif category == "deliberate":
        # check() already set lifecycle=cancelled; nothing to do.
        pass
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
            cluster = remote.get("cluster", "azzurra")
            try:
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


# Set by server.py at import time so the worker thread sees the right project.
PROJECT_DIR_FOR_TIMER: str = ""


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
    t = threading.Thread(target=_run_poll_timer_background_worker, daemon=True)
    t.start()


def _scan_active_runs(project_dir: str) -> list[dict[str, Any]]:
    """Return ssh-slurm runs in lifecycle ∈ {submitted, running} with a job_id.

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
        if remote.get("scheduler") != "ssh-slurm":
            continue
        if data.get("lifecycle") not in ("submitted", "running"):
            continue
        if not remote.get("job_id"):
            continue
        active.append(data)
    return active
