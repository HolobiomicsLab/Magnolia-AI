"""Pre-render boot-context.md via assemble_context, so opencode `instructions` loads
project-level memory automatically on every session start.

SPEC GUARDRAIL: must call assemble_context (not custom assembly). This preserves
the budget allocation (§1.4 anti-windup) and corrected tier precedence (§2.6).
"""

from pathlib import Path

from compchem_memory.context_assembly import _memory_store, assemble_context
from compchem_memory.writer_lease import file_fingerprint, project_writer_lease


def _boot_source_fingerprint(project_dir: str) -> dict[str, str | None]:
    """Fingerprint every file that can contribute to boot-context assembly."""
    store = _memory_store(project_dir)
    paths = [store / "GOAL.md", store / ".handover-state.md"]
    for directory, pattern in (
        ("entries", "*.md"),
        ("staging", "*.md"),
        ("runs", "*.yaml"),
        ("sessions", "*.jsonl"),
    ):
        paths.extend(sorted((store / directory).glob(pattern)))
    return file_fingerprint(paths)


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
    out_path = Path(project_dir) / ".magnolia" / "boot-context.md"
    while True:
        source_read = _boot_source_fingerprint(project_dir)
        result = assemble_context(
            task_description="project boot context",
            project_dir=project_dir,
            token_budget=token_budget,
            compact_project_tier=True,
        )

        # Assembly may call an LLM. Lease only validation and the derived write;
        # recompute when the source view changed instead of writing stale output.
        with project_writer_lease(project_dir):
            if _boot_source_fingerprint(project_dir) != source_read:
                continue
            out_path.parent.mkdir(parents=True, exist_ok=True)
            out_path.write_text(result.content)
            break

    # R5 surfacing counter: log only the source set that was actually served.
    try:
        from compchem_memory.surfacing import log_surfacing
        log_surfacing(_memory_store(project_dir), result.sources, "boot_context")
    except Exception:  # noqa: BLE001 - telemetry must never break boot
        pass
    return str(out_path)
