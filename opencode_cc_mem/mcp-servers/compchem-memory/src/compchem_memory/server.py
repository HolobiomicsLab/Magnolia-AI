"""compchem-memory MCP server: memory-backed store for computational chemistry.

Tier model since 2026-09: session + project/staging memory tiers in .magnolia/,
with elevation to durable doctrine via the git-tracked rules/ directory (the
former .magnolia/skills skill tier was retired; protocols live in
.opencode/skills/, loaded on demand by the agent)."""

import json
import os
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

_BOOT_T0 = time.monotonic()  # anchor for server-import latency in the boot-timing rows

from fastmcp import FastMCP

from compchem_memory.capture import captured, get_session_manager
from compchem_memory.tiers.session import SessionManager
from compchem_memory.tiers.project import ProjectManager
from compchem_memory.learning.consolidator import consolidate_tier
from compchem_memory.context_assembly import assemble_context, _memory_store
from compchem_memory.retrieval import select_relevant_entries
from compchem_memory.scanning import (
    scan_memory_headers,
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
    ensure_project_store,
    resolve_project_dir,
    resolved_rules_dir,
)
from compchem_memory.project_guard import check_project
from compchem_memory.startup_scan import scan_and_distill, _opencode_available
from compchem_memory.llm import is_llm_available
from compchem_memory.opencode_ingest import _latest_sid, distill_session_transcript

RULES_DIR = resolved_rules_dir()
PROJECT_DIR = os.environ.get("MAGNOLIA_PROJECT_DIR", ".")
GLOBAL_BASE = Path(os.path.expanduser("~/.magnolia"))


def _claim_once(name: str) -> bool:
    """Claim a once-per-PROCESS side effect (boot pipeline, distill timer).

    server.py can be imported twice in one process: ``python -m
    compchem_memory.server`` registers it only as ``__main__``, so a later
    ``import compchem_memory.server`` re-executes the module body — including
    the module-level boot/timer spawns below. A module-global flag would not
    be shared between the two module objects, so the claim lives in the
    process environment, keyed by project dir. First caller gets True and
    must run; later callers get False and skip. (Children inherit the marker,
    so keep this for side effects that must have a single in-process owner —
    the memory server is spawned by opencode, never the reverse.)"""
    key = f"MAGNOLIA_ONCE_{name}_{abs(hash(PROJECT_DIR))}"
    if os.environ.get(key):
        return False
    os.environ[key] = "1"
    return True


def _build_boot_steps(project_dir):
    """Ordered boot-worker steps. Handover runs after distillation (so the just-
    ended session is captured) and before boot-context (so assemble_context sees
    the fresh handover). Extracted as a function so the ordering is testable."""
    from compchem_memory.startup_scan import scan_and_distill
    from compchem_memory.handover import generate_handover
    from compchem_memory.boot_context import regenerate_boot_context
    from compchem_memory.audit import run_audit

    return [
        ("startup_scan", lambda: scan_and_distill(project_dir)),
        ("handover", lambda: generate_handover(project_dir)),
        ("boot_context", lambda: regenerate_boot_context(project_dir)),
        ("audit", lambda: run_audit(project_dir)),
    ]


def _run_startup_scan_background():
    """Run startup_scan, handover, boot_context, audit, then write .current-session-id.

    Every step is timed: a row per step goes to .magnolia/boot-timing.jsonl and
    to stderr, so a slow restart is decomposable (step wall time; per-LLM-call
    detail already lives in llm-timing.jsonl). Guarded to run once per process —
    see _claim_once."""
    if not _claim_once("boot"):
        return
    import threading
    import datetime

    def _log_boot_step(step: str, ms: float, ok: bool = True, error: object = None) -> None:
        row = {
            "ts": datetime.datetime.now(datetime.timezone.utc).isoformat(),
            "step": step,
            "ms": int(ms),
            "ok": ok,
        }
        if error is not None:
            row["error"] = str(error)[:200]
        try:
            p = Path(PROJECT_DIR) / ".magnolia" / "boot-timing.jsonl"
            p.parent.mkdir(parents=True, exist_ok=True)
            with p.open("a", encoding="utf-8") as f:
                f.write(json.dumps(row) + "\n")
        except Exception:
            pass
        suffix = "" if ok else f" (error: {error})"
        print(f"[boot-timing] {step}: {int(ms)} ms{suffix}")

    def _worker():
        _log_boot_step("server_import", (time.monotonic() - _BOOT_T0) * 1000)
        t_all = time.monotonic()
        for step_name, step_fn in _build_boot_steps(PROJECT_DIR):
            t0 = time.monotonic()
            try:
                step_fn()
            except Exception as e:
                print(f"[{step_name}] error: {e}")
                _log_boot_step(step_name, (time.monotonic() - t0) * 1000, ok=False, error=e)
            else:
                _log_boot_step(step_name, (time.monotonic() - t0) * 1000)
        t0 = time.monotonic()
        try:
            session_id = datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%d_%H%M%S")
            current = Path(PROJECT_DIR) / ".magnolia" / ".current-session-id"
            current.parent.mkdir(parents=True, exist_ok=True)
            current.write_text(session_id)
        except Exception as e:
            print(f"[session_id] error: {e}")
        _log_boot_step("total", (time.monotonic() - t_all) * 1000)

    threading.Thread(target=_worker, daemon=True).start()


