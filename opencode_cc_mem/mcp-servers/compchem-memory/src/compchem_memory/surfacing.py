"""R5 surfacing counter: append-only log of every entry that reaches an agent.

Plan: distill-admission R5 (projects/xiulian/docs/distill-admission-plan-2026-09-30.md).

Channels:
- get_context   — memory_get_context sources (project/staging entries served)
- boot_context  — boot-context.md includes (regenerated at each boot)
- action_memory — action-retrieval plugin injections (written by the plugin
  itself: only it knows the final injected set and the opencode sessionID)

Attribution is by SERVING STORE (the resolved .magnolia dir that actually
served the entries), not by the calling project — a symlinked store
(ad_verum serving gpx4 reads) is credited to the store that served.

Row shape (one JSON line per surfaced ENTRY; goal/session/run context
sections are not entries and are not logged):
  {ts, channel, store, session_id, tier, id, counts_for_exposure, task_len}

counts_for_exposure applies the R6 substance test AT LOG TIME so retirement
never needs to re-read session transcripts:
- get_context:   True iff len(task_description) >= 40 chars
- action_memory: True (a real tool call just ran in the session)
- boot_context:  False — boot regen never counts (anti-manipulation guard 1)

Failure model: logging must NEVER break the serving tool — every error is
swallowed (a lost row only costs us telemetry, never a memory operation).
"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path

ENTRY_TIERS = ("project", "staging")
CHANNELS = ("get_context", "boot_context", "action_memory")

# R6 substance test, task_description disjunct: minimum length that counts.
MIN_SUBSTANTIVE_TASK_LEN = 40


def surfacing_path(store: Path | str) -> Path:
    return Path(store) / "surfacing.jsonl"


def guess_session_id(store: Path | str) -> str | None:
    """Best-effort opencode session id of the LIVE caller.

    The capture plugin appends the current session last to the store's
    opencode-sessions.jsonl mapping, so the final line names the caller
    (same contract as opencode_ingest._latest_sid).
    """
    mapping = Path(store) / "opencode-sessions.jsonl"
    if not mapping.exists():
        return None
    latest: str | None = None
    for line in mapping.read_text(encoding="utf-8", errors="replace").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            sid = json.loads(line).get("opencode_session_id")
        except json.JSONDecodeError:
            continue
        if sid:
            latest = sid
    return latest


def counts_for_exposure(channel: str, task_description: str | None = None) -> bool:
    if channel == "get_context":
        return bool(task_description) and len(task_description) >= MIN_SUBSTANTIVE_TASK_LEN
    if channel == "action_memory":
        return True
    return False  # boot_context (and anything unknown) never counts


def log_surfacing(
    store: Path | str,
    sources: list[dict],
    channel: str,
    session_id: str | None = None,
    task_description: str | None = None,
) -> int:
    """Append one surfacing row per project/staging entry in ``sources``.

    ``store`` must be the RESOLVED serving store (callers resolve symlinks
    via context_assembly._memory_store before logging). Returns the number
    of rows written; never raises.
    """
    try:
        store = Path(store)
        rows = []
        now = datetime.now(timezone.utc).isoformat()
        qualifies = counts_for_exposure(channel, task_description)
        for src in sources or []:
            tier = src.get("tier")
            if tier not in ENTRY_TIERS:
                continue
            rows.append(json.dumps({
                "ts": now,
                "channel": channel,
                "store": str(store),
                "session_id": session_id,
                "tier": tier,
                "id": src.get("id", ""),
                "counts_for_exposure": qualifies,
                "task_len": len(task_description) if task_description else 0,
            }))
        if not rows:
            return 0
        with open(surfacing_path(store), "a", encoding="utf-8") as f:
            f.write("\n".join(rows) + "\n")
        return len(rows)
    except Exception:  # noqa: BLE001 - telemetry must never break the caller
        return 0
