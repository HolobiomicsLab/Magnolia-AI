"""compchem-memory MCP server: Enhanced three-tier memory store for computational chemistry."""

import json
import os
from pathlib import Path
from typing import Any

from fastmcp import FastMCP

from compchem_memory.tiers.session import SessionManager
from compchem_memory.tiers.project import ProjectManager
from compchem_memory.tiers.skill import SkillManager
from compchem_memory.learning.assessor import assess_run
from compchem_memory.learning.distiller import distill_session
from compchem_memory.learning.consolidator import consolidate_tier
from compchem_memory.index import MemoryIndex
from compchem_memory.context_assembly import assemble_context
from compchem_memory.retrieval import select_relevant_entries, select_relevant_skills
from compchem_memory.scanning import (
    scan_memory_headers,
    scan_skills_headers,
    format_manifest,
)
from compchem_memory.extraction import AutomaticMemoryExtractor
from compchem_memory.compaction import (
    maybe_compact_session,
    compact_session_to_notes,
    estimate_tokens,
)
from compchem_memory.health import run_health_check
from compchem_memory.notebook import generate_notebook
from compchem_memory.storage import (
    SKILLS_DIR as _SKILLS_DIR,
    ensure_project_store,
    resolve_project_dir,
)

SKILLS_DIR = Path(os.environ.get("MAGNOLIA_SKILLS_DIR", str(_SKILLS_DIR)))
PROJECT_DIR = os.environ.get("MAGNOLIA_PROJECT_DIR", ".")
GLOBAL_BASE = Path(os.path.expanduser("~/.magnolia"))

mcp = FastMCP("compchem-memory")

session_mgr: SessionManager | None = None
project_mgr: ProjectManager | None = None
skill_mgr: SkillManager | None = None
memory_idx: MemoryIndex | None = None
_extractor: AutomaticMemoryExtractor | None = None


def _get_session_mgr(project_dir: str) -> SessionManager:
    global session_mgr
    sessions_dir = Path(project_dir) / "sessions"
    if session_mgr is None or str(session_mgr.sessions_dir) != str(sessions_dir):
        session_mgr = SessionManager(sessions_dir)
    return session_mgr


def _get_project_mgr() -> ProjectManager:
    global project_mgr
    if project_mgr is None:
        project_mgr = ProjectManager(GLOBAL_BASE)
    return project_mgr


def _get_skill_mgr() -> SkillManager:
    global skill_mgr
    if skill_mgr is None:
        skill_mgr = SkillManager(SKILLS_DIR)
    return skill_mgr


def _get_index() -> MemoryIndex:
    global memory_idx
    if memory_idx is None:
        memory_idx = MemoryIndex(GLOBAL_BASE)
    return memory_idx


def _get_extractor(project_dir: str | None = None) -> AutomaticMemoryExtractor:
    global _extractor
    if _extractor is None:
        _extractor = AutomaticMemoryExtractor(project_dir)
    return _extractor


def _resolve_project_store(project_dir: str | None = None) -> str:
    pd = resolve_project_dir(project_dir, PROJECT_DIR)
    ensure_project_store(pd)
    return pd


# ── v1 Tools (preserved, enhanced) ──────────────────────────────────────────


@mcp.tool()
def memory_get_context(
    task_description: str,
    project_dir: str | None = None,
    token_budget: int = 8000,
    conversation_history: list[dict[str, Any]] | None = None,
) -> str:
    """Multi-stage context assembly pipeline. Retrieves relevant entries from
    all three tiers (session, project, skill) with token budget management.
    Applies semantic scoring to select the most relevant project-tier entries.
    Optionally pass conversation_history to improve tool-diversity filtering."""
    pd = _resolve_project_store(project_dir)
    result = assemble_context(
        task_description=task_description,
        project_dir=pd,
        skills_dir=str(SKILLS_DIR),
        token_budget=token_budget,
        conversation_history=conversation_history,
    )
    return json.dumps(
        {
            "content": result.content,
            "tokens_used": result.tokens_used,
            "sources": result.sources,
        },
        indent=2,
    )


@mcp.tool()
def memory_record_session(
    event_type: str,
    data: dict[str, Any],
    project_dir: str | None = None,
) -> str:
    """Append a structured entry to the current session log (JSONL).
    data should contain: tool_name, args, result_summary, error (if any)."""
    pd = _resolve_project_store(project_dir)
    sess_m = _get_session_mgr(pd)
    return sess_m.record(event_type, data, pd)