_run_startup_scan_background()


def _resolve_distill_interval_seconds() -> int:
    """Distillation timer interval in seconds. Default 20 minutes; overridable
    via MAGNOLIA_DISTILL_INTERVAL_MIN. A bad value falls back to the default."""
    default = 20 * 60
    raw = os.environ.get("MAGNOLIA_DISTILL_INTERVAL_MIN")
    if not raw:
        return default
    try:
        minutes = int(raw)
        if minutes <= 0:
            return default
        return minutes * 60
    except ValueError:
        return default


# DeepSeek peak-pricing windows (UTC, weekdays): 01:00-04:00 and 06:00-10:00
# (= Beijing 09:00-12:00 / 14:00-18:00), when token prices are 2x. Everything
# else — the rest of the day and weekends — is off-peak.
_PEAK_WINDOWS_UTC = ((1 * 60, 4 * 60), (6 * 60, 10 * 60))


def _is_peak_time(now: datetime | None = None) -> bool:
    """True during a DeepSeek peak-pricing window. `now` is injectable for
    tests; defaults to the current UTC time."""
    now = now or datetime.now(timezone.utc)
    if now.weekday() >= 5:  # Sat/Sun are entirely off-peak
        return False
    minutes = now.hour * 60 + now.minute
    return any(start <= minutes < end for start, end in _PEAK_WINDOWS_UTC)


def _offpeak_gate_disabled() -> bool:
    """Kill-switch: MAGNOLIA_OFFPEAK_DISABLE=1 restores 24/7 sweeping."""
    return (os.environ.get("MAGNOLIA_OFFPEAK_DISABLE") or "").strip().lower() in {
        "1", "true", "yes", "on",
    }


def _distill_timer_tick(project_dir: str) -> None:
    """One timer tick: sweep the project for undistilled sessions, unless a
    DeepSeek peak-pricing window is active — then the sweep is deferred and
    the backlog runs at the next off-peak tick. The boot sweep and manual
    memory_distill_session are deliberately ungated. Never raises — a timer
    failure must not crash the server."""
    try:
        # Smoke detector (Phase 0): cadence-gated, off the boot path. Runs the
        # distiller canary + boot-duplication + dead-man checks; on drift the
        # canary freeze flag halts auto-promotion. MAGNOLIA_SMOKE_INTERVAL_H
        # (default 12, 0 = off).
        from compchem_memory import smoke
        smoke.maybe_run_scheduled(project_dir)
        if not _offpeak_gate_disabled() and _is_peak_time():
            print("[distill_timer] peak hours (UTC) — skipping sweep; "
                  "backlog will run at the next off-peak tick")
            return
        scan_and_distill(project_dir)
    except Exception as e:
        print(f"[distill_timer] tick error: {e}")


def _run_distill_timer_background() -> None:
    """Daemon thread: sweep distillation on a wall-clock interval, so distillation
    is robust to a long-lived server (no reboot to trigger startup_scan).
    Guarded to run once per process — see _claim_once."""
    if not _claim_once("distill_timer"):
        return
    import threading
    import time

    interval = _resolve_distill_interval_seconds()

    def _worker():
        while True:
            time.sleep(interval)
            _distill_timer_tick(PROJECT_DIR)

    threading.Thread(target=_worker, daemon=True).start()


_run_distill_timer_background()

mcp = FastMCP("compchem-memory")

project_mgr: ProjectManager | None = None
_extractor: AutomaticMemoryExtractor | None = None


def _get_session_mgr(project_dir: str) -> SessionManager:
    return get_session_manager(project_dir)


def _get_project_mgr() -> ProjectManager:
    global project_mgr
    if project_mgr is None:
        project_mgr = ProjectManager(GLOBAL_BASE)
    return project_mgr


