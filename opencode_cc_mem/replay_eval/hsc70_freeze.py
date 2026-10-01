"""Freeze the hsc70 bake-off corpus and Arm-0 rent labels.

Distill-admission plan (projects/xiulian/docs/distill-admission-plan-2026-09-30.md),
harness section. Two manual subcommands, both read-only against the LIVE
hsc70_new store and the opencode DB; every write lands inside
replay_eval/corpus/hsc70_bakeoff/ (frozen, versioned):

    python -m replay_eval.hsc70_freeze corpus   # exports + transcripts + manifest
    python -m replay_eval.hsc70_freeze labels   # opencode DB -> labels.json

Corpus rules (harness §6.5): once frozen, never extended after seeing arm
results — new sessions go into a NEW corpus version with a reason.

Label semantics (plan O11): rent = distinct sessions whose memory_get_context
result SOURCES named the entry file. Attribution is by CONTENT (the entry id
must exist in the hsc70 store), so mis-pinned calls that served another
project's store are dropped by construction. CLEAN = sandbox/replay-class
sessions excluded (the 62→37 correction); the exclusion patterns and the
per-session contribution table are frozen in labels.json for audit.
"""

from __future__ import annotations

import hashlib
import json
import re
import sqlite3
import subprocess
import sys
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path

# Sandbox/replay-class session titles — the 2026-09-14 slice-validation and
# similar work that replayed store copies; O11: these inflated workhorse
# counts by 25. Extend ONLY with a corpus-version reason, never silently.
CLEAN_EXCLUDE_PATTERNS = (
    "sandbox", "slice-validation", "slice validation", "replay",
    "bake-off", "bakeoff", "distill-admission",
)

HSC70_STORE = Path(
    "/home/tjiang/repos/project_magnolia/opencode_cc_mem/projects/hsc70_new/.magnolia"
)
OPENCODE_DB = Path.home() / ".local/share/opencode/opencode.db"
CORPUS_DIR = Path(__file__).parent / "corpus" / "hsc70_bakeoff"

GET_CONTEXT_TOOL_HINT = '"tool":"compchem-memory_memory_get_context"'


def sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    h.update(path.read_bytes())
    return h.hexdigest()


def is_clean_title(title: str, extra_excludes: list[str] | None = None) -> bool:
    t = (title or "").lower()
    for pat in CLEAN_EXCLUDE_PATTERNS + tuple(extra_excludes or []):
        if pat.lower() in t:
            return False
    return True


def parse_get_context_sources(part_data: str) -> list[dict]:
    """Extract the sources list from one opencode DB tool part row.

    A part row is JSON: {type:'tool', tool:'compchem-memory_memory_get_context',
    state:{status:'completed', output:'<json str>'}}. output itself is the
    tool's JSON result string; sources is a list of {tier,id}. Returns [] for
    anything else (call parts, failed calls, other tools, unparseable).
    """
    try:
        row = json.loads(part_data)
    except (json.JSONDecodeError, TypeError):
        return []
    if not isinstance(row, dict) or row.get("type") != "tool":
        return []
    if row.get("tool") != "compchem-memory_memory_get_context":
        return []
    state = row.get("state") or {}
    if state.get("status") != "completed":
        return []
    output = state.get("output")
    if not isinstance(output, str):
        return []
    try:
        result = json.loads(output)
    except json.JSONDecodeError:
        return []
    sources = result.get("sources")
    if not isinstance(sources, list):
        return []
    return [s for s in sources if isinstance(s, dict) and s.get("id")]


def store_entry_ids(store: Path) -> set[str]:
    """All entry/staging filenames in the live store (content-attribution set)."""
    ids: set[str] = set()
    for sub in ("entries", "staging"):
        d = store / sub
        if d.is_dir():
            ids.update(f.name for f in d.glob("*.md"))
    return ids


def fetch_get_context_rows(db: Path) -> list[tuple[str, str]]:
    """(session_id, part_data) for every completed get_context tool part."""
    con = sqlite3.connect(str(db))
    try:
        rows = con.execute(
            "SELECT session_id, data FROM part WHERE data LIKE ?",
            (f"%{GET_CONTEXT_TOOL_HINT}%",),
        ).fetchall()
    finally:
        con.close()
    return rows


def fetch_session_titles(db: Path) -> dict[str, str]:
    con = sqlite3.connect(str(db))
    try:
        rows = con.execute("SELECT id, title FROM session").fetchall()
    finally:
        con.close()
    return {sid: title for sid, title in rows}