@mcp.tool()
def memory_record_learning(
    title: str,
    content: str,
    tags: list[str] | None = None,
    source: str = "auto",
    entry_type: str = "note",
    tools: list[str] | None = None,
    confidence: float = 0.5,
    project_dir: str | None = None,
) -> str:
    """Propose a new project-tier entry with typed frontmatter. Writes to staging
    area; entries become active after confirmation or N consistent observations."""
    pd = _resolve_project_store(project_dir)
    proj_m = _get_project_mgr()

    # Check for similar existing staging entry to bump instead of duplicate
    similar = proj_m.find_similar_staging(pd, title, tags or [])
    if similar:
        proj_m.bump_observation_count(pd, similar)
        promoted = proj_m.auto_promote_staging(pd)
        return json.dumps({
            "status": "bumped",
            "similar_entry": similar,
            "promoted": promoted,
        })

    result = proj_m.create_entry(
        pd,
        title,
        content,
        tags=tags,
        source=source,
        staging=True,
        entry_type=entry_type,
        tools=tools,
        confidence=confidence,
    )

    promoted = proj_m.auto_promote_staging(pd)
    return json.dumps({
        "status": "created",
        "path": result,
        "promoted": promoted,
    })


@mcp.tool()
def memory_search(
    keyword: str = "",
    tags: list[str] | None = None,
    project_dir: str | None = None,
) -> str:
    """Keyword + tag search across all tiers. Returns matching entries with
    tier label, date, confidence. Uses semantic scoring for project tier."""
    pd = _resolve_project_store(project_dir)
    results = []

    skill_m = _get_skill_mgr()
    results.extend(skill_m.search_skills(keyword=keyword, tags=tags))

    proj_m = _get_project_mgr()
    entries = proj_m.search_entries(pd, keyword=keyword, tags=tags)
    results.extend(entries)

    sess_m = _get_session_mgr(pd)
    session_matches = sess_m.search(keyword)
    for sm in session_matches[:5]:
        sm["tier"] = "session"
        sm["confidence"] = 0.5
        results.append(sm)

    return json.dumps(results, indent=2)


@mcp.tool()
def memory_get_run_history(
    project_dir: str | None = None,
) -> str:
    """Return run history for the current project (list of YAML records with
    status, scores, dates)."""
    pd = _resolve_project_store(project_dir)
    proj_m = _get_project_mgr()
    return json.dumps(proj_m.get_run_history(pd), indent=2)


@mcp.tool()
def memory_record_run(
    run_id: str,
    tool: str,
    status: str,
    metrics: dict[str, Any] | None = None,
    errors_solved: list[str] | None = None,
    project_dir: str | None = None,
) -> str:
    """Append a new run record to the project's run history index."""
    pd = _resolve_project_store(project_dir)
    proj_m = _get_project_mgr()
    return proj_m.record_run(pd, run_id, tool, status, metrics, errors_solved)


@mcp.tool()
def memory_promote(
    entry_name: str,
    project_dir: str | None = None,
    skills_dir: str | None = None,
) -> str:
    """Move an entry from project tier to skill tier. Requires explicit invocation
    (human-gated)."""
    pd = _resolve_project_store(project_dir)
    sd = skills_dir or str(SKILLS_DIR)
    proj_m = _get_project_mgr()
    return proj_m.promote_to_skill(pd, entry_name, sd)


@mcp.tool()
def memory_consolidate(
    tier: str = "project",
    project_dir: str | None = None,
    stale_days: int = 90,
    max_entries: int = 50,
) -> str:
    """Merge duplicates, expire stale entries, trim to budget within one tier.
    Can be called on-demand or scheduled."""
    pd = _resolve_project_store(project_dir)
    result = consolidate_tier(
        tier,
        pd,
        stale_days=stale_days,
        max_entries=max_entries,
        skills_dir=str(SKILLS_DIR),
    )
    return json.dumps(result, indent=2)


@mcp.tool()
def post_run_assess(
    run_dir: str,
    tool: str,
    exit_code: int = 0,
    project_dir: str | None = None,
) -> str:
    """After a computation completes: check exit code, verify output files exist,
    extract metrics, flag quality issues. Records run in memory automatically."""
    assessment = assess_run(run_dir, tool, exit_code)
    pd = _resolve_project_store(project_dir)
    proj_m = _get_project_mgr()
    run_id = Path(run_dir).name
    proj_m.record_run(
        pd,
        run_id=run_id,
        tool=tool,
        status=assessment.get("overall", "pass" if exit_code == 0 else "failed"),
        metrics=assessment.get("metrics", {}),
        quality_flags=assessment.get("quality_flags", []),
        errors_solved=[],
    )
    sess_m = _get_session_mgr(pd)
    sess_m.record(
        "post_run_assess",
        {"run_dir": run_dir, "tool": tool, "assessment": assessment},
        pd,
    )
    return json.dumps(assessment, indent=2)