def _get_extractor(project_dir: str | None = None) -> AutomaticMemoryExtractor:
    global _extractor
    if _extractor is None:
        _extractor = AutomaticMemoryExtractor(project_dir)
    return _extractor


def _resolve_project_store(project_dir: str | None = None) -> str:
    pd = resolve_project_dir(project_dir, PROJECT_DIR)
    ensure_project_store(pd)
    return pd


def _safe_version_commit(store: Path, message: str) -> None:
    """Commit the store to its versioning repo, best-effort. A versioning failure
    must never turn an already-saved distill into a tool error (mirrors the
    sweep's _commit_after_sweep contract)."""
    from compchem_memory import versioning
    try:
        versioning.commit_all(store, message)
    except Exception as e:  # noqa: BLE001 - versioning must never break distill
        print(f"[versioning] commit skipped: {e}")


def _drop_review_file(pd: str, filename: str) -> None:
    """Remove one review file from the shared magnolia-review/ dir, and the dir
    itself only if it's now empty. Scoped to the caller's own file so the two
    self-reflex passes (consolidation, promotion) don't delete each other's
    pending review."""
    review_dir = Path(pd) / "magnolia-review"
    (review_dir / filename).unlink(missing_ok=True)
    if review_dir.exists() and not any(review_dir.iterdir()):
        review_dir.rmdir()


def _project_switch_blocked_payload(guard) -> str:
    return json.dumps({
        "status": "project_switch_blocked",
        "pinned": guard.pinned,
        "requested": guard.requested,
        "message": (
            f"This session is pinned to project '{Path(guard.pinned).name}'. "
            f"To record work for '{Path(guard.requested).name}', start a new "
            f"opencode session."
        ),
    })


# ── v1 Tools (preserved, enhanced) ──────────────────────────────────────────


