"""Finding consolidation (self-reflex): detect same-claim findings with an LLM,
compute a deterministic merged preview. Increment A is proposal-only — it writes
proposals and mutates nothing.

Detect with intelligence (LLM clusters same-claim findings), mutate with
determinism (this module assembles merges from source text — it never authors
new knowledge), human-confirm (Increment B). Scoped to natural-language types;
deterministic tool-output keeps the existing lexical dedup.
"""

import json
from pathlib import Path
from typing import Any, Callable

import yaml

# Natural-language learning types that need semantic (not lexical) matching.
NL_TYPES = ("scientific_finding", "success_pattern", "parameter_guidance",
            "note", "workflow_note")


def _sessions_of(meta: dict[str, Any]) -> set[str]:
    s = set(meta.get("observed_in_sessions") or [])
    sid = meta.get("opencode_session_id")
    if sid:
        s.add(sid)
    return s


def merge_entries(entries: list[dict[str, Any]]) -> dict[str, Any]:
    """Deterministically merge a cluster of same-claim entries into a preview.

    Each entry is {"id","path","meta","body"}. The canonical is the longest body;
    the merged body is the canonical text plus the OTHER entries' verbatim text
    under a provenance section (source text only — nothing is invented).
    observation_count is the number of DISTINCT contributing sessions (so
    within-session repeats do not inflate it)."""
    if not entries:
        raise ValueError("merge_entries requires at least one entry")
    canonical = max(entries, key=lambda e: len(e["body"]))
    sessions: set[str] = set()
    tags: set[str] = set()
    tools: set[str] = set()
    confidence = 0.0
    for e in entries:
        m = e["meta"]
        sessions |= _sessions_of(m)
        tags |= set(m.get("tags") or [])
        tools |= set(m.get("tools") or [])
        try:
            confidence = max(confidence, float(m.get("confidence", 0.5)))
        except (TypeError, ValueError):
            pass

    merged_meta = dict(canonical["meta"])
    merged_meta["observed_in_sessions"] = sorted(sessions)
    merged_meta["observation_count"] = max(len(sessions), 1)
    merged_meta["tags"] = sorted(tags)
    merged_meta["tools"] = sorted(tools)
    merged_meta["confidence"] = confidence

    body = canonical["body"]
    others = [e for e in entries if e is not canonical]
    if others:
        prov = ["\n\n## Corroborating observations (merged)\n"]
        for e in others:
            sid = e["meta"].get("opencode_session_id", "?")
            prov.append(f"\n- (session {sid}) {e['meta'].get('title','')}\n\n{e['body']}\n")
        body = body + "".join(prov)

    return {
        "meta": merged_meta,
        "body": body,
        "sources": [e["path"] for e in entries],
        "canonical": canonical["path"],
    }


def _load_findings(staging_dir: Path, types: tuple[str, ...] = NL_TYPES) -> list[dict[str, Any]]:
    """Load natural-language finding entries from staging as
    {"id","path","meta","body"}; id is the filename (stable, unique)."""
    staging_dir = Path(staging_dir)
    out: list[dict[str, Any]] = []
    if not staging_dir.exists():
        return out
    for f in sorted(staging_dir.glob("*.md")):
        if f.name == "INDEX.md":
            continue
        text = f.read_text(errors="replace")
        meta, body = {}, text
        if text.startswith("---"):
            parts = text.split("---", 2)
            if len(parts) == 3:
                try:
                    meta = yaml.safe_load(parts[1]) or {}
                except yaml.YAMLError:
                    meta = {}
                body = parts[2]
        if meta.get("type") in types:
            out.append({"id": f.name, "path": str(f), "meta": meta, "body": body.strip()})
    return out


def cluster_findings(
    entries: list[dict[str, Any]],
    clusterer: Callable[[list[dict[str, Any]]], list[dict[str, Any]]],
) -> list[dict[str, Any]]:
    """Ask `clusterer` to group same-claim entries. Payload sent to the clusterer
    is compact (id/title/gist/type/session). Returns clusters of >=2 resolved
    members with confidence + rationale; singletons are dropped."""
    payload = [{
        "id": e["id"],
        "title": e["meta"].get("title", ""),
        "gist": e["body"][:200],
        "type": e["meta"].get("type"),
        "session": e["meta"].get("opencode_session_id"),
    } for e in entries]
    raw = clusterer(payload) or []
    by_id = {e["id"]: e for e in entries}
    clusters: list[dict[str, Any]] = []
    for c in raw:
        members = [by_id[i] for i in c.get("ids", []) if i in by_id]
        if len(members) >= 2:
            clusters.append({
                "members": members,
                "confidence": float(c.get("confidence", 0.0)),
                "rationale": c.get("rationale", ""),
            })
    return clusters


def _default_clusterer(payload: list[dict[str, Any]]) -> list[dict[str, Any]]:
    # Real LLM implementation lands in Task 5. Tests always inject a clusterer.
    return []


def consolidate_project_findings(
    store_dir: str,
    *,
    clusterer: Callable[[list[dict[str, Any]]], list[dict[str, Any]]] | None = None,
    types: tuple[str, ...] = NL_TYPES,
) -> dict[str, Any]:
    """Increment A (proposal-only): cluster a project's natural-language findings,
    compute each cluster's merged preview, and write them to
    `<store>/reflex/consolidation-proposal.json`. Mutates NO staging entries and
    applies nothing. Returns {"clusters": int, "artifact": str}."""
    clusterer = clusterer or _default_clusterer
    store = Path(store_dir)
    entries = _load_findings(store / "staging", types)
    clusters = cluster_findings(entries, clusterer)

    proposals = []
    for c in clusters:
        preview = merge_entries(c["members"])
        proposals.append({
            "confidence": c["confidence"],
            "rationale": c["rationale"],
            "sources": preview["sources"],
            "canonical": preview["canonical"],
            "merged_preview": {
                "title": preview["meta"].get("title", ""),
                "observation_count": preview["meta"]["observation_count"],
                "observed_in_sessions": preview["meta"]["observed_in_sessions"],
                "body": preview["body"],
            },
        })

    artifact = store / "reflex" / "consolidation-proposal.json"
    artifact.parent.mkdir(parents=True, exist_ok=True)
    artifact.write_text(json.dumps({"proposals": proposals, "applied": []}, indent=2))
    return {"clusters": len(proposals), "artifact": str(artifact)}
