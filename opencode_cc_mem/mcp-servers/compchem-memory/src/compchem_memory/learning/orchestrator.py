"""Side-effect-free orchestration: assess a run + record it in project memory.

Extracted from server.post_run_assess so both the MCP tool and the async
poller call ONE implementation. No FastMCP, no module-level side effects,
no started threads — safe to import from a daemon thread.
"""
from __future__ import annotations
from pathlib import Path
from typing import Any

from compchem_memory.learning.assessor import assess_run
from compchem_memory.tiers.project import ProjectManager


def assess_and_record(
    run_dir: str,
    tool: str,
    exit_code: int,
    project_dir: str,
    project_mgr: ProjectManager,
    run_id: str | None = None,
) -> dict[str, Any]:
    """Run assess_run; record the assessment to runs/<id>.yaml.

    Returns the assessment dict (NOT a json string — that's the MCP tool's
    job). Designed for two callers:
      - post_run_assess MCP tool (wraps + json.dumps)
      - poller.dispatch_terminal (uses the dict directly)

    Session-event recording is the caller's responsibility — the poller
    operates outside a user session, and over-recording from there would
    pollute the distill stream.

    run_id: explicit magnolia run_id to key the YAML on. When None (default),
    the run_dir is resolved against existing run records first — submit paths
    pin ``remote.local_run_dir`` — so an assessment lands on the SAME record
    the submit wrote instead of forking a twin keyed on basename(run_dir).
    Falls back to basename(run_dir) when no record matches (e.g. a tool run
    outside submit_job). The poller passes run_id explicitly.
    """
    assessment = assess_run(run_dir, tool, exit_code)
    if run_id is None:
        run_id = resolve_run_id(project_mgr, project_dir, run_dir)
    # Upsert (not record_run): merge the assessment into the EXISTING run_id
    # record so a same-day-completed job keeps its remote/lifecycle data instead
    # of having it overwritten, and a next-day assessment doesn't fork a twin.
    project_mgr.upsert_run(
        project_dir,
        run_id=run_id,
        tool=tool,
        status=assessment.get("overall", "pass" if exit_code == 0 else "fail"),
        metrics=assessment.get("metrics", {}),
        quality_flags=assessment.get("quality_flags", []),
    )
    return assessment


def resolve_run_id(
    project_mgr: ProjectManager, project_dir: str, run_dir: str
) -> str:
    """Canonical id for ``run_dir``.

    Prefer the existing run record's run_id — submit paths pin
    ``remote.local_run_dir``, so this is how an assessment joins the submit
    record instead of forking a twin. Fall back to basename(run_dir) for runs
    that never went through submit_job.
    """
    try:
        rec = project_mgr.find_run_by_local_dir(project_dir, run_dir)
    except Exception:
        rec = None
    return (rec or {}).get("run_id") or Path(run_dir).name
