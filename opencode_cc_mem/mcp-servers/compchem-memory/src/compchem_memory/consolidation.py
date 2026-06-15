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


# Findings are clustered in title-sorted batches (near-duplicates sort adjacent,
# so cross-batch misses are rare). The clustering call disables thinking and uses
# temperature 0: deterministic output, and a reasoning model no longer spends its
# token budget on reasoning_content (which previously starved `content` to empty
# on large batches, silently dropping clusters).
_CLUSTER_BATCH = 30

_CLUSTER_SYSTEM = (
    "You consolidate a computational-chemistry project's memory. Given a JSON "
    "list of finding entries (id, title, gist), group ONLY entries that assert "
    "the SAME claim about the SAME system. Do NOT group entries that merely "
    "share a topic or differ in any material detail (different peptide, metric, "
    "residue, or conclusion). Most entries will be singletons. "
    'Return JSON: {"clusters": [{"ids": [...], "confidence": 0.0-1.0, '
    '"rationale": "one line"}]}. Only include clusters with 2+ ids.'
)


def _default_clusterer(payload: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """LLM clusterer: group findings that assert the SAME claim about the SAME
    system. Conservative — never group merely-related items. Processes the
    payload in small title-sorted batches so a reasoning model's budget is not
    exhausted on a large payload. Returns [{"ids":[...], "confidence":float,
    "rationale":str}]; batches that fail the LLM call contribute nothing."""
    from compchem_memory.llm import call_llm_json

    items = sorted(payload, key=lambda p: (p.get("title") or "").lower())
    clusters: list[dict[str, Any]] = []
    for i in range(0, len(items), _CLUSTER_BATCH):
        batch = items[i:i + _CLUSTER_BATCH]
        result = call_llm_json(_CLUSTER_SYSTEM, json.dumps(batch), max_tokens=4000,
                               temperature=0, disable_thinking=True)
        if isinstance(result, dict):
            # `or []` guards against {"clusters": null} — valid JSON the LLM could emit.
            clusters.extend(c for c in (result.get("clusters") or []) if isinstance(c, dict))
    return clusters


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


def _parse_entry(path: str | Path) -> dict[str, Any] | None:
    """Load one staging entry as {"id","path","meta","body"}, or None if missing."""
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


def apply_merge(source_paths: list[str]) -> dict[str, Any]:
    """Apply one consolidation: re-read the still-present sources, merge them, write
    the merged entry OVER the canonical file, and remove the other sources. Re-reads
    from disk (authoritative — tolerates sources changed/removed since the proposal).
    Requires >=2 surviving sources, else skips. Returns
    {"merged": path|None, "removed": [...], "skipped": bool}."""
    entries = [e for e in (_parse_entry(p) for p in source_paths) if e]
    if len(entries) < 2:
        return {"merged": None, "removed": [], "skipped": True}
    merged = merge_entries(entries)
    canonical = merged["canonical"]
    Path(canonical).write_text(
        "---\n"
        + yaml.dump(merged["meta"], default_flow_style=False, allow_unicode=True)
        + "---\n\n" + merged["body"].strip() + "\n",
        encoding="utf-8",
    )
    removed = []
    for e in entries:
        if e["path"] != canonical:
            Path(e["path"]).unlink(missing_ok=True)
            removed.append(e["path"])
    return {"merged": canonical, "removed": removed, "skipped": False}
