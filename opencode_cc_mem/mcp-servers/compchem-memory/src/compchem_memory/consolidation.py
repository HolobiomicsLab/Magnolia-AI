"""Finding consolidation (self-reflex): detect same-claim findings with an LLM,
compute a deterministic merged preview. Increment A is proposal-only — it writes
proposals and mutates nothing.

Detect with intelligence (LLM clusters same-claim findings), mutate with
determinism (this module assembles merges from source text — it never authors
new knowledge), human-confirm (Increment B). Scoped to natural-language types;
deterministic tool-output keeps the existing lexical dedup.
"""

import json
import os
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable

import yaml

from compchem_memory.reflex_common import (
    parse_frontmatter_file as _parse_entry,
    pending_indices as _pending_indices,
)

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
        if meta.get("parked"):
            continue  # R9: parked entries stay out of the consolidation pool
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
    "Before grouping a pair, ask: if these entries were merged into one, would "
    "any information be lost? If yes, do NOT group them. Return FEWER, "
    "higher-certainty clusters rather than more speculative ones — an EMPTY "
    "clusters list is a valid and common answer. Set confidence to your honest "
    "certainty that the entries assert the same claim; never inflate it. "
    'Return JSON: {"clusters": [{"ids": [...], "confidence": 0.0-1.0, '
    '"rationale": "one line"}]}. Only include clusters with 2+ ids.'
)

# Generation-time floor (2026-10-06, volume tuning): clusters the judge scores
# below this never become pending proposals — they are appended to
# reflex/suppressed-proposals.jsonl instead. Measured on live batches, the
# judge emitted 54.5% weak-band (0.5-0.79 "same topic only") groupings, which
# the human then rejects one by one; the floor cuts that inflow at the source
# while the side log keeps the strictness improvement measurable. The human
# review queue only ever holds clusters at or above the floor.
PROPOSAL_FLOOR_DEFAULT = 0.65


def _proposal_floor() -> float:
    try:
        return float(os.environ.get(
            "MAGNOLIA_CONSOLIDATION_FLOOR", PROPOSAL_FLOOR_DEFAULT))
    except (TypeError, ValueError):
        return PROPOSAL_FLOOR_DEFAULT


