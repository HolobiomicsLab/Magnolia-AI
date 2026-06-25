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


_INSTRUCTION = (
    "Relevant past pitfalls were found for this {tool} submission (see "
    "'pitfalls'). Verify whether each applies to THIS submission; adjust "
    "parameters if needed; then resubmit with acknowledge=True to proceed. "
    "The job was held before launch — no compute was spent."
)


def recall_gate(
    tool: str | None, command: str, project_dir: str | None, acknowledge: bool,
) -> dict[str, Any] | None:
    """Return a hold-dict if the submission should be held, else None to proceed."""
    try:
        if acknowledge:
            return None
        if not tool:
            return None
        hits = _warnings_for_tool(tool, project_dir)
        if not hits:
            return None
        pitfalls = [
            {"title": h.get("title"), "type": h.get("type"),
             "provisional": h.get("provisional", False),
             "summary": h.get("summary", ""), "source": h.get("source", "")}
            for h in hits
        ]
        return {
            "success": False,
            "held": True,
            "reason": "recall_gate",
            "tool": tool,
            "pitfalls": pitfalls,
            "instruction": _INSTRUCTION.format(tool=tool),
        }
    except Exception as e:  # fail-open: a memory glitch must never block a submit
        log.warning("recall_gate failed open: %s", e)
        return None
