"""A2 historical backfill (2026-10-06): staging/project entries created before
cross-session tracking lack `observed_in_sessions`, so observation counting and
R7 auto-confirm eligibility cannot see them. For each entry that has an
`opencode_session_id` but an empty/missing `observed_in_sessions`, set
`observed_in_sessions = [opencode_session_id]` and raise `observation_count` to
at least 1. DRY-RUN BY DEFAULT: this mutates the store, so the real run is a
phase-gate decision — the flag-gated sweep never calls this automatically.
"""

import json
from datetime import datetime, timezone
from pathlib import Path

import yaml


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _rewrite_frontmatter(f: Path, meta: dict, body: str) -> None:
    f.write_text("---\n" + yaml.dump(meta, default_flow_style=False,
                                     allow_unicode=True)
                 + "---\n\n" + body + "\n", encoding="utf-8")


def backfill_observed_sessions(store_dir: str, dry_run: bool = True) -> dict:
    """Fill missing `observed_in_sessions` from `opencode_session_id` across
    staging/ and entries/. Returns {"scanned": int, "changed": [names],
    "dry_run": bool}. `changed` holds what changed (dry_run=False) or WOULD
    change (dry_run=True)."""
    store = Path(store_dir)
    scanned = 0
    changed: list[str] = []
    for sub in ("staging", "entries"):
        d = store / sub
        if not d.exists():
            continue
        for f in sorted(d.glob("*.md")):
            if f.name == "INDEX.md":
                continue
            scanned += 1
            try:
                text = f.read_text(encoding="utf-8", errors="replace")
            except OSError:
                continue
            parts = text.split("---", 2)
            if len(parts) != 3:
                continue
            try:
                meta = yaml.safe_load(parts[1]) or {}
            except yaml.YAMLError:
                continue
            sid = meta.get("opencode_session_id")
            sessions = meta.get("observed_in_sessions") or []
            if not sid or sessions:
                continue  # nothing to backfill
            changed.append(f"{sub}/{f.name}")
            if dry_run:
                continue
            meta["observed_in_sessions"] = [str(sid)]
            obs = meta.get("observation_count", 0) or 0
            if obs < 1:
                meta["observation_count"] = 1
            meta.setdefault("backfilled", _now()[:10])
            _rewrite_frontmatter(f, meta, parts[2])
    return {"scanned": scanned, "changed": changed,
            "dry_run": dry_run, "count": len(changed)}
