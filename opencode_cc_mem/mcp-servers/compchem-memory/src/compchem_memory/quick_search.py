"""Fast keyword search CLI for the action-time retrieval plugin.

`python -m compchem_memory.quick_search "<query>" [--project-dir DIR] [--k N]`

Deliberately imports ONLY the tier managers (stdlib + yaml, ~50 ms cold) —
not `compchem_memory.server`, whose MCP SDK import chain costs >1 s, which
would be paid on every instrumented tool call. Prints top-k matches as one
JSON object per line: title, type, tier, provisional, confidence, path, gist.

Exit codes: 0 on success (including "no hits" -> empty output), 2 on usage
error, 1 on unexpected failure. Any single tier failing is demoted to a
stderr warning, not a crash — the plugin treats partial results as fine.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path

from compchem_memory.tiers.project import ProjectManager
from compchem_memory.storage import resolve_project_dir


def _gist(text: str, limit: int = 180) -> str:
    """One-line gist: skip frontmatter, collapse whitespace."""
    if text.startswith("---"):
        end = text.find("---", 3)
        text = text[end + 3:] if end != -1 else text
    line = " ".join(text.split())
    return line[:limit]


def main() -> int:
    ap = argparse.ArgumentParser(prog="quick_search")
    ap.add_argument("query")
    ap.add_argument("--project-dir", default=None)
    ap.add_argument("--k", type=int, default=5)
    args = ap.parse_args()

    query = args.query.strip()
    project_dir = resolve_project_dir(args.project_dir, os.environ.get("MAGNOLIA_PROJECT_DIR", "."))
    proj_m = ProjectManager(Path.home() / ".magnolia")

    results: list[dict] = []

    # Project tier: scored search (title/body token overlap).
    try:
        results.extend(proj_m.search_entries(project_dir, keyword=query))
    except Exception as e:  # noqa: BLE001 - partial results are fine
        print(f"[quick_search] project tier skipped: {e}", file=sys.stderr)

    # Staging tier: same scoring, flagged provisional.
    try:
        for e in proj_m.search_staging(project_dir, keyword=query):
            e.setdefault("provisional", True)
            results.append(e)
    except Exception as e:  # noqa: BLE001
        print(f"[quick_search] staging tier skipped: {e}", file=sys.stderr)

    # (The skill tier was retired 2026-09; rules/ doctrine is git-tracked and
    # loaded via AGENTS.md, not searched here.)

    tier_weight = {"project": 2, "staging": 1}
    results.sort(key=lambda e: (e.get("score", 0), tier_weight.get(e.get("tier", ""), 0)),
                 reverse=True)

    for e in results[: max(args.k, 0)]:
        out = {
            "title": e.get("title") or e.get("name", ""),
            "type": e.get("type", ""),
            "tier": e.get("tier", ""),
            "score": e.get("score", 0),
            "provisional": bool(e.get("provisional", False)),
            "confidence": e.get("confidence", 0.5),
            "path": e.get("path", ""),
            "gist": e.get("description") or "",
        }
        if not out["gist"] and out["path"]:
            try:
                out["gist"] = _gist(Path(out["path"]).read_text(encoding="utf-8", errors="replace"))
            except OSError:
                pass
        print(json.dumps(out, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except SystemExit:
        raise
    except Exception as e:  # noqa: BLE001 - the plugin treats any failure as "no hits"
        print(f"[quick_search] fatal: {e}", file=sys.stderr)
        sys.exit(1)
