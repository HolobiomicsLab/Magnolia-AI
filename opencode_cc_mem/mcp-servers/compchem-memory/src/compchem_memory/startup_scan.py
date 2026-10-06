"""Startup/timer distillation sweep.

One distillation path: the opencode dialogue transcript is the source of truth.
The tool-event heuristic extractor runs ONLY as a fallback, when the dialogue
path cannot — no `opencode` binary, no LLM, or no captured session mapping.
"""

import json
import os
import shutil
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from compchem_memory.extraction import AutomaticMemoryExtractor
from compchem_memory.llm import is_llm_available


_CONSOLIDATION_MIN_FINDINGS = 20  # gate: skip consolidation below this many findings


def _opencode_available() -> bool:
    return shutil.which("opencode") is not None


def scan_and_distill(project_dir: str) -> dict[str, Any]:
    """Distil the project's sessions into staging.

    Dialogue is primary: if a captured opencode mapping exists and both the
    `opencode` binary and an LLM are available, distil the real conversation
    transcripts (incrementally, cursor-based) and nothing else. Otherwise fall
    back to the tool-event heuristic extractor over `.magnolia/sessions/*.jsonl`.

    Returns a dict with a `mode` of "dialogue" or "tool_event".
    """
    pd = Path(project_dir)
    store = pd / ".magnolia"
    mapping = store / "opencode-sessions.jsonl"

    if mapping.exists() and is_llm_available() and _opencode_available():
        result = _distill_dialogue(store)
    else:
        result = _distill_tool_events(pd)
    _maybe_consolidate(store)
    _surface_pending_consolidation(project_dir, store)
    _maybe_promote(store)
    _maybe_auto_confirm(project_dir, store)
    _surface_pending_promotion(project_dir, store)
    _commit_after_sweep(store, result)
    return result


def _distill_dialogue(store: Path) -> dict[str, Any]:
    from compchem_memory.opencode_ingest import ingest_opencode_sessions

    ingested = len(ingest_opencode_sessions(str(store)))
    return {"mode": "dialogue", "opencode_ingested": ingested,
            "scanned": 0, "distilled": 0, "skipped": 0}


def _distill_tool_events(pd: Path) -> dict[str, Any]:
    """Fallback: distil the SessionManager tool-event logs.

    Closed sessions (stem != current-session-id) are committed then sealed with a
    .distilled marker. The active session is committed but NOT sealed — it is
    still being appended to, and the cursor in extraction-state.yaml tracks
    progress so a later scan picks up its new events.
    """
    sessions_dir = pd / ".magnolia" / "sessions"
    sessions_dir.mkdir(parents=True, exist_ok=True)

    active_id = ""
    current_file = pd / ".magnolia" / ".current-session-id"
    if current_file.exists():
        try:
            active_id = current_file.read_text().strip()
        except OSError:
            active_id = ""

    extractor = AutomaticMemoryExtractor(str(pd))
    scanned = distilled = skipped = 0

    for session_path in sorted(sessions_dir.glob("*.jsonl")):
        scanned += 1
        marker = session_path.with_suffix(".distilled")
        if marker.exists():
            skipped += 1
            continue
        is_active = session_path.stem == active_id
        try:
            extractor.commit(session_path, str(pd))
            if not is_active:
                marker.write_text(
                    json.dumps({"distilled_at": _now_iso(), "events_path": session_path.name})
                    + "\n"
                )
            distilled += 1
        except Exception as e:
            print(f"[startup_scan] failed on {session_path.name}: {e}")
            continue

    return {"mode": "tool_event", "scanned": scanned, "distilled": distilled,
            "skipped": skipped, "opencode_ingested": 0}


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _commit_after_sweep(store: Path, result: dict) -> None:
    """One boundary commit per sweep. Never lets a versioning failure break
    distillation."""
    from compchem_memory import versioning
    try:
        msg = (f"distill: {result.get('opencode_ingested', 0)} dialogue, "
               f"{result.get('distilled', 0)} tool-event")
        versioning.commit_all(store, msg)
    except Exception as e:  # noqa: BLE001 - versioning must never break the sweep
        print(f"[versioning] commit skipped: {e}")


def _has_pending_review(artifact: Path) -> bool:
    """True if the proposal artifact has proposals not yet applied or rejected.
    Freezes regeneration while a human review is outstanding so the review→apply
    contract (keyed by positional index) can't be invalidated by a concurrent
    sweep regenerating the artifact in a different order."""
    if not artifact.exists():
        return False
    try:
        data = json.loads(artifact.read_text())
    except (json.JSONDecodeError, OSError):
        return False
    from compchem_memory.reflex_common import pending_indices
    return bool(pending_indices(data))


def _maybe_promote(store: Path) -> None:
    """Gated, proposal-only project→rules promotion. Never breaks the sweep."""
    try:
        if not is_llm_available():
            return
        if _has_pending_review(store / "reflex" / "promotion-proposal.json"):
            return  # don't regenerate while a review is pending — keeps [i] stable
        from compchem_memory.promotion import propose_promotions, eligible_entries
        from compchem_memory.storage import resolved_rules_dir
        if not eligible_entries(str(store)):
            return
        propose_promotions(str(store), rules_dir=str(resolved_rules_dir()))
    except Exception as e:  # noqa: BLE001 - promotion must never break the sweep
        print(f"[promotion] skipped: {e}")


