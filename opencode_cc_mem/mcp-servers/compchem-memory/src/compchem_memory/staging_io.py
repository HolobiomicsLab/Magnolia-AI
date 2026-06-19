"""Single staging writer shared by every distillation path.

Both the dialogue path (opencode transcript) and the tool-event fallback write
candidate learnings to ``<store>/staging`` through this one function, so the
on-disk shape of a staging entry has exactly one definition.
"""

import re
from datetime import datetime
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

    # Dedup: if a genuinely-similar staging entry already exists, bump it (append
    # this observation) instead of writing a near-duplicate. Re-distilling the
    # same finding across sessions must not multiply entries — the dedup that
    # memory_record_learning already does, applied to the distillation path too.
    # Best-effort: any failure falls through to writing a fresh entry.
    try:
        from compchem_memory.tiers.project import ProjectManager
        _pm = ProjectManager(global_base=Path.home() / ".magnolia")
        _project_dir = str(store.parent)
        _similar = _pm.find_similar_staging(
            _project_dir, title, candidate.get("tags", []) or [],
            entry_type=candidate.get("type", "note"),
        )
        if _similar:
            _pm.bump_observation_count(
                _project_dir, _similar,
                session_id=opencode_session_id,
                content=candidate.get("content", ""),
            )
            return str(staging / _similar)
    except Exception:
        pass  # dedup is best-effort; never block a distillation save

    slug = re.sub(r"[^a-zA-Z0-9]+", "_", title)[:60].strip("_")
    # System local time (timezone-aware), so filenames/timestamps match the
    # operator's wall clock. .astimezone() keeps it tz-aware (explicit offset),
    # so downstream date math (.date(), fromisoformat) stays correct.
    local_now = datetime.now().astimezone()
    ts = local_now.strftime("%Y%m%d_%H%M%S_%f")
    now = local_now.isoformat()
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