def compute_rent(
    rows: list[tuple[str, str]],
    titles: dict[str, str],
    valid_ids: set[str],
    clean_only: bool = True,
) -> dict:
    """Per-entry rent table + audit tables.

    Returns {entries: {fname: {sessions: [sid...], distinct: N}},
             excluded_sessions: {sid: title}, sessions: {sid: title}}.
    """
    surfaced: dict[str, set[str]] = defaultdict(set)
    used_titles: dict[str, str] = {}
    excluded: dict[str, str] = {}
    for sid, data in rows:
        title = titles.get(sid, "")
        clean = is_clean_title(title)
        if clean_only and not clean:
            excluded[sid] = title
            continue
        used_titles[sid] = title
        for src in parse_get_context_sources(data):
            fid = str(src.get("id", ""))
            if fid in valid_ids:
                surfaced[fid].add(sid)
    return {
        "entries": {
            fid: {"sessions": sorted(s), "distinct": len(s)}
            for fid, s in sorted(surfaced.items())
        },
        "workhorses": sorted(
            fid for fid, s in surfaced.items() if len(s) >= 3
        ),
        "never_surfaced": sorted(valid_ids - set(surfaced)),
        "excluded_sessions": excluded,
        "sessions": used_titles,
    }


def mapping_sessions(store: Path) -> list[dict]:
    """Chronological mapping rows {ts, sid}, UNION marker-only sids (v2: the
    mapping starts 2026-06-10 and missed early-June sessions that produced
    the workhorse labels; their markers exist)."""
    seen: dict[str, dict] = {}
    mapping = store / "opencode-sessions.jsonl"
    for line in mapping.read_text(encoding="utf-8", errors="replace").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            r = json.loads(line)
        except json.JSONDecodeError:
            continue
        sid = r.get("opencode_session_id")
        if sid:
            seen[sid] = {"ts": r.get("ts", ""), "sid": sid}
    mdir = store / "opencode-distilled"
    if mdir.is_dir():
        for f in mdir.glob("*.json"):
            sid = f.stem
            if sid and sid not in seen:
                seen[sid] = {"ts": "0000", "sid": sid}  # sort: unknown ts first
    # v3: DB-window recovery — sessions that PRODUCED store entries dated
    # before the mapping era (2026-06-10). Time tags on entry filenames match
    # to session windows in the opencode DB.
    try:
        seen.update(_pre_mapping_db_sids(store))
    except Exception as e:  # noqa: BLE001
        print(f"[v3] DB recovery skipped: {e}")
    return sorted(seen.values(), key=lambda m: m["ts"])


def _pre_mapping_db_sids(store: Path) -> dict[str, dict]:
    db = Path.home() / ".local/share/opencode/opencode.db"
    con = sqlite3.connect(str(db))
    cols = [r[1] for r in con.execute("PRAGMA table_info(session)")]
    tcol = next(c for c in cols if "time" in c.lower() or "created" in c.lower())
    rows = list(con.execute(f"SELECT id, {tcol} FROM session ORDER BY {tcol}"))
    con.close()
    sess = []
    for sid, t in rows:
        ts = datetime.fromtimestamp((t or 0) / 1000 if t and t > 10**12 else (t or 0),
                                    tz=timezone.utc)
        sess.append((sid, ts))
    out: dict[str, dict] = {}
    for sub in ("entries", "staging"):
        for f in (store / sub).glob("*.md"):
            m = re.match(r"(\d{8})_(\d{6})_", f.name)
            if not m:
                continue
            ets = datetime.strptime(m.group(1) + m.group(2), "%Y%m%d%H%M%S").replace(
                tzinfo=timezone.utc)
            if ets >= datetime(2026, 6, 10, tzinfo=timezone.utc):
                continue  # mapping era — already covered
            for i, (sid, st) in enumerate(sess):
                nxt = sess[i + 1][1] if i + 1 < len(sess) else None
                if st <= ets and (nxt is None or ets < nxt):
                    if sid not in out:
                        out[sid] = {"ts": st.isoformat(), "sid": sid}
                    break
    return out


