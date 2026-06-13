"""Finding consolidation (self-reflex): detect same-claim findings with an LLM,
compute a deterministic merged preview. Increment A is proposal-only — it writes
proposals and mutates nothing.

Detect with intelligence (LLM clusters same-claim findings), mutate with
determinism (this module assembles merges from source text — it never authors
new knowledge), human-confirm (Increment B). Scoped to natural-language types;
deterministic tool-output keeps the existing lexical dedup.
"""

from typing import Any, Callable

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