@mcp.tool()
def memory_confirm(
    entry_name: str,
    project_dir: str | None = None,
) -> str:
    """Confirm a staging entry, moving it to the active project entries."""
    pd = _resolve_project_store(project_dir)
    proj_m = _get_project_mgr()
    return proj_m.confirm_staging(pd, entry_name)


# ── v2 New Tools ─────────────────────────────────────────────────────────────


@mcp.tool()
def memory_select_relevant(
    task_description: str,
    project_dir: str | None = None,
    max_selections: int = 5,
    token_budget: int = 12000,
) -> str:
    """Semantic memory selection: scores and selects the most relevant project-tier
    entries for a given task. Uses heuristic scoring based on title, description,
    tags, tools, entry type, and confidence."""
    pd = _resolve_project_store(project_dir)
    entries = select_relevant_entries(
        task_description,
        pd,
        budget=token_budget,
        max_selections=max_selections,
    )
    output = []
    for e in entries:
        output.append(
            {
                "filename": e["filename"],
                "title": e["title"],
                "type": e.get("type", "note"),
                "relevance_score": e.get("relevance_score", 0),
            }
        )
    return json.dumps(output, indent=2)


@mcp.tool()
def memory_extract_from_session(
    project_dir: str | None = None,
) -> str:
    """Automatic memory extraction: distills session logs into typed staging entries
    (error_resolution, success_pattern, parameter_guidance). Runs when thresholds
    are met (5K tokens or 3 tool calls since last extraction)."""
    pd = _resolve_project_store(project_dir)
    sess_m = _get_session_mgr(pd)
    log_path = sess_m.get_session_log_path()
    if not log_path:
        return json.dumps({"status": "no_active_session", "extracted": 0})

    extractor = _get_extractor(pd)
    session_path = Path(log_path)

    if not extractor.should_extract(session_path):
        return json.dumps({"status": "threshold_not_met", "extracted": 0})

    saved = extractor.extract_and_save(session_path, pd)
    return json.dumps(
        {
            "status": "extracted",
            "extracted": len(saved),
            "paths": saved,
        },
        indent=2,
    )


@mcp.tool()
def memory_compact_session(
    project_dir: str | None = None,
    model_context_window: int = 128000,
    max_notes_tokens: int = 6000,
) -> str:
    """Compact session by pruning old tool results and generating summary notes.
    Three-tier strategy: micro-compact, session-memory compact, auto-compact.
    Returns compaction notes if pruning occurred."""
    pd = _resolve_project_store(project_dir)
    sess_m = _get_session_mgr(pd)
    log_path = sess_m.get_session_log_path()
    if not log_path:
        return json.dumps({"status": "no_active_session"})

    session_path = Path(log_path)
    result = maybe_compact_session(session_path, model_context_window)

    if result is None:
        notes = compact_session_to_notes(session_path, max_notes_tokens)
        if notes:
            notes_dir = Path(pd) / "session-notes"
            notes_dir.mkdir(parents=True, exist_ok=True)
            from datetime import datetime, timezone

            ts = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")
            notes_path = notes_dir / f"compact_{ts}.md"
            notes_path.write_text(notes)
            return json.dumps(
                {
                    "status": "notes_generated",
                    "notes_path": str(notes_path),
                }
            )
        return json.dumps({"status": "no_compaction_needed"})

    return json.dumps(
        {
            "status": "compacted",
            "pruned_count": result.pruned_count,
            "tokens_before": result.tokens_before,
            "tokens_after": result.tokens_after,
        },
        indent=2,
    )


@mcp.tool()
def memory_search_errors(
    error_message: str,
    tool: str | None = None,
    project_dir: str | None = None,
    max_results: int = 5,
) -> str:
    """Search memory for similar past errors and their resolutions.
    Automatically invoked when a tool fails to find relevant fix history."""
    pd = _resolve_project_store(project_dir)
    proj_m = _get_project_mgr()
    entries = proj_m.search_entries(pd, keyword=error_message[:50], tags=["error-resolution"])
    if tool:
        entries = [e for e in entries if tool in str(e.get("tools", []))]

    # Also search session logs for error patterns
    sess_m = _get_session_mgr(pd)
    session_matches = sess_m.search(error_message[:50])
    session_errors = [m for m in session_matches if "error" in m.get("event_type", "")]

    results = {
        "project_entries": entries[:max_results],
        "session_errors": session_errors[:3],
        "total_matches": len(entries) + len(session_errors),
    }
    return json.dumps(results, indent=2)