def freeze_corpus() -> int:
    sys.path.insert(0, str(Path(__file__).parents[1] / "mcp-servers" / "compchem-memory" / "src"))
    from compchem_memory.opencode_ingest import export_session, reconstruct_transcript

    out = CORPUS_DIR
    (out / "exports").mkdir(parents=True, exist_ok=True)
    (out / "transcripts").mkdir(parents=True, exist_ok=True)

    manifest = {"frozen_at": datetime.now(timezone.utc).isoformat(), "sessions": []}
    sids = mapping_sessions(HSC70_STORE)
    print(f"mapping sessions: {len(sids)}")
    for i, m in enumerate(sids, 1):
        sid = m["sid"]
        export = export_session(sid)
        if not export:
            manifest["sessions"].append({"sid": sid, "ts": m["ts"], "error": "export failed"})
            print(f"[{i}/{len(sids)}] {sid}: EXPORT FAILED")
            continue
        (out / "exports" / f"{sid}.json").write_text(
            json.dumps(export, ensure_ascii=False), encoding="utf-8"
        )
        transcript = reconstruct_transcript(export)
        tpath = out / "transcripts" / f"{sid}.txt"
        tpath.write_text(transcript, encoding="utf-8")
        info = export.get("info") or {}
        title = info.get("title", "")
        manifest["sessions"].append({
            "sid": sid,
            "ts": m["ts"],
            "title": title,
            "chars": len(transcript),
            "clean": is_clean_title(title),
            "sha256_transcript": sha256_file(tpath),
        })
        print(f"[{i}/{len(sids)}] {sid}: {len(transcript)} chars — {title[:60]}")

    (out / "manifest.json").write_text(
        json.dumps(manifest, ensure_ascii=False, indent=1), encoding="utf-8"
    )
    ok = sum(1 for s in manifest["sessions"] if "sha256_transcript" in s)
    print(f"froze {ok}/{len(sids)} transcripts + manifest -> {out}")
    return 0


_ENTRY_TOKEN_RE = re.compile(r"\d{8}_\d{6}_\d{6}_[A-Za-z0-9_]{1,80}\.md")


def rent_from_mentions(
    rows: list[tuple[str, str]],
    titles: dict[str, str],
    valid_ids: set[str],
    clean_only: bool = True,
) -> dict:
    """Mention-based rent: an entry is 'surfaced' in a session if its FILENAME
    appears in any of that session's parts — this captures get_context
    sources, read/edit calls on entry files, and action-memory injections
    (which embed entry paths), i.e. every channel recoverable from the DB.
    Filenames are timestamped and unique, so token extraction is precise.
    """
    surfaced: dict[str, set[str]] = defaultdict(set)
    used_titles: dict[str, str] = {}
    excluded: dict[str, str] = {}
    for sid, data in rows:
        title = titles.get(sid, "")
        if clean_only and not is_clean_title(title):
            excluded[sid] = title
            continue
        used_titles[sid] = title
        for tok in _ENTRY_TOKEN_RE.findall(data or ""):
            if tok in valid_ids:
                surfaced[tok].add(sid)
    return {
        "entries": {
            fid: {"sessions": sorted(s), "distinct": len(s)}
            for fid, s in sorted(surfaced.items())
        },
        "workhorses": sorted(fid for fid, s in surfaced.items() if len(s) >= 3),
        "never_surfaced": sorted(valid_ids - set(surfaced)),
        "excluded_sessions": excluded,
        "sessions": used_titles,
    }


def fetch_parts_with_md(db: Path) -> list[tuple[str, str]]:
    """(session_id, data) for every part whose data mentions a .md filename."""
    con = sqlite3.connect(str(db))
    try:
        rows = con.execute(
            "SELECT session_id, data FROM part WHERE data LIKE '%.md%'"
        ).fetchall()
    finally:
        con.close()
    return rows



_INJECTION_MARKER = "[Magnolia · action-memory]"
_READ_ENTRY_RE = re.compile(r"\.magnolia/(?:entries|staging)/(\d{8}_\d{6}_\d{6}_[A-Za-z0-9_]{1,80}\.md)")


