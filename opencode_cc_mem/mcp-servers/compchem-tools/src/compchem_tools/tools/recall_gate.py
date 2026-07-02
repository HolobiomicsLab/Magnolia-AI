"""Pre-launch recall gate for submit_job. Fail-open: never raises, never blocks
on a memory problem. v1 surfaces tool-scoped pitfalls and holds the submission
until the caller resubmits with acknowledge=True."""
import logging
from typing import Any

log = logging.getLogger(__name__)


def _warnings_for_tool(tool, project_dir):
    # Indirection so tests can monkeypatch without importing compchem_memory.
    from compchem_memory.recall import warnings_for_tool
    return warnings_for_tool(tool, project_dir)


def _similar_runs(tool, system_tags, project_dir):
    # Indirection so tests can monkeypatch without importing compchem_memory.
    from compchem_memory.recall import similar_runs
    return similar_runs(tool, system_tags, project_dir)


_INSTRUCTION = (
    "Relevant context was found for this {tool} submission. If 'pitfalls' is "
    "non-empty, verify each applies and adjust parameters. If 'similar_runs' is "
    "non-empty, inspect the most similar past run's directory and model your "
    "configuration on it (its config/inputs are saved there). Then resubmit "
    "with acknowledge=True to proceed. Held before launch — no compute spent."
)


def recall_gate(
    tool: str | None, command: str, project_dir: str | None, acknowledge: bool,
    system_tags: list[str] | None = None,
) -> dict[str, Any] | None:
    """Hold-dict if the submission should be held (tool warnings and/or a
    similar prior run), else None. Fail-open: never raises."""
    try:
        if acknowledge:
            return None
        if not tool:
            return None
        warnings = _warnings_for_tool(tool, project_dir)
        similar = _similar_runs(tool, system_tags, project_dir) if system_tags else []
        if not warnings and not similar:
            return None
        pitfalls = [
            {"title": h.get("title"), "type": h.get("type"),
             "provisional": h.get("provisional", False),
             "summary": h.get("summary", ""), "source": h.get("source", "")}
            for h in warnings
        ]
        return {
            "success": False,
            "held": True,
            "reason": "recall_gate",
            "tool": tool,
            "pitfalls": pitfalls,
            "similar_runs": similar,
            "instruction": _INSTRUCTION.format(tool=tool),
        }
    except Exception as e:  # fail-open
        log.warning("recall_gate failed open: %s", e)
        return None
