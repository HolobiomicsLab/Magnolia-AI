"""Pre-render boot-context.md via assemble_context, so opencode `instructions` loads
project-level memory automatically on every session start.

SPEC GUARDRAIL: must call assemble_context (not custom assembly). This preserves
the budget allocation (§1.4 anti-windup) and corrected tier precedence (§2.6).
"""

from pathlib import Path

from compchem_memory.context_assembly import assemble_context


def regenerate_boot_context(
    project_dir: str,
    token_budget: int = 10000,
) -> str:
    """Write .magnolia/boot-context.md with prerendered project memory.
    Returns the path of the written file.

    Budget was 6000; raised to 10000 (2026-09-16): the session tier's 20%
    share (~4.8 KB) could not cover a ~17 KB handover state, and the overflow
    fallback tail-sliced mid-line, dropping a restarted session's To do list.
    """
    result = assemble_context(
        task_description="project boot context",
        project_dir=project_dir,
        token_budget=token_budget,
    )

    # R5 surfacing counter: log every entry include to the SERVING store's
    # surfacing.jsonl. Boot regen never counts toward exposure (guard 1).
    try:
        from compchem_memory.context_assembly import _memory_store
        from compchem_memory.surfacing import log_surfacing
        log_surfacing(_memory_store(project_dir), result.sources, "boot_context")
    except Exception:  # noqa: BLE001 - telemetry must never break boot
        pass

    out_path = Path(project_dir) / ".magnolia" / "boot-context.md"
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(result.content)
    return str(out_path)