def rent_curated(
    rows: list[tuple[str, str]],
    titles: dict[str, str],
    valid_ids: set[str],
    clean_only: bool = True,
) -> dict:
    """Curated surfacing rent — the defensible middle between the naive
    extremes. Counts an entry as surfaced in a session ONLY via real
    knowledge-delivery channels:

    1. get_context result sources (selective retrieval), or
    2. a read tool call opening the entry file (agent chose to read it), or
    3. an action-memory injection block embedding the entry path.

    Bulk listings (scan_headers/search/consolidation reviews, my own freeze
    scripts) do NOT count — they enumerate filenames without delivering the
    entry as knowledge. This is why naive mention-rent found 291 workhorses
    where the plan's O11 method found 37.
    """
    surfaced: dict[str, set[str]] = defaultdict(set)
    used_titles: dict[str, str] = {}
    excluded: dict[str, str] = {}
    for sid, data in rows:
        title = titles.get(sid, "")
        if clean_only and not is_clean_title(title):
            excluded[sid] = title
            continue
        used_titles[sid] = title
        hits: set[str] = set()
        # Channel 1: get_context sources
        for src in parse_get_context_sources(data):
            fid = str(src.get("id", ""))
            if fid in valid_ids:
                hits.add(fid)
        try:
            row = json.loads(data or "")
        except (json.JSONDecodeError, TypeError):
            row = None
        if isinstance(row, dict) and row.get("type") == "tool":
            state = row.get("state") or {}
            output = state.get("output")
            if isinstance(output, str):
                # Channel 2: read of an entry file
                if row.get("tool") == "read":
                    inp = (state.get("input") or {}) if isinstance(state.get("input"), dict) else {}
                    fp = str(inp.get("filePath", ""))
                    m = _READ_ENTRY_RE.search(fp)
                    if m and m.group(1) in valid_ids:
                        hits.add(m.group(1))
                # Channel 3: action-memory injection (paths listed under the marker)
                if _INJECTION_MARKER in output:
                    for tok in _ENTRY_TOKEN_RE.findall(output):
                        if tok in valid_ids:
                            hits.add(tok)
        for fid in hits:
            surfaced[fid].add(sid)
    return {
        "entries": {
            fid: {"sessions": sorted(s), "distinct": len(s)}
            for fid, s in sorted(surfaced.items())
        },
        "workhorses": sorted(fid for fid, s in surfaced.items() if len(s) >= 3),
        "never_surfaced": sorted(valid_ids - set(surfaced)),
        "excluded_sessions": excluded,
        "sessions": used_titles,
    }



def freeze_labels() -> int:
    valid_ids = store_entry_ids(HSC70_STORE)
    titles = fetch_session_titles(OPENCODE_DB)

    # Channel 1: get_context tool-result sources (exact, tool-attributed).
    gc_rows = fetch_get_context_rows(OPENCODE_DB)
    print(f"get_context parts: {len(gc_rows)} | store entry ids: {len(valid_ids)}")
    gc_raw = compute_rent(gc_rows, titles, valid_ids, clean_only=False)
    gc_clean = compute_rent(gc_rows, titles, valid_ids, clean_only=True)

    # Channel 2: mention-based rent over ALL parts (get_context sources +
    # read/edit calls on entries + action-memory injections embedding entry
    # paths). This is the canonical workhorse set — the plan's O4/O11 rent
    # table was computed this way (top workhorses reach 50-70+ sessions,
    # impossible from get_context alone).
    md_rows = fetch_parts_with_md(OPENCODE_DB)
    print(f"parts mentioning .md: {len(md_rows)}")
    mn_raw = rent_from_mentions(md_rows, titles, valid_ids, clean_only=False)
    mn_clean = rent_from_mentions(md_rows, titles, valid_ids, clean_only=True)
    cu_raw = rent_curated(md_rows, titles, valid_ids, clean_only=False)
    cu_clean = rent_curated(md_rows, titles, valid_ids, clean_only=True)

    labels = {
        "frozen_at": datetime.now(timezone.utc).isoformat(),
        "store": str(HSC70_STORE),
        "valid_ids": len(valid_ids),
        "canonical": "curated_clean",
        "get_context": {
            "raw": {"ever": len(gc_raw["entries"]), "workhorses": len(gc_raw["workhorses"])},
            "clean": gc_clean,
        },
        "mentions": {
            "raw": {"ever": len(mn_raw["entries"]), "workhorses": len(mn_raw["workhorses"])},
            "clean": {"ever": len(mn_clean["entries"]), "workhorses": len(mn_clean["workhorses"])},
        },
        "curated": {
            "raw": {"ever": len(cu_raw["entries"]), "workhorses": len(cu_raw["workhorses"])},
            "clean": cu_clean,
        },
        "notes": (
            "rent = distinct sessions per entry. CANONICAL = curated_clean: "
            "get_context sources + entry reads + action-injection blocks "
            "(real knowledge delivery). naive mentions overcount (bulk "
            "listings); get_context alone undercounts. workhorse = >=3 "
            "distinct clean sessions. clean excludes sandbox/replay-class "
            "sessions by title (O11 62->37 correction)."
        ),
    }
    CORPUS_DIR.mkdir(parents=True, exist_ok=True)
    (CORPUS_DIR / "labels.json").write_text(
        json.dumps(labels, ensure_ascii=False, indent=1), encoding="utf-8"
    )
    print(
        f"get_context: raw ever={len(gc_raw['entries'])}/wh={len(gc_raw['workhorses'])} "
        f"clean ever={len(gc_clean['entries'])}/wh={len(gc_clean['workhorses'])}"
    )
    print(
        f"mentions:    raw ever={len(mn_raw['entries'])}/wh={len(mn_raw['workhorses'])} "
        f"clean ever={len(mn_clean['entries'])}/wh={len(mn_clean['workhorses'])}"
    )
    print(
        f"curated:     raw ever={len(cu_raw['entries'])}/wh={len(cu_raw['workhorses'])} "
        f"clean ever={len(cu_clean['entries'])}/wh={len(cu_clean['workhorses'])} "
        f"| excluded sessions={len(cu_clean['excluded_sessions'])}"
    )
    return 0


