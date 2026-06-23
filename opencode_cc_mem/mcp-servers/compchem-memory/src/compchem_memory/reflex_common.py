# src/compchem_memory/reflex_common.py
"""Operation-agnostic helpers shared by the self-reflex passes (consolidation,
promotion). Each pass keeps its own domain logic; only the generic artifact /
review plumbing lives here."""

import json
from pathlib import Path
from typing import Any, Callable

import yaml


def parse_frontmatter_file(path: str | Path) -> dict[str, Any] | None:
    """Load a markdown entry as {"id","path","meta","body"}, or None if missing."""
    p = Path(path)
    if not p.exists():
        return None
    text = p.read_text(encoding="utf-8", errors="replace")
    meta, body = {}, text
    if text.startswith("---"):
        parts = text.split("---", 2)
        if len(parts) == 3:
            try:
                meta = yaml.safe_load(parts[1]) or {}
            except yaml.YAMLError:
                meta = {}
            body = parts[2]
    return {"id": p.name, "path": str(p), "meta": meta, "body": body.strip()}


def pending_indices(data: dict[str, Any]) -> list[int]:
    """Proposal indices that are neither applied nor rejected."""
    handled = set(data.get("applied", [])) | set(data.get("rejected", []))
    return [i for i in range(len(data.get("proposals", []))) if i not in handled]


def prior_rejected_keys(
    artifact: Path, key_of: Callable[[dict[str, Any]], Any]
) -> set:
    """Content keys (via `key_of`) of proposals rejected in an existing artifact.
    Stable across regeneration, unlike positional index."""
    if not Path(artifact).exists():
        return set()
    try:
        prior = json.loads(Path(artifact).read_text())
    except (json.JSONDecodeError, OSError):
        return set()
    old = prior.get("proposals", [])
    keys = set()
    for idx in prior.get("rejected", []):
        if isinstance(idx, int) and 0 <= idx < len(old):
            keys.add(key_of(old[idx]))
    return keys


def fenced_preview(body: str, limit: int = 1500) -> list[str]:
    """Markdown lines for a truncated, tilde-fenced preview block. `~~~~` won't be
    closed early by a ``` block inside the body."""
    body = body or ""
    shown = body[:limit] + ("\n…(truncated)" if len(body) > limit else "")
    return ["~~~~", shown, "~~~~"]