def _maybe_consolidate(store: Path) -> None:
    """Gated, proposal-only finding consolidation. Never breaks the sweep."""
    try:
        if not is_llm_available():
            return
        # Front-of-sweep pre-clean: with MAGNOLIA_CONSOLIDATION_AUTO set, clear
        # the safe band from an EXISTING queue before the pending-review guard —
        # otherwise high-confidence duplicates pile up behind an untouched human
        # review. Without the variable this call returns "disabled" immediately,
        # so the guard below still fully controls regeneration and [i] stability.
        from compchem_memory.consolidation import auto_apply_band
        auto_apply_band(str(store))
        if _has_pending_review(store / "reflex" / "consolidation-proposal.json"):
            return  # don't regenerate while a review is pending — keeps [i] stable
        from compchem_memory.consolidation import _load_findings, consolidate_project_findings
        if len(_load_findings(store / "staging")) < _CONSOLIDATION_MIN_FINDINGS:
            return
        consolidate_project_findings(str(store))
    except Exception as e:  # noqa: BLE001 - consolidation must never break the sweep
        print(f"[consolidation] skipped: {e}")


def _maybe_auto_confirm(project_dir: str, store: Path) -> None:
    """R7 auto-confirm sweep step. No-op unless MAGNOLIA_AUTO_CONFIRM is set
    (default off — build ships dormant, activation changes store composition).
    Promotes staging entries corroborated in >=3 distinct sessions; candidates
    whose title matches an existing project entry are HELD and flagged instead
    of promoting a possible contradiction (stop-and-flag conflict queue). The
    shortlist always rides the .distill-notices queue: an auto-confirm is a
    store rewrite the user never saw, so it must be visible. Never raises."""
    try:
        if str(os.environ.get("MAGNOLIA_AUTO_CONFIRM", "")).strip().lower() not in (
                "1", "true", "yes", "on"):
            return
        from compchem_memory.tiers.project import ProjectManager
        res = ProjectManager(Path.home() / ".magnolia").auto_confirm_staging(project_dir)
        n_promoted, n_conflicts = len(res["promoted"]), len(res["conflicts"])
        if not n_promoted and not n_conflicts:
            return
        from compchem_memory import distill_log
        parts = []
        if n_promoted:
            parts.append(f"Auto-confirmed {n_promoted} entry(ies) "
                         f"(observed in 3+ sessions): "
                         + "; ".join(res["promoted"][:8]))
        if n_conflicts:
            parts.append(f"HELD {n_conflicts} conflicting candidate(s) "
                         "(title matches an existing project entry — needs "
                         "your judgment): " + "; ".join(res["conflicts"][:8]))
        distill_log.push_distill_notice(
            project_dir,
            "Auto-confirm moved staging entries to the durable project tier; "
            "held conflicts need review via memory_confirm.",
            " | ".join(parts))
    except Exception as e:  # noqa: BLE001 - auto-confirm must never break the sweep
        print(f"[auto-confirm] skipped: {e}")


def _surface_pending_consolidation(project_dir: str, store: Path) -> None:
    """Push a notice when consolidation proposals are pending review.

    Without this the loop silently stalls: pending proposals freeze regeneration
    (see _has_pending_review) and nothing else surfaces them, so the
    review->apply contract never runs. The notice rides the existing
    .distill-notices queue, drained by the @captured decorator onto the next
    memory tool result. Runs every sweep (startup + timer) so it re-nudges until
    the proposals are handled. Never raises — surfacing must not break the sweep.
    """
    try:
        artifact = store / "reflex" / "consolidation-proposal.json"
        if not artifact.exists():
            return
        from compchem_memory.reflex_common import pending_indices
        data = json.loads(artifact.read_text())
        n = len(pending_indices(data))
        if not n:
            return
        from compchem_memory import distill_log
        # Plain-language wording: a non-expert user (and the agent relaying to
        # them) must understand it without knowing the term "consolidation".
        distill_log.push_distill_notice(
            project_dir,
            quote=(
                "Explain this to the user in plain words and ask if they want to "
                "review — nothing is applied without their approval. Run "
                "memory_review_consolidation to show the list."
            ),
            summary=(
                f"Memory cleanup ready: {n} set(s) of duplicate lessons can be "
                "merged into single clean entries (keeps recall accurate)."
            ),
        )
    except Exception as e:  # noqa: BLE001 - surfacing must never break the sweep
        print(f"[consolidation] surface skipped: {e}")


def _surface_pending_promotion(project_dir: str, store: Path) -> None:
    """Push a notice when project→skill rule-elevation proposals are pending review.

    Mirror of _surface_pending_consolidation for the promotion tier. Without this
    the promotion loop silently stalls: pending proposals freeze regeneration
    (see _maybe_promote / _has_pending_review) and nothing else surfaces them, so
    the review->apply contract never runs. The notice rides the existing
    .distill-notices queue, drained by the @captured decorator onto the next
    memory tool result. Runs every sweep so it re-nudges until the proposals are
    handled. Never raises — surfacing must not break the sweep.
    """
    try:
        artifact = store / "reflex" / "promotion-proposal.json"
        if not artifact.exists():
            return
        from compchem_memory.reflex_common import pending_indices
        data = json.loads(artifact.read_text())
        n = len(pending_indices(data))
        if not n:
            return
        from compchem_memory import distill_log
        # Plain-language wording: a non-expert user (and the agent relaying to
        # them) must understand it without knowing the term "promotion".
        distill_log.push_distill_notice(
            project_dir,
            quote=(
                "Explain this to the user in plain words and ask if they want to "
                "review — nothing is applied without their approval. Run "
                "memory_review_promotions to show the list."
            ),
            summary=(
                f"Rule elevation ready: {n} repeated lesson(s) qualify to become "
                "durable skill rule(s) (seen across enough sessions to trust)."
            ),
        )
    except Exception as e:  # noqa: BLE001 - surfacing must never break the sweep
        print(f"[promotion] surface skipped: {e}")
