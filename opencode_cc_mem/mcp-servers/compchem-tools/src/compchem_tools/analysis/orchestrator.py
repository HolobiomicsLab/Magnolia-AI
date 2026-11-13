"""Assess a run and record the result in project memory.

Moved from compchem_memory.learning.orchestrator to compchem-tools: assessment
is a tools concern; memory only stores the run record. See
docs/verified-run-promotion.md.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from compchem_tools.analysis.assessor import assess_run
from compchem_memory.tiers.project import ProjectManager


def assess_and_record(
    run_dir: str,
    tool: str,
    exit_code: int,
    project_dir: str,
    project_mgr: ProjectManager,
    run_id: str | None = None,
) -> dict[str, Any]:
    """Run assess_run; upsert the assessment into runs/<id>.yaml.

    Returns the assessment dict. Used by the post_run_assess MCP tool and by
    the job poller on terminal state."""
    assessment = assess_run(run_dir, tool, exit_code)
    if run_id is None:
        run_id = resolve_run_id(project_mgr, project_dir, run_dir)
    project_mgr.upsert_run(
        project_dir,
        run_id=run_id,
        tool=tool,
        status=assessment.get("overall", "pass" if exit_code == 0 else "fail"),
        metrics=assessment.get("metrics", {}),
        quality_flags=assessment.get("quality_flags", []),
        exit_code=exit_code,
    )
    return assessment


def resolve_run_id(
    project_mgr: ProjectManager, project_dir: str, run_dir: str
) -> str:
    """Canonical id for ``run_dir``: reuse the submit record when present."""
    try:
        rec = project_mgr.find_run_by_local_dir(project_dir, run_dir)
    except Exception:
        rec = None
    if rec and rec.get("run_id"):
        return rec["run_id"]
    return Path(run_dir).name


def backfill_run_assessments(
    project_dir: str,
    project_mgr: ProjectManager,
    tool: str = "haddock3",
) -> list[str]:
    """Assess completed run records whose status is still null.

    Needed because older runs were fetched manually and never went through
    post_run_assess. Returns the run_ids that were assessed."""
    runs_dir = Path(project_dir) / ".magnolia" / "runs"
    if not runs_dir.exists():
        return []
    import yaml

    assessed: list[str] = []
    for path in sorted(runs_dir.glob("*.yaml")):
        try:
            rec = yaml.safe_load(path.read_text()) or {}
        except Exception:
            continue
        if rec.get("status") is not None:
            continue
        if rec.get("lifecycle") != "completed":
            continue
        run_id = rec.get("run_id") or path.stem.split("_", 1)[-1]
        run_dir = (rec.get("remote") or {}).get("local_run_dir")
        if not run_dir or not Path(run_dir).exists():
            continue
        assess_and_record(
            run_dir=run_dir,
            tool=rec.get("tool", tool),
            exit_code=0,
            project_dir=project_dir,
            project_mgr=project_mgr,
            run_id=run_id,
        )
        assessed.append(run_id)
    return assessed