@mcp.tool()
def memory_distill_session(
    project_dir: str | None = None,
) -> str:
    """Distill the current session into proposed project-tier entries.
    Extracts error resolutions, parameter guidance, and success patterns."""
    pd = _resolve_project_store(project_dir)
    sess_m = _get_session_mgr(pd)
    log_path = sess_m.get_session_log_path()
    if not log_path:
        return json.dumps({"status": "no_active_session", "proposed": 0})

    candidates = distill_session(log_path)
    if not candidates:
        return json.dumps({"status": "no_candidates", "proposed": 0})

    proj_m = _get_project_mgr()
    saved = []
    for c in candidates:
        path = proj_m.create_entry(
            pd,
            title=c["title"],
            content=c["content"],
            tags=c.get("tags", []),
            source="session_distillation",
            staging=True,
            entry_type=c.get("type", "note"),
            tools=c.get("tools", []),
            confidence=c.get("confidence", 0.5),
        )
        saved.append(path)

    return json.dumps({
        "status": "proposed",
        "proposed": len(saved),
        "paths": saved,
    }, indent=2)


@mcp.tool()
def memory_scan_headers(
    project_dir: str | None = None,
    tier: str = "project",
) -> str:
    """Fast header scan of memory entries (frontmatter only, no full content).
    Returns catalogue of titles, types, tags, tools for selection."""
    pd = _resolve_project_store(project_dir)

    if tier == "project":
        entries_dir = Path(pd) / "entries"
        headers = scan_memory_headers(entries_dir)
    elif tier == "skill":
        headers = scan_skills_headers(SKILLS_DIR)
    else:
        return json.dumps({"error": f"Unknown tier: {tier}. Use 'project' or 'skill'."})

    manifest = format_manifest(headers)
    return json.dumps(
        {
            "count": len(headers),
            "manifest": manifest,
        },
        indent=2,
    )


# ── Karpathy-style knowledge management tools ────────────────────────────────


@mcp.tool()
def memory_health_check(
    project_dir: str | None = None,
    stale_days: int = 90,
    min_confidence: float = 0.3,
    fix: bool = False,
) -> str:
    """Audit the knowledge base for staleness, contradictions, gaps, and orphaned entries.
    Returns a structured report. Default mode is dry-run (no side effects).
    Set fix=True to auto-resolve safe issues (remove broken refs, mark stale entries)."""
    pd = _resolve_project_store(project_dir)
    result = run_health_check(
        project_dir=pd,
        stale_days=stale_days,
        min_confidence=min_confidence,
        fix=fix,
    )
    return json.dumps(result, indent=2)


@mcp.tool()
def memory_notebook(
    start_date: str | None = None,
    end_date: str | None = None,
    section: str | None = None,
    project_dir: str | None = None,
) -> str:
    """Generate a chronological lab notebook timeline from sessions, runs, and entries.
    Returns markdown. Optionally filter by date range or section (entries, runs, sessions).
    This is a read-only view tool — it does not modify any data."""
    pd = _resolve_project_store(project_dir)
    return generate_notebook(
        project_dir=pd,
        start_date=start_date,
        end_date=end_date,
        section=section,
    )


@mcp.tool()
def memory_annotate(
    title: str,
    content: str,
    tags: list[str] | None = None,
    references: list[str] | None = None,
    notebook_section: str | None = None,
    project_dir: str | None = None,
) -> str:
    """Create a human-authored lab notebook entry (type 'note') with optional
    references (paper DOIs, PDB IDs, URLs) and notebook section label.
    Entries are created directly in the active entries area (not staging)."""
    pd = _resolve_project_store(project_dir)
    proj_m = _get_project_mgr()
    result = proj_m.create_entry(
        pd,
        title=title,
        content=content,
        tags=tags,
        source="human_annotation",
        staging=False,
        entry_type="note",
        references=references,
        notebook_section=notebook_section,
    )
    return json.dumps({"status": "created", "path": result})


# ── Resources (preserved from v1) ────────────────────────────────────────────


@mcp.resource("memory://skills/{tool_name}")
def get_skill_resource(tool_name: str) -> str:
    """Full skill file content for a given tool."""
    skill_m = _get_skill_mgr()
    content = skill_m.get_skill(tool_name)
    return content or f"No skill found for {tool_name}"


@mcp.resource("memory://project/index")
def get_project_index() -> str:
    """Project-tier entry catalogue for current project."""
    pd = _resolve_project_store()
    proj_m = _get_project_mgr()
    return json.dumps(proj_m.list_entries(pd), indent=2)


@mcp.resource("memory://project/entry/{name}")
def get_project_entry(name: str) -> str:
    """A single project-tier entry by name."""
    pd = _resolve_project_store()
    proj_m = _get_project_mgr()
    content = proj_m.get_entry(pd, name)
    return content or f"Entry not found: {name}"


@mcp.resource("memory://runs/index")
def get_runs_index() -> str:
    """Run history index for current project."""
    pd = _resolve_project_store()
    proj_m = _get_project_mgr()
    return json.dumps(proj_m.get_run_history(pd), indent=2)


if __name__ == "__main__":
    mcp.run()
