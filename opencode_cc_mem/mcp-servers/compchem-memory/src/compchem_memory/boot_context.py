"""Pre-render boot-context.md via assemble_context, so opencode `instructions` loads
project-level memory automatically on every session start.

SPEC GUARDRAIL: must call assemble_context (not custom assembly). This preserves
the budget allocation (§1.4 anti-windup) and corrected tier precedence (§2.6).
"""

from pathlib import Path

from compchem_memory.context_assembly import assemble_context


def regenerate_boot_context(
    project_dir: str,
    skills_dir: str | None = None,
    token_budget: int = 4000,
) -> str:
    """Write .magnolia/boot-context.md with prerendered project memory.
    Returns the path of the written file."""
    result = assemble_context(
        task_description="project boot context",
        project_dir=project_dir,
        skills_dir=skills_dir,
        token_budget=token_budget,
    )

    out_path = Path(project_dir) / ".magnolia" / "boot-context.md"
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(result.content)
    return str(out_path)