def finalize_corpus() -> int:
    """Reshape frozen transcripts into the replay_eval loader contract
    (manifest.yaml + extraction_gold/slices + slices_manifest.json).
    One slice per session, whole-transcript — matching production ingest,
    which distills the full post-cursor session in one call."""
    import shutil

    gold = CORPUS_DIR / "extraction_gold" / "slices"
    gold.mkdir(parents=True, exist_ok=True)
    manifest = json.loads((CORPUS_DIR / "manifest.json").read_text(encoding="utf-8"))
    entries = []
    SLICE_CHARS = 20000  # production cursor-slice median (hsc70: 19.6k, O-sizes entry)
    for s in manifest["sessions"]:
        if "sha256_transcript" not in s:
            continue
        text = (CORPUS_DIR / "transcripts" / f"{s['sid']}.txt").read_text(
            encoding="utf-8", errors="replace")
        # v4: chunk to production slice sizes at line boundaries (production
        # distills ~20k-char cursor slices, not whole sessions; run-1..3
        # whole-session shape caused hijack/clipping and failed fidelity).
        chunks, buf = [], ""
        for line in text.splitlines(keepends=True):
            if buf and len(buf) + len(line) > SLICE_CHARS:
                chunks.append(buf)
                buf = ""
            buf += line
        if buf.strip():
            chunks.append(buf)
        for k, chunk in enumerate(chunks):
            name = s["sid"] if len(chunks) == 1 else f"{s['sid']}__k{k}"
            (gold / f"{name}.txt").write_text(chunk, encoding="utf-8")
            entries.append({
                "name": name,
                "project": "hsc70_new",
                "session": s["sid"],
                "capture_version": "export-1.18.33-prodslice20k",
                "chars": len(chunk),
                "clean": s.get("clean", True),
            })
    (CORPUS_DIR / "extraction_gold" / "slices_manifest.json").write_text(
        json.dumps(entries, indent=1), encoding="utf-8"
    )
    (CORPUS_DIR / "manifest.yaml").write_text(
        "frozen: true\n"
        "corpus_version: 4\n"
        "capture_version: export-1.18.33-prodslice20k\n"
        f"changes:\n  - version: 1\n    date: {datetime.now(timezone.utc).date().isoformat()}\n"
        "    reason: 'initial freeze of hsc70_new session transcripts for the "
        "distiller admission bake-off (plan 2026-09-30); one slice per "
        "session, whole-transcript shape matching production ingest'\n"
        f"  - version: 2\n    date: {datetime.now(timezone.utc).date().isoformat()}\n"
        "    reason: 'add 7 pre-mapping sessions recovered from distill "
        "markers (mapping starts 2026-06-10; workhorse-label sessions were "
        "missing, causing bake-off run 1 fidelity/recall FAIL)'\n"
        f"  - version: 3\n    date: {datetime.now(timezone.utc).date().isoformat()}\n"
        "    reason: 'time-tag recovery — sessions that PRODUCED pre-mapping "
        "store entries, matched by entry-timestamp to DB session windows "
        "(run-2 recall 3/36 root cause — 17/36 workhorse-producers absent)'\n"
        f"  - version: 4\n    date: {datetime.now(timezone.utc).date().isoformat()}\n"
        "    reason: 'slice transcripts to production cursor-slice sizes "
        "(~20k chars): production distills slices, not whole sessions; "
        "whole-session shape caused truncation and fidelity/recall FAIL "
        "in runs 1-3 (matches promoted hijack rule)'\n",
        encoding="utf-8",
    )
    print(f"finalized {len(entries)} slices into extraction_gold/")
    return 0


def main(argv: list[str]) -> int:
    if len(argv) != 2 or argv[1] not in ("corpus", "labels", "finalize"):
        print(__doc__)
        return 2
    return {
        "corpus": freeze_corpus,
        "labels": freeze_labels,
        "finalize": finalize_corpus,
    }[argv[1]]()


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