def _log_suppressed(store: Path, suppressed: list[dict[str, Any]], floor: float) -> None:
    """Append sub-floor clusters to the side log (measurement, not review).
    Never raises — suppression bookkeeping must not break proposal generation."""
    if not suppressed:
        return
    try:
        artifact = store / "reflex" / "suppressed-proposals.jsonl"
        artifact.parent.mkdir(parents=True, exist_ok=True)
        with open(artifact, "a") as f:
            for p in suppressed:
                f.write(json.dumps({
                    "ts": datetime.now(timezone.utc).isoformat(),
                    "confidence": p["confidence"],
                    "floor": floor,
                    "rationale": p["rationale"],
                    "sources": [Path(s).name for s in p["sources"]],
                }) + "\n")
    except Exception as e:  # noqa: BLE001 - side log must never break the sweep
        print(f"[consolidation] suppressed-log skipped: {e}")


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
    applies nothing — EXCEPT when MAGNOLIA_CONSOLIDATION_AUTO is set, in which
    case the high-confidence band is applied immediately (see auto_apply_band).
    Clusters below the generation-time floor (PROPOSAL_FLOOR_DEFAULT,
    MAGNOLIA_CONSOLIDATION_FLOOR override) never reach the queue; they are
    appended to reflex/suppressed-proposals.jsonl so strictness stays measurable.
    Returns {"clusters": int, "suppressed": int, "artifact": str,
    "auto_applied": int}."""
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

    # Volume-tuning floor: sub-floor clusters go to the side log, not the queue.
    floor = _proposal_floor()
    suppressed = [p for p in proposals if p["confidence"] < floor]
    _log_suppressed(store, suppressed, floor)
    proposals = [p for p in proposals if p["confidence"] >= floor]

    artifact = store / "reflex" / "consolidation-proposal.json"
    artifact.parent.mkdir(parents=True, exist_ok=True)
    # Carry forward prior REJECTIONS durably: rejection is non-destructive (the
    # sources stay in staging), so a rejected cluster would otherwise re-cluster
    # on a later sweep and re-surface forever. Rejected keys persist in the
    # artifact's `rejected_keys` across regenerations (the legacy index-only
    # scheme carried them just one sweep deep, so a rejected pair re-appeared
    # every other batch). A new proposal is auto-rejected when it CONTAINS a
    # previously rejected pair. (Applied merges removed their sources, so they
    # cannot recur — applied resets to [].)
    prior_rejected_keys = _prior_rejected_keys(artifact)
    rejected = [i for i, p in enumerate(proposals)
                if _contains_rejected_pair(p["sources"], prior_rejected_keys)]
    artifact.write_text(json.dumps(
        {"proposals": proposals, "applied": [], "rejected": rejected,
         "rejected_keys": _serialize_keys(prior_rejected_keys)}, indent=2))
    # Auto-band (2026-10-05): with MAGNOLIA_CONSOLIDATION_AUTO set, apply the
    # high-confidence band right here so the human review only ever sees the
    # proposals that actually need judgment. Off by default — without the
    # variable this function stays proposal-only (Increment A contract).
    auto_applied = auto_apply_band(store_dir).get("applied", 0)
    return {"clusters": len(proposals), "suppressed": len(suppressed),
            "artifact": str(artifact), "auto_applied": auto_applied}


def _cluster_key(sources: list[str]) -> tuple[str, ...]:
    """Content identity of a cluster — its sorted source basenames. Stable across
    artifact regeneration, unlike a positional index."""
    return tuple(sorted(Path(s).name for s in sources))


def _serialize_keys(keys: set[tuple[str, ...]]) -> list[list[str]]:
    """JSON-serializable, deterministically ordered form of a key set."""
    return sorted(list(k) for k in keys if len(k) >= 2)


def _contains_rejected_pair(sources: list[str], rejected_keys: set[tuple[str, ...]]) -> bool:
    """True if the proposed cluster CONTAINS any durably-rejected pair. Subset
    semantics: 'these sources do not belong together' holds however the clusterer
    re-groups them — including larger supersets that add a third member."""
    key_set = set(_cluster_key(sources))
    return any(set(k) <= key_set for k in rejected_keys if len(k) >= 2)


def _prior_rejected_keys(artifact: Path) -> set[tuple[str, ...]]:
    """Content keys of durably rejected clusters. Reads the artifact's
    `rejected_keys` (persistent across regenerations); also folds in the legacy
    index-based `rejected` list so artifacts written before `rejected_keys`
    existed still contribute their rejections."""
    if not artifact.exists():
        return set()
    try:
        prior = json.loads(artifact.read_text())
    except (json.JSONDecodeError, OSError):
        return set()
    keys: set[tuple[str, ...]] = set()
    for k in prior.get("rejected_keys", []):
        if isinstance(k, list) and len(k) >= 2:
            keys.add(tuple(sorted(k)))
    old = prior.get("proposals", [])
    for idx in prior.get("rejected", []):
        if isinstance(idx, int) and 0 <= idx < len(old):
            keys.add(_cluster_key(old[idx].get("sources", [])))
    return keys


def _current_session(store: Path) -> str:
    try:
        return (store / ".current-session-id").read_text().strip()
    except OSError:
        return ""


def _append_labels(store: Path, rows: list[dict[str, Any]]) -> None:
    """Append human/auto verdict rows to reflex/labels.jsonl (the label store:
    ground truth for bake-off arms and future retirement tuning). Never raises —
    labels are bookkeeping and must not break the review→apply path."""
    if not rows:
        return
    try:
        with open(store / "reflex" / "labels.jsonl", "a") as f:
            for r in rows:
                f.write(json.dumps(r) + "\n")
    except Exception as e:  # noqa: BLE001 - labels must never break apply
        print(f"[consolidation] label-log skipped: {e}")


def apply_proposals(
    store_dir: str, accepted: list[int], reject: list[int] | None = None,
    via: str = "human",
) -> dict[str, Any]:
    """Apply the accepted proposal indices via apply_merge and record `reject`ed
    indices as durably handled (so they stop re-surfacing). A proposal whose
    sources no longer exist on disk (already merged / promoted / archived) is
    STALE: it is durably DISMISSED (artifact `dismissed` list + its source pair
    into `rejected_keys`) instead of staying pending forever. Marks state in the
    artifact and persists it AFTER EACH handled proposal, so a later failure
    can't lose earlier marks. A merge that raises is recorded in `failed` and
    never aborts the batch. Unknown / out-of-range / already-handled indices are
    ignored. `via` marks who decided ("human" review or "auto" band) and lands
    in the label store row. Returns {"applied": int, "merged": [...],
    "dismissed": [...], "rejected": int, "failed": [...]}."""
    store = Path(store_dir)
    artifact = store / "reflex" / "consolidation-proposal.json"
    if not artifact.exists():
        return {"applied": 0, "merged": [], "dismissed": [], "rejected": 0, "failed": []}
    data = json.loads(artifact.read_text())
    proposals = data.get("proposals", [])
    already = set(data.get("applied", []))
    rejected_set = set(data.get("rejected", []))
    dismissed_set = set(data.get("dismissed", []))
    rejected_keys: set[tuple[str, ...]] = set()
    for k in data.get("rejected_keys", []):
        if isinstance(k, list) and len(k) >= 2:
            rejected_keys.add(tuple(sorted(k)))

    def _persist():
        data["applied"] = sorted(already)
        data["rejected"] = sorted(rejected_set)
        data["dismissed"] = sorted(dismissed_set)
        data["rejected_keys"] = _serialize_keys(rejected_keys)
        artifact.write_text(json.dumps(data, indent=2))

    def _label(verdict: str, p: dict[str, Any], key: tuple[str, ...],
               merged: str | None = None) -> dict[str, Any]:
        return {
            "ts": datetime.now(timezone.utc).isoformat(),
            "verdict": verdict,
            "via": via,
            "session": _current_session(store),
            "cluster_key": list(key),
            "confidence": p.get("confidence"),
            "rationale": p.get("rationale", ""),
            "sources": sorted(Path(s).name for s in p.get("sources", [])),
            **({"merged": Path(merged).name} if merged else {}),
        }

    label_rows: list[dict[str, Any]] = []
    rejected_count = 0
    for i in reject or []:
        if (isinstance(i, int) and 0 <= i < len(proposals)
                and i not in already and i not in rejected_set and i not in dismissed_set):
            rejected_set.add(i)
            key = _cluster_key(proposals[i].get("sources", []))
            if len(key) >= 2:
                rejected_keys.add(key)
            label_rows.append(_label("reject", proposals[i], key))
            rejected_count += 1

    merged_paths: list[str] = []
    dismissed: list[int] = []
    failed: list[int] = []
    for i in accepted:
        if (not isinstance(i, int) or i < 0 or i >= len(proposals)
                or i in already or i in rejected_set or i in dismissed_set):
            continue
        try:
            res = apply_merge(proposals[i].get("sources", []))
        except Exception as e:  # noqa: BLE001 - one bad merge must not abort the batch
            print(f"[consolidation] apply failed for proposal {i}: {e}")
            failed.append(i)
            label_rows.append(_label(
                "error", proposals[i],
                _cluster_key(proposals[i].get("sources", []))))
            continue
        if res["skipped"]:
            # Stale cluster: fewer than two sources survive on disk (the entries
            # were already merged/promoted elsewhere). Dismiss it durably — a
            # transient `skipped` left the index pending forever, so the review
            # re-surfaced it every session.
            dismissed_set.add(i)
            key = _cluster_key(proposals[i].get("sources", []))
            if len(key) >= 2:
                rejected_keys.add(key)
            dismissed.append(i)
            label_rows.append(_label("dismiss_stale", proposals[i], key))
            _persist()
        else:
            merged_paths.append(res["merged"])
            already.add(i)
            label_rows.append(_label(
                "accept", proposals[i],
                _cluster_key(proposals[i].get("sources", [])),
                merged=res["merged"]))
            _persist()  # incremental: earlier marks survive a later failure

    _persist()
    _append_labels(store, label_rows)
    return {"applied": len(merged_paths), "merged": merged_paths,
            "dismissed": dismissed, "rejected": rejected_count, "failed": failed}


def backfill_labels(store_dir: str) -> dict[str, Any]:
    """One-time import of proposals handled BEFORE the label store existed into
    reflex/labels.jsonl. Reads the artifact's applied/rejected/dismissed marks;
    applied indices that appear in an auto-band receipt are labelled via="auto",
    all others via="human". Content-key idempotent: cluster keys already present
    in the label store are skipped, so re-running is safe. Backfilled rows carry
    "backfilled": true and ts = import time (the original decision time is not
    recorded in the artifact). Returns {"written": int, "skipped": int}."""
    store = Path(store_dir)
    artifact = store / "reflex" / "consolidation-proposal.json"
    if not artifact.exists():
        return {"written": 0, "skipped": 0}
    data = json.loads(artifact.read_text())
    proposals = data.get("proposals", [])
    try:
        existing = {tuple(sorted(r["cluster_key"])) for r in
                    (json.loads(l) for l in
                     (store / "reflex" / "labels.jsonl").read_text().splitlines())
                    if r.get("cluster_key")}
    except (OSError, json.JSONDecodeError):
        existing = set()
    auto_idx: set[int] = set()
    try:
        for line in (store / "reflex" / "consolidation-auto-log.jsonl").read_text().splitlines():
            auto_idx.update(json.loads(line).get("requested", []))
    except (OSError, json.JSONDecodeError):
        pass
    verdicts = ([(i, "accept") for i in data.get("applied", [])]
                + [(i, "reject") for i in data.get("rejected", [])]
                + [(i, "dismiss_stale") for i in data.get("dismissed", [])])
    rows: list[dict[str, Any]] = []
    for i, verdict in verdicts:
        if not (isinstance(i, int) and 0 <= i < len(proposals)):
            continue
        p = proposals[i]
        key = _cluster_key(p.get("sources", []))
        if key in existing:
            continue
        existing.add(key)
        rows.append({
            "ts": datetime.now(timezone.utc).isoformat(),
            "verdict": verdict,
            "via": "auto" if i in auto_idx else "human",
            "session": _current_session(store),
            "cluster_key": list(key),
            "confidence": p.get("confidence"),
            "rationale": p.get("rationale", ""),
            "sources": sorted(Path(s).name for s in p.get("sources", [])),
            "backfilled": True,
        })
    _append_labels(store, rows)
    return {"written": len(rows), "skipped": len(verdicts) - len(rows)}


# ── Auto-band (2026-10-05) ─────────────────────────────────────────────────
# With MAGNOLIA_CONSOLIDATION_AUTO on, pending proposals in the high-confidence
# band are applied without human review. The band starts at 0.8 — the review
# file's own glossary line: "same claim, near-duplicate (accepting is safe)".
# Everything below the band (0.5-0.79 "same topic only", weak groupings) and
# any cluster containing a durably rejected pair keeps waiting for a human.
# Off by default: without the variable, nothing changes.
AUTO_BAND_MIN_CONFIDENCE = 0.8
AUTO_BAND_DEFAULT_CAP = 5


def _env_flag(name: str) -> bool:
    return str(os.environ.get(name, "")).strip().lower() in (
        "1", "true", "yes", "on")


def auto_apply_band(store_dir: str) -> dict[str, Any]:
    """Apply pending proposals in the auto band (see AUTO_BAND_MIN_CONFIDENCE).

    No-op unless MAGNOLIA_CONSOLIDATION_AUTO is set (same accepted values as
    the admission gate: 1/true/yes/on). At most MAGNOLIA_CONSOLIDATION_AUTO_MAX
    proposals per call (default AUTO_BAND_DEFAULT_CAP), highest confidence
    first, so one bad sweep cannot rewrite the whole store. Appends one JSONL
    receipt row per run to <store>/reflex/consolidation-auto-log.jsonl — the
    seed of the label store: what was auto-applied, at which confidence, from
    which sources, so later analysis can audit the band. Applies through the
    same apply_proposals path as human review (identical merges, same versioning
    commits). Never raises."""
    try:
        if not _env_flag("MAGNOLIA_CONSOLIDATION_AUTO"):
            return {"applied": 0, "merged": [], "skipped_reason": "disabled"}
        store = Path(store_dir)
        artifact = store / "reflex" / "consolidation-proposal.json"
        if not artifact.exists():
            return {"applied": 0, "merged": [], "skipped_reason": "no_artifact"}
        data = json.loads(artifact.read_text())
        proposals = data.get("proposals", [])
        pending = _pending_indices(data)
        try:
            cap = int(os.environ.get(
                "MAGNOLIA_CONSOLIDATION_AUTO_MAX", AUTO_BAND_DEFAULT_CAP))
        except ValueError:
            cap = AUTO_BAND_DEFAULT_CAP
        cap = max(cap, 0)

        conf_by_index: dict[int, float] = {}
        for i in pending:
            try:
                conf = float(proposals[i].get("confidence") or 0.0)
            except (TypeError, ValueError):
                conf = 0.0
            if conf >= AUTO_BAND_MIN_CONFIDENCE:
                conf_by_index[i] = conf
        # Highest confidence first; ties resolved by artifact order (index).
        chosen = sorted(conf_by_index, key=lambda i: (-conf_by_index[i], i))[:cap]
        if not chosen:
            return {"applied": 0, "merged": [], "skipped_reason": "none_in_band"}

        res = apply_proposals(store_dir, accepted=chosen, via="auto")
        receipt = {
            "ts": datetime.now(timezone.utc).isoformat(),
            "auto": True,
            "requested": chosen,
            "applied": res["applied"],
            "merged": res["merged"],
            "dismissed": res["dismissed"],
            "failed": res["failed"],
            "candidates": [{"index": i,
                            "confidence": conf_by_index[i],
                            "sources": [Path(s).name
                                        for s in proposals[i].get("sources", [])]}
                           for i in chosen],
        }
        with open(store / "reflex" / "consolidation-auto-log.jsonl", "a") as f:
            f.write(json.dumps(receipt) + "\n")
        # Surface the auto-action: a store rewrite the user never saw is the
        # failure mode this project distrusts. The notice rides the existing
        # .distill-notices queue (latched — identical repeats don't pile up).
        try:
            from compchem_memory import distill_log
            n = res["applied"]
            if n:
                distill_log.push_distill_notice(
                    str(store.parent),
                    f"auto-band applied {n} proposal(s): "
                    + "; ".join(Path(m).name for m in res["merged"]),
                    f"Auto-merged {n} high-confidence duplicate set(s) "
                    f"(>= {AUTO_BAND_MIN_CONFIDENCE}); details in "
                    "reflex/consolidation-auto-log.jsonl — nothing needs your "
                    "action, this is the heads-up.")
        except Exception:
            pass
        return res
    except Exception as e:  # noqa: BLE001 - auto-band must never break the sweep
        print(f"[consolidation] auto-band skipped: {e}")
        return {"applied": 0, "merged": [], "failed": [], "error": str(e)}


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


def render_review_markdown(store_dir: str) -> str | None:
    """Write a human-readable review of the UNAPPLIED proposals to a VISIBLE,
    ephemeral `<project>/magnolia-review/proposals.md` (sibling of .magnolia — not
    hidden). Returns the path, or None if there is nothing left to review."""
    store = Path(store_dir)
    artifact = store / "reflex" / "consolidation-proposal.json"
    if not artifact.exists():
        return None
    data = json.loads(artifact.read_text())
    proposals = data.get("proposals", [])
    pending = _pending_indices(data)
    if not pending:
        return None

    lines = [
        "# Consolidation proposals — review",
        "",
        "Each finding below was distilled multiple times; the agent proposes merging",
        "the duplicates into one entry. For each: leave `action: accept` to merge, or",
        "change it to `reject`. Then tell the agent which to apply (e.g. \"apply 0 and 2\").",
        "",
        "How to read this file:",
        "",
        "- **confidence** is the clustering model's own certainty that the entries",
        "  assert the SAME claim: **>= 0.8** same claim, near-duplicate (accepting",
        "  is safe); **0.5-0.79** grouped despite doubt, usually same topic with",
        "  different claims (read the `why` line; default to reject); **< 0.5** weak.",
        "- **accept** merges the sources into one entry under the title below;",
        "  member bodies are kept as corroborating observations; the merge is",
        "  committed to the versioning repo (git-reversible). **reject** durably",
        "  dismisses this proposal; the entries stay separate.",
        "- Nothing is applied until you tell the agent.",
        "",
    ]
    for i in pending:
        p = proposals[i]
        mp = p.get("merged_preview", {})
        try:
            conf = float(p.get("confidence") or 0.0)
        except (TypeError, ValueError):
            conf = 0.0
        if conf >= 0.8:
            conf_words = "same claim, near-duplicate"
        elif conf >= 0.5:
            conf_words = "grouped despite doubt — usually same topic, different claims"
        else:
            conf_words = "weak grouping"
        lines += [
            f"## [{i}] {mp.get('title', '')}",
            "- action: accept",
            f"- confidence: {conf} — {conf_words}  |  distinct sessions: {mp.get('observation_count')}",
            f"- why: {p.get('rationale', '')}",
            "- sources:",
        ]
        lines += [f"    - {Path(s).name}" for s in p.get("sources", [])]
        body = mp.get("body", "") or ""
        shown = body[:1500] + ("\n…(truncated)" if len(body) > 1500 else "")
        # `~~~~` fence won't be closed early by a ``` block inside a finding body.
        lines += [
            "",
            "<details><summary>merged preview</summary>",
            "",
            "~~~~",
            shown,
            "~~~~",
            "</details>",
            "",
        ]

    review_dir = store.parent / "magnolia-review"
    review_dir.mkdir(parents=True, exist_ok=True)
    out = review_dir / "proposals.md"
    out.write_text("\n".join(lines), encoding="utf-8")
    return str(out)