@mcp.tool()
@captured(source="compchem-memory")
def memory_get_context(
    task_description: str,
    project_dir: str | None = None,
    token_budget: int = 8000,
    conversation_history: list[dict[str, Any]] | None = None,
) -> str:
    """Multi-stage context assembly pipeline. Retrieves relevant entries from
    the session and project/staging tiers with token budget management.
    Applies semantic scoring to select the most relevant project-tier entries.
    Optionally pass conversation_history to improve tool-diversity filtering.

    Call this when: starting a new task. First action in every session."""
    pd = _resolve_project_store(project_dir)
    result = assemble_context(
        task_description=task_description,
        project_dir=pd,
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
@captured(source="compchem-memory")
def memory_record_session(
    event_type: str,
    data: dict[str, Any],
    project_dir: str | None = None,
) -> str:
    """Append a structured entry to the current session log (JSONL).
    data should contain: tool_name, args, result_summary, error (if any).

    Call this when: logging an observation the auto-capture decorator cannot infer (e.g., user-stated intent, a design decision, a hypothesis)."""
    guard = check_project(project_dir, pinned_dir=PROJECT_DIR, is_write=True)
    if guard.kind == "cross_write":
        return _project_switch_blocked_payload(guard)
    pd = _resolve_project_store(project_dir)
    sess_m = _get_session_mgr(pd)
    return sess_m.record(event_type, data, pd)


@mcp.tool()
@captured(source="compchem-memory")
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
    area; entries become active after confirmation or N consistent observations.

    Call this when: you have discovered something worth keeping — error_resolution, success_pattern, failure_pattern, or parameter_guidance. See AGENTS.md for content structure."""
    guard = check_project(project_dir, pinned_dir=PROJECT_DIR, is_write=True)
    if guard.kind == "cross_write":
        return _project_switch_blocked_payload(guard)
    pd = _resolve_project_store(project_dir)
    proj_m = _get_project_mgr()

    # Resolve session_id for cross-session tracking
    sess_m = _get_session_mgr(pd)
    session_id = sess_m._current_session_id  # may be None on first call

    # Check for similar existing staging entry to bump instead of duplicate
    similar = proj_m.find_similar_staging(pd, title, tags or [], entry_type=entry_type)
    if similar:
        proj_m.bump_observation_count(pd, similar, session_id=session_id, content=content)
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

    # Stamp the new entry with the current session_id
    if session_id:
        proj_m._update_entry_frontmatter(
            Path(result), "observed_in_sessions", [session_id]
        )

    promoted = proj_m.auto_promote_staging(pd)
    return json.dumps({
        "status": "created",
        "path": result,
        "promoted": promoted,
    })


@mcp.tool()
@captured(source="compchem-memory")
def memory_search(
    keyword: str = "",
    tags: list[str] | None = None,
    project_dir: str | None = None,
) -> str:
    """Keyword + tag search across all tiers (project, staging, session).
    Returns matches with a tier label; staging hits are marked provisional=True
    (unconfirmed) and ranked below the durable project tier.

    Call this when: you suspect a relevant entry exists but did not surface in memory_get_context."""
    pd = _resolve_project_store(project_dir)
    results = []

    proj_m = _get_project_mgr()
    entries = proj_m.search_entries(pd, keyword=keyword, tags=tags)
    results.extend(entries)

    # Staging: unconfirmed entries, tagged provisional and ranked below the durable
    # tiers (appended after them). Without this, useful un-promoted lessons are
    # invisible to search ("flag, don't hide").
    results.extend(proj_m.search_staging(pd, keyword=keyword, tags=tags))

    sess_m = _get_session_mgr(pd)
    # Drop self-referential noise: tool_call events are invocation logs (e.g. this
    # very memory_search call, whose args contain the keyword) — not memory content.
    # Filter BEFORE capping so real hits aren't crowded out by the log entries.
    session_matches = [
        sm for sm in sess_m.search(keyword)
        if sm.get("event_type") != "tool_call"
    ]
    for sm in session_matches[:5]:
        sm["tier"] = "session"
        sm["confidence"] = 0.5
        results.append(sm)

    return json.dumps(results, indent=2)


@mcp.tool()
@captured(source="compchem-memory")
def memory_get_run_history(
    project_dir: str | None = None,
) -> str:
    """Return run history for the current project (list of YAML records with
    status, scores, dates).

    Call this when: assessing past run outcomes for the current project."""
    pd = _resolve_project_store(project_dir)
    proj_m = _get_project_mgr()
    return json.dumps(proj_m.get_run_history(pd), indent=2)


@mcp.tool()
@captured(source="compchem-memory")
def memory_record_run(
    run_id: str,
    tool: str,
    status: str,
    metrics: dict[str, Any] | None = None,
    errors_solved: list[str] | None = None,
    project_dir: str | None = None,
) -> str:
    """Append a new run record to the project's run history index.

    Call this when: a scientific run has completed and you want to record its metadata + status alongside the run history (magnolia-run already does this for recognized tools)."""
    guard = check_project(project_dir, pinned_dir=PROJECT_DIR, is_write=True)
    if guard.kind == "cross_write":
        return _project_switch_blocked_payload(guard)
    pd = _resolve_project_store(project_dir)
    proj_m = _get_project_mgr()
    return proj_m.record_run(pd, run_id, tool, status, metrics=metrics, errors_solved=errors_solved)


@mcp.tool()
@captured(source="compchem-memory")
def memory_consolidate(
    tier: str = "project",
    project_dir: str | None = None,
    stale_days: int = 90,
    max_entries: int = 200,
) -> str:
    """Merge duplicates, expire stale entries, trim to budget within one tier.
    Can be called on-demand or scheduled.

    Call this when: running consolidation/deduplication on the project tier (normally periodic, not per-task)."""
    pd = _resolve_project_store(project_dir)
    result = consolidate_tier(
        tier,
        pd,
        stale_days=stale_days,
        max_entries=max_entries,
    )
    return json.dumps(result, indent=2)


@mcp.tool()
@captured(source="compchem-memory")
def memory_verify_claim(
    claim: str,
    project_dir: str | None = None,
    run_id: str | None = None,
    evidence: dict[str, Any] | None = None,
) -> str:
    """Verify the labeled numbers in a claim against run-record evidence.

    Deterministic: extracts label=number assertions from the claim and checks
    them against the run record's metrics (via run_id) plus any explicit
    evidence dict. Fail-closed: an unmatched label is 'unverified', a
    disagreement is 'contradicted' — never a silent pass, and the verdict
    names the offending numbers.

    Call this when: grounding a quantitative claim before writing it to
    memory, or checking an entry/receipt against the run that produced it."""
    guard = check_project(project_dir, pinned_dir=PROJECT_DIR, is_write=False)
    if guard.kind == "cross_write":
        return _project_switch_blocked_payload(guard)
    from compchem_memory.verifier import verify_claim

    merged: dict[str, Any] = dict(evidence or {})
    if run_id:
        pd = _resolve_project_store(project_dir)
        rec = _get_project_mgr().get_run(pd, run_id)
        if rec is not None:
            merged.setdefault("run_record", rec)
        else:
            merged.setdefault("run_record_missing", run_id)
    return json.dumps(verify_claim(claim, merged), indent=2)


@mcp.tool()
@captured(source="compchem-memory")
def post_run_assess(
    run_dir: str,
    tool: str,
    exit_code: int = 0,
    project_dir: str | None = None,
    run_id: str | None = None,
) -> str:
    """After a computation completes: check exit code, verify output files exist,
    extract metrics, flag quality issues. Records run in memory automatically.

    run_id: optional canonical id from the submit_job result; when omitted, an
    existing record for run_dir is reused, else basename(run_dir).

    Call this when: a run completed but magnolia-run did not assess it (e.g., manual invocation outside the wrapper)."""
    guard = check_project(project_dir, pinned_dir=PROJECT_DIR, is_write=True)
    if guard.kind == "cross_write":
        return _project_switch_blocked_payload(guard)
    from compchem_memory.learning.orchestrator import assess_and_record
    pd = _resolve_project_store(project_dir)
    proj_m = _get_project_mgr()
    assessment = assess_and_record(
        run_dir=run_dir,
        tool=tool,
        exit_code=exit_code,
        project_dir=pd,
        project_mgr=proj_m,
        run_id=run_id,
    )
    sess_m = _get_session_mgr(pd)
    sess_m.record(
        "post_run_assess",
        {"run_dir": run_dir, "tool": tool, "assessment": assessment},
        pd,
    )
    # Negative confidence feedback: if run failed, decrement confidence
    # on success_pattern entries for this tool.
    if assessment.get("overall") in ("fail", "failed"):
        count = proj_m.decrement_confidence_for_tool(pd, tool)
        if count > 0:
            sess_m.record(
                "confidence_decrement",
                {"tool": tool, "run_dir": run_dir, "entries_adjusted": count},
                pd,
            )
    return json.dumps(assessment, indent=2)


@mcp.tool()
@captured(source="compchem-memory")
def memory_confirm(
    entry_name: str,
    project_dir: str | None = None,
) -> str:
    """Confirm a staging entry, moving it to the active project entries.

    Call this when: reviewing staging entries you want to promote to the durable project tier (staging → project; for project → rules elevation use memory_review_promotions/memory_apply_promotions)."""
    pd = _resolve_project_store(project_dir)
    proj_m = _get_project_mgr()
    return proj_m.confirm_staging(pd, entry_name)


# ── v2 New Tools ─────────────────────────────────────────────────────────────


@mcp.tool()
@captured(source="compchem-memory")
def memory_select_relevant(
    task_description: str,
    project_dir: str | None = None,
    max_selections: int = 5,
    token_budget: int = 12000,
) -> str:
    """Semantic memory selection: scores and selects the most relevant project-tier
    entries for a given task. Uses heuristic scoring based on title, description,
    tags, tools, entry type, and confidence.

    Call this when: filtering a candidate list of entries down to the most relevant for a task."""
    pd = _resolve_project_store(project_dir)
    store = str(_memory_store(pd))
    entries = select_relevant_entries(
        task_description,
        store,
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
@captured(source="compchem-memory")
def memory_extract_from_session(
    project_dir: str | None = None,
) -> str:
    """Automatic memory extraction: distills session logs into typed staging entries
    (error_resolution, success_pattern, parameter_guidance). Runs when thresholds
    are met (5K tokens or 3 tool calls since last extraction).

    Call this when: forcing an extraction pass on the current session log (normally automatic via inline trigger)."""
    pd = _resolve_project_store(project_dir)
    sess_m = _get_session_mgr(pd)
    log_path = sess_m.get_session_log_path()
    if not log_path:
        return json.dumps({"status": "no_active_session", "extracted": 0})

    extractor = _get_extractor(pd)
    session_path = Path(log_path)

    if not extractor.should_extract(session_path):
        return json.dumps({"status": "threshold_not_met", "extracted": 0})

    saved = extractor.commit(session_path, pd)
    return json.dumps(
        {
            "status": "extracted",
            "extracted": len(saved),
            "paths": saved,
        },
        indent=2,
    )


@mcp.tool()
@captured(source="compchem-memory")
def memory_compact_session(
    project_dir: str | None = None,
    model_context_window: int = 128000,
    max_notes_tokens: int = 6000,
) -> str:
    """Compact session by pruning old tool results and generating summary notes.
    Three-tier strategy: micro-compact, session-memory compact, auto-compact.
    Returns compaction notes if pruning occurred.

    Call this when: the session JSONL has grown large and you want to compact older events into summary notes."""
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
@captured(source="compchem-memory")
def memory_search_errors(
    error_message: str,
    tool: str | None = None,
    project_dir: str | None = None,
    max_results: int = 5,
) -> str:
    """Search memory for similar past errors and their resolutions.
    Automatically invoked when a tool fails to find relevant fix history.

    Call this when: troubleshooting; given an error message, finds similar past resolutions."""
    pd = _resolve_project_store(project_dir)
    proj_m = _get_project_mgr()
    entries = proj_m.search_entries(pd, keyword=error_message[:50], tags=["error-resolution"])
    # Also find entries by type error_resolution/failure_pattern (not just by tag)
    type_entries = proj_m.search_entries(pd, keyword=error_message[:50])
    type_entries = [
        e for e in type_entries
        if e.get("type") in ("error_resolution", "failure_pattern")
    ]
    # Merge, dedup by name
    seen_names = {e["name"] for e in entries}
    for e in type_entries:
        if e["name"] not in seen_names:
            entries.append(e)
            seen_names.add(e["name"])
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
    commit: bool = False,
    project_dir: str | None = None,
) -> str:
    """Distill the current session into project-tier learnings.

    commit=False (default): PREVIEW — compute candidate learnings and return them,
    save nothing. Use this to inspect what the system would learn.

    commit=True: COMMIT — compute and save the learnings to staging now, un-gated
    (skips the should_extract threshold check). Use this to force consolidation
    of the current session immediately.

    Call this when: you want to see what the session would distill into
    (commit=False), or force-distill it now (commit=True)."""
    pd = _resolve_project_store(project_dir)
    from compchem_memory.reflections import pick_quote

    # Primary: distil the active session's real dialogue transcript. Same single
    # path the timer/startup sweep uses — the manual tool just forces a sweep of
    # the current session now.
    store = Path(pd) / ".magnolia"
    sid = _latest_sid(store / "opencode-sessions.jsonl")
    if sid and is_llm_available() and _opencode_available():
        res = distill_session_transcript(str(store), sid, commit=commit)
        if res is not None:
            if res["status"] == "committed":
                _safe_version_commit(store, f"manual distill: {len(res['saved'])} dialogue")
                return json.dumps(
                    {"status": "committed", "saved_count": len(res["saved"]),
                     "paths": res["saved"], "source": "dialogue",
                     "reflection": pick_quote("closing")},
                    indent=2,
                )
            return json.dumps(
                {"status": "preview", "candidate_count": len(res["candidates"]),
                 "candidates": res["candidates"], "source": "dialogue",
                 "reflection": pick_quote("opening")},
                indent=2,
            )

    # Fallback: tool-event log (no dialogue available — no LLM, no opencode, or
    # no captured session).
    sess_m = _get_session_mgr(pd)
    log_path = sess_m.get_session_log_path()
    if not log_path:
        return json.dumps({"status": "no_active_session"})

    extractor = _get_extractor(pd)
    session_path = Path(log_path)

    if commit:
        saved = extractor.commit(session_path, pd)
        _safe_version_commit(Path(pd) / ".magnolia", f"manual distill: {len(saved)} tool-event")
        return json.dumps(
            {
                "status": "committed",
                "saved_count": len(saved),
                "paths": saved,
                "source": "tool_event",
                "reflection": pick_quote("closing"),
            },
            indent=2,
        )

    candidates = extractor.preview(session_path)
    return json.dumps(
        {
            "status": "preview",
            "candidate_count": len(candidates),
            "candidates": candidates,
            "source": "tool_event",
            "reflection": pick_quote("opening"),
        },
        indent=2,
    )


@mcp.tool()
def memory_review_consolidation(project_dir: str | None = None) -> str:
    """Render pending finding-consolidation proposals to a visible, editable review
    file at <project>/magnolia-review/proposals.md and report a summary.

    Call this at session start when a consolidation proposal exists, or when the
    user asks to review proposed merges."""
    from compchem_memory.consolidation import render_review_markdown, _pending_indices
    pd = _resolve_project_store(project_dir)
    store = Path(pd) / ".magnolia"
    review_file = render_review_markdown(str(store))
    if not review_file:
        return json.dumps({"status": "no_pending_proposals", "pending": 0})
    data = json.loads((store / "reflex" / "consolidation-proposal.json").read_text())
    proposals = data.get("proposals", [])
    pending = [{"index": i, "confidence": proposals[i].get("confidence"),
                "title": proposals[i].get("merged_preview", {}).get("title", "")}
               for i in _pending_indices(data)]
    return json.dumps({"status": "review_ready", "pending": len(pending),
                       "review_file": review_file, "proposals": pending}, indent=2)


@mcp.tool()
def memory_apply_consolidation(
    accept: list[int], reject: list[int] | None = None, project_dir: str | None = None
) -> str:
    """Apply the accepted consolidation proposals (by index): merge each cluster's
    duplicate findings into one entry, then commit to the versioning repo
    (reversible via git). `reject` durably dismisses proposals so they stop
    re-surfacing. The magnolia-review/ dir is removed once every proposal has been
    handled (accepted or rejected).

    Call this after the user confirms which proposals to accept/reject. Pass the
    accepted indices in `accept` and the explicitly-rejected ones in `reject`."""
    from compchem_memory import consolidation
    pd = _resolve_project_store(project_dir)
    store = Path(pd) / ".magnolia"
    result = consolidation.apply_proposals(str(store), accept, reject=reject)
    if result["applied"]:
        _safe_version_commit(store, f"consolidate: {result['applied']} merge(s)")
    artifact = store / "reflex" / "consolidation-proposal.json"
    data = json.loads(artifact.read_text()) if artifact.exists() else {"proposals": [], "applied": [], "rejected": []}
    handled = set(data.get("applied", [])) | set(data.get("rejected", []))
    if len(handled) >= len(data.get("proposals", [])):
        _drop_review_file(pd, "proposals.md")
    return json.dumps({"status": "applied", **result}, indent=2)


@mcp.tool()
def memory_review_promotions(project_dir: str | None = None) -> str:
    """Render pending project→rules rule-elevation proposals to a visible,
    read-only review file at <project>/magnolia-review/promotions.md and report a
    summary. Call this at session start when a promotion proposal exists, or when
    the user asks to review proposed rule elevations."""
    from compchem_memory.promotion import render_promotions_markdown
    from compchem_memory.reflex_common import pending_indices
    pd = _resolve_project_store(project_dir)
    store = Path(pd) / ".magnolia"
    review_file = render_promotions_markdown(str(store))
    if not review_file:
        return json.dumps({"status": "no_pending_proposals", "pending": 0})
    data = json.loads((store / "reflex" / "promotion-proposal.json").read_text())
    proposals = data.get("proposals", [])
    pending = [{"index": i, "entry_title": proposals[i].get("entry_title", ""),
                "distinct_sessions": proposals[i].get("distinct_sessions"),
                "consistency": proposals[i].get("consistency", {}).get("status")}
               for i in pending_indices(data)]
    return json.dumps({"status": "review_ready", "pending": len(pending),
                       "review_file": review_file, "proposals": pending}, indent=2)


@mcp.tool()
def memory_apply_promotions(
    accept: list[int], reject: list[int] | None = None,
    promote_raw: list[int] | None = None, project_dir: str | None = None,
) -> str:
    """Apply confirmed rule elevations (by index): `accept` writes each drafted
    rule to its proposal's destination (shared `rules/` by default; a lesson
    containing cluster-specific facts goes to "your cluster file" — nothing is
    written; copy the draft into your `hpc-<cluster>` skill yourself) and
    archives the source project entry; `promote_raw` elevates the entry
    verbatim instead of the draft (refused for cluster-file proposals);
    `reject` durably dismisses a proposal. Commits to the versioning repo
    (reversible via git). Removes magnolia-review/promotions.md once every
    proposal is handled. Edit the resulting rule file afterward if needed."""
    from compchem_memory import promotion
    pd = _resolve_project_store(project_dir)
    store = Path(pd) / ".magnolia"
    result = promotion.apply_promotions(
        str(store), str(RULES_DIR), accept=accept, reject=reject,
        promote_raw=promote_raw)
    if result["applied"] or result["promoted_raw"]:
        n = result["applied"] + result["promoted_raw"]
        _safe_version_commit(store, f"promote: {n} rule(s)")
    artifact = store / "reflex" / "promotion-proposal.json"
    data = json.loads(artifact.read_text()) if artifact.exists() else {"proposals": [], "applied": [], "rejected": []}
    handled = set(data.get("applied", [])) | set(data.get("rejected", []))
    if len(handled) >= len(data.get("proposals", [])):
        _drop_review_file(pd, "promotions.md")
    return json.dumps({"status": "applied", **result}, indent=2)


@mcp.tool()
@captured(source="compchem-memory")
def memory_scan_headers(
    project_dir: str | None = None,
    tier: str = "project",
) -> str:
    """Fast header scan of memory entries (frontmatter only, no full content).
    Returns catalogue of titles, types, tags, tools for selection. `tier` is one
    of 'project' (default), 'staging', or 'all' (project+staging).

    Call this when: enumerating entry headers without loading bodies."""
    pd = _resolve_project_store(project_dir)

    if tier == "project":
        headers = scan_memory_headers(Path(pd) / ".magnolia" / "entries")
    elif tier == "staging":
        headers = scan_memory_headers(Path(pd) / ".magnolia" / "staging")
    elif tier == "all":
        headers = (scan_memory_headers(Path(pd) / ".magnolia" / "entries")
                   + scan_memory_headers(Path(pd) / ".magnolia" / "staging"))
    else:
        return json.dumps(
            {"error": f"Unknown tier: {tier}. Use 'project', 'staging', or 'all'."}
        )

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
@captured(source="compchem-memory")
def memory_health_check(
    project_dir: str | None = None,
    stale_days: int = 90,
    min_confidence: float = 0.3,
    fix: bool = False,
) -> str:
    """Audit the knowledge base for staleness, contradictions, gaps, and orphaned entries.
    Returns a structured report. Default mode is dry-run (no side effects).
    Set fix=True to auto-resolve safe issues (remove broken refs, mark stale entries).

    Call this when: running diagnostics for stale entries, low-confidence entries, orphans, duplicates, etc."""
    pd = _resolve_project_store(project_dir)
    result = run_health_check(
        project_dir=pd,
        stale_days=stale_days,
        min_confidence=min_confidence,
        fix=fix,
    )
    return json.dumps(result, indent=2)


@mcp.tool()
@captured(source="compchem-memory")
def memory_notebook(
    start_date: str | None = None,
    end_date: str | None = None,
    section: str | None = None,
    project_dir: str | None = None,
) -> str:
    """Generate a chronological lab notebook timeline from sessions, runs, and entries.
    Returns markdown. Optionally filter by date range or section (entries, runs, sessions).
    This is a read-only view tool — it does not modify any data.

    Call this when: generating a chronological timeline of sessions, runs, and entries for the current project."""
    pd = _resolve_project_store(project_dir)
    return generate_notebook(
        project_dir=pd,
        start_date=start_date,
        end_date=end_date,
        section=section,
    )


@mcp.tool()
@captured(source="compchem-memory")
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
    Entries are created directly in the active entries area (not staging).

    Call this when: creating a human-authored lab notebook note entry directly in the active project tier."""
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


