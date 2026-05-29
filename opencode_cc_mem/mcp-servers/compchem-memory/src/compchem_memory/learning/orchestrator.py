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
) -> dict[str, Any]:
    """Run assess_run; record the assessment to runs/<id>.yaml.

    Returns the assessment dict (NOT a json string — that's the MCP tool's
    job). Designed for two callers:
      - post_run_assess MCP tool (wraps + json.dumps)
      - poller.dispatch_terminal (uses the dict directly)

    Session-event recording is the caller's responsibility — the poller
    operates outside a user session, and over-recording from there would
    pollute the distill stream.
    """
    assessment = assess_run(run_dir, tool, exit_code)
    run_id = Path(run_dir).name
    project_mgr.record_run(
        project_dir,
        run_id=run_id,
        tool=tool,
        status=assessment.get("overall", "pass" if exit_code == 0 else "fail"),
        metrics=assessment.get("metrics", {}),
        quality_flags=assessment.get("quality_flags", []),
        errors_solved=[],
    )
    return assessment
