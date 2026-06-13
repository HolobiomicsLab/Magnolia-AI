"""Single staging writer shared by every distillation path.

Both the dialogue path (opencode transcript) and the tool-event fallback write
candidate learnings to ``<store>/staging`` through this one function, so the
on-disk shape of a staging entry has exactly one definition.
"""

import re
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import yaml


def save_candidate(
    store: Path,
    candidate: dict[str, Any],
    *,
    source: str,
    opencode_session_id: str | None = None,
) -> str:
    """Write one distilled candidate to ``<store>/staging`` and return its path.

    ``source`` records which pipeline produced it (``opencode_distill`` for the
    dialogue path, ``auto_extraction`` for the tool-event fallback).
    ``opencode_session_id`` stamps provenance back to the conversation; it is
    omitted from the frontmatter entirely when not supplied.
    """
    staging = store / "staging"
    staging.mkdir(parents=True, exist_ok=True)

    title = candidate.get("title", "untitled")
    slug = re.sub(r"[^a-zA-Z0-9]+", "_", title)[:60].strip("_")
    ts = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S_%f")
    now = datetime.now(timezone.utc).isoformat()
    fpath = staging / f"{ts}_{slug}.md"

    fm: dict[str, Any] = {
        "id": ts,
        "type": candidate.get("type", "note"),
        "title": title,
        "description": candidate.get("content", "")[:200],
        "tools": candidate.get("tools", []),
        "tags": candidate.get("tags", []),
        "created": now,
        "updated": now,
        "source": source,
        "observation_count": 1,
        "confidence": candidate.get("confidence", 0.5),
    }
    if opencode_session_id is not None:
        fm["opencode_session_id"] = opencode_session_id
        fm["observed_in_sessions"] = [opencode_session_id]

    fpath.write_text(
        "---\n" + yaml.dump(fm, default_flow_style=False) + "---\n\n"
        + candidate.get("content", "") + "\n"
    )
    return str(fpath)