@mcp.tool()
@captured(source="compchem-memory")
def memory_set_goal(
    goal: str,
    project_dir: str | None = None,
) -> str:
    """Set or update the project goal. This is the persistent reference signal that
    guides all memory retrieval and context assembly. Should describe what the project
    is trying to achieve, key constraints, and success criteria.

    Call this when: defining the project's reference signal — the persistent intent against which decisions are validated."""
    pd = _resolve_project_store(project_dir)
    proj_m = _get_project_mgr()
    path = proj_m.set_goal(pd, goal)
    return json.dumps({"status": "set", "path": path})


@mcp.tool()
@captured(source="compchem-memory")
def memory_get_goal(
    project_dir: str | None = None,
) -> str:
    """Retrieve the current project goal. Returns the GOAL.md content or a message
    if no goal has been set.

    Call this when: retrieving the project's reference signal."""
    pd = _resolve_project_store(project_dir)
    proj_m = _get_project_mgr()
    content = proj_m.get_goal(pd)
    if content:
        return content
    return "No project goal set. Use memory_set_goal to define one."


# ── Resources (preserved from v1) ────────────────────────────────────────────


@mcp.resource("memory://project/index")
def get_project_index() -> str:
    """Project-tier entry catalogue for current project.

    Call this when: listing all project-tier entries to browse available memory."""
    pd = _resolve_project_store()
    proj_m = _get_project_mgr()
    return json.dumps(proj_m.list_entries(pd), indent=2)


@mcp.resource("memory://project/entry/{name}")
def get_project_entry(name: str) -> str:
    """A single project-tier entry by name.

    Call this when: reading the full body of a specific project-tier entry."""
    pd = _resolve_project_store()
    proj_m = _get_project_mgr()
    content = proj_m.get_entry(pd, name)
    return content or f"Entry not found: {name}"


@mcp.resource("memory://runs/index")
def get_runs_index() -> str:
    """Run history index for current project.

    Call this when: browsing the full run history index for the current project."""
    pd = _resolve_project_store()
    proj_m = _get_project_mgr()
    return json.dumps(proj_m.get_run_history(pd), indent=2)


if __name__ == "__main__":
    mcp.run()
