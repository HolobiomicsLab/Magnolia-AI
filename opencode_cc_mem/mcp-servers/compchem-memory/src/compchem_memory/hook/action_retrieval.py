"""Action-time memory retrieval shared by client hook adapters.

Retrieval is read-only and failure-tolerant. The search runs in a daemon thread
so a deadline can return without joining it; the hook CLI terminates with
``os._exit`` after rendering, preventing a timed-out search from holding the
process. When ``MAGNOLIA_HOOK_TEST == "1"``, the test-only environment variable
``MAGNOLIA_HOOK_TEST_SEARCH_SLEEP`` delays the search by the given seconds.

Deliberate change: OpenCode starts retrieval before a tool and injects after it;
the portable hook runs at post-tool time because one-shot clients cannot retain
that promise. Its 120-second, 50-entry cache therefore lives in session state,
keyed by a SHA-256 of the query after the transcript's secret redaction, so no
tool argument text is written to the state file.
"""

from __future__ import annotations

import hashlib
import json
import os
import queue
import re
import threading
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path

from compchem_memory import quick_search, surfacing
from compchem_memory.hook.events import HookEvent, canonical_tool
from compchem_memory.hook.project import ProjectPaths
from compchem_memory.hook.state import SessionState, private_mkdir


CACHE_TTL_SECONDS = 120.0
MAX_CACHE_ENTRIES = 50
MAX_HITS = 4
MIN_SCORE = 3
MAX_INJECTIONS_PER_SESSION = 8
MAX_PER_ENTRY_PER_SESSION = 2

ACTION_TOOLS = frozenset(
    {
        "compchem-tools.run_shell",
        "compchem-tools.submit_job",
        "submit_job",
        "write",
        "edit",
        "compchem-tools.gnina_dock",
        "compchem-tools.gromacs_run",
        "compchem-tools.gromacs_setup",
        "compchem-tools.haddock3_run",
        "compchem-tools.gaussian_run",
        "compchem-tools.orca_run",
        "compchem-tools.xtb_optimize",
        "compchem-tools.xtb_singlepoint",
        "compchem-tools.preprocess_pdb",
        "compchem-tools.run_acpype",
    }
)

_DISABLED = frozenset({"0", "off", "false", "no"})
_STOP = frozenset(
    {
        "about",
        "there",
        "these",
        "those",
        "which",
        "while",
        "their",
        "would",
        "could",
        "should",
        "where",
        "after",
        "before",
        "under",
        " learning",
        "memory",
        "project",
        "entry",
        "staging",
        "magnolia",
    }
)


@dataclass
class _Delivery:
    """A selected note's reservation and the rows it records once settled.

    Selection reserves one unit of the session budget and one use of each entry
    under the state lock, so overlapping post-tool hooks of one session cannot
    exceed the caps. ``commit_exposure`` keeps the reservation; ``discard_note``
    releases it. Whichever runs first settles the note; later calls do nothing.
    """

    paths: ProjectPaths
    harness: str
    session_id: str
    entry_paths: list[str]
    pending: dict
    coarse_row: dict
    hits_row: dict
    settled: bool = False


@dataclass
class ActionNote:
    """An injection block and the serving-store rows it can expose."""

    text: str
    sources_by_store: dict[Path, list[dict]]
    delivery: _Delivery | None = field(default=None, repr=False)


def serialise_note(note: ActionNote) -> dict:
    """Return the JSON-safe reservation needed to deliver a note later."""
    delivery = note.delivery
    if delivery is None:
        return {"text": note.text, "sources": {}}
    return {
        "text": note.text,
        "sources": {str(store): rows for store, rows in note.sources_by_store.items()},
        "delivery": {
            "harness": delivery.harness,
            "session_id": delivery.session_id,
            "entry_paths": delivery.entry_paths,
            "pending": delivery.pending,
            "coarse_row": delivery.coarse_row,
            "hits_row": delivery.hits_row,
        },
    }


def restore_note(payload: dict, paths: ProjectPaths) -> ActionNote | None:
    """Rebuild a carried note without taking a second reservation."""
    text = payload.get("text")
    raw_delivery = payload.get("delivery")
    if not isinstance(text, str) or not isinstance(raw_delivery, dict):
        return None
    sources = payload.get("sources")
    sources_by_store = {
        Path(store): rows for store, rows in sources.items()
        if isinstance(store, str) and isinstance(rows, list)
    } if isinstance(sources, dict) else {}
    delivery = _Delivery(
        paths=paths,
        harness=str(raw_delivery.get("harness") or "vibe"),
        session_id=str(raw_delivery.get("session_id") or ""),
        entry_paths=[str(item) for item in raw_delivery.get("entry_paths", [])],
        pending=dict(raw_delivery.get("pending") or {}),
        coarse_row=dict(raw_delivery.get("coarse_row") or {}),
        hits_row=dict(raw_delivery.get("hits_row") or {}),
    )
    return ActionNote(text, sources_by_store, delivery)


def note_entry_ids(note: ActionNote) -> list[str]:
    """Return stable source identifiers for an injected transcript row."""
    delivery = note.delivery
    return [] if delivery is None else list(delivery.entry_paths)


def args_to_query(args: object) -> str:
    """Collect short string leaves through depth two and cap at 400 chars."""
    parts: list[str] = []

    def walk(value: object, depth: int) -> None:
        if value is None:
            return
        if isinstance(value, str):
            if 0 < len(value) < 2000:
                parts.append(value)
        elif depth < 2:
            if isinstance(value, (list, tuple)):
                for item in value:
                    walk(item, depth + 1)
            elif isinstance(value, dict):
                for item in value.values():
                    walk(item, depth + 1)

    walk(args, 0)
    return " ".join(parts)[:400]


def _cache_key(tool_name: str, args: object) -> str:
    """Hash the query text after key-aware secret redaction; store no argument text."""
    from compchem_memory.hook.transcript import _redact_secret_keys
    from compchem_memory.opencode_ingest import scrub_secrets

    text = scrub_secrets(f"{tool_name} {args_to_query(_redact_secret_keys(args))}")
    return hashlib.sha256(text.encode("utf-8", "surrogatepass")).hexdigest()


def _settle(note: ActionNote) -> _Delivery | None:
    """Mark a note settled; return its delivery only the first time."""
    delivery = note.delivery
    if delivery is None or delivery.settled:
        return None
    delivery.settled = True
    return delivery


def commit_exposure(note: ActionNote, session_id: str) -> None:
    """Commit a delivered note: keep its reservation, record probe, ledger, exposure.

    Called only once the client emitted the note. The budget and entry uses
    were reserved at selection; a dropped note releases them in ``discard_note``.
    """
    delivery = _settle(note)
    if delivery is None:
        return
    with SessionState.open(delivery.paths.store, delivery.harness, delivery.session_id) as current:
        pending = current.data.get("pending_application")
        if not isinstance(pending, list):
            pending = []
            current.data["pending_application"] = pending
        pending.append(delivery.pending)
    _log(delivery.paths, delivery.coarse_row)
    _log(delivery.paths, delivery.hits_row)
    for store, sources in note.sources_by_store.items():
        surfacing.log_surfacing(
            store,
            sources,
            channel="action_memory",
            session_id=session_id,
        )


def _release(paths: ProjectPaths, harness: str, session_id: str, entry_paths: list[str]) -> None:
    """Give back one budget unit and one use of each entry reserved at selection."""
    with SessionState.open(paths.store, harness, session_id) as current:
        used = current.data.get("injections", 0)
        if isinstance(used, int) and used > 0:
            current.data["injections"] = used - 1
        entry_counts = current.data.get("entry_counts")
        if isinstance(entry_counts, dict):
            for entry in entry_paths:
                count = entry_counts.get(entry, 0)
                if isinstance(count, int) and count > 1:
                    entry_counts[entry] = count - 1
                else:
                    entry_counts.pop(entry, None)


def discard_note(note: ActionNote, *, reason: str | None = None) -> None:
    """Release a note the client did not emit and log it with ``injected: 0``.

    The note is marked settled before the release, so a failed state write
    keeps the reservation: one showing fewer at most, never one more.
    """
    delivery = _settle(note)
    if delivery is None:
        return
    try:
        _release(delivery.paths, delivery.harness, delivery.session_id, delivery.entry_paths)
    finally:
        row = {**delivery.coarse_row, "injected": 0}
        if reason is not None:
            row["reason"] = reason
        _log(delivery.paths, row)


def _canonical_key(event: HookEvent) -> str | None:
    if event.tool is None:
        return None
    server, name = canonical_tool(event.tool.raw_name)
    return f"{server}.{name}" if server else name


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _log(paths: ProjectPaths, row: dict) -> None:
    """Append one O_APPEND write per row without ever breaking a hook call."""
    try:
        private_mkdir(paths.store)
        data = json.dumps({"ts": _now(), **row}, ensure_ascii=False).encode("utf-8") + b"\n"
        descriptor = os.open(
            paths.store / "action-retrieval.jsonl",
            os.O_WRONLY | os.O_CREAT | os.O_APPEND,
            0o600,
        )
        try:
            # Avoid BufferedWriter splitting a large row into interleavable writes.
            remaining = memoryview(data)
            while remaining:
                written = os.write(descriptor, remaining)
                remaining = remaining[written:]
        finally:
            os.close(descriptor)
    except Exception:  # noqa: BLE001 - telemetry is never session-critical
        pass


def _coarse_row(event: HookEvent, tool_key: str, injected: int) -> dict:
    output = event.tool.output if event.tool is not None else ""
    failed = re.search(
        r"\b(error|failed|traceback|exception)\b",
        (output or "")[:400],
        re.I,
    )
    outcome = "error?" if failed else "ok"
    return {
        "sessionID": event.session_id,
        "tool": tool_key,
        "callID": event.tool.call_id if event.tool is not None else None,
        "outcome": outcome,
        "injected": injected,
    }


def _log_skip(
    event: HookEvent,
    paths: ProjectPaths,
    ledger_tool: str,
    reason: str,
) -> None:
    should_log = False
    with SessionState.open(paths.store, event.harness, event.session_id) as current:
        seen = current.data.setdefault("skips_logged", [])
        if not isinstance(seen, list):
            seen = []
            current.data["skips_logged"] = seen
        if reason not in seen:
            seen.append(reason)
            should_log = True
    if should_log:
        _log(
            paths,
            {
                "sessionID": event.session_id,
                "tool": ledger_tool,
                "callID": event.tool.call_id if event.tool is not None else None,
                "skipped": reason,
            },
        )


def _search(query_text: str, project_dir: str) -> list[dict]:
    if os.environ.get("MAGNOLIA_HOOK_TEST") == "1":
        delay = os.environ.get("MAGNOLIA_HOOK_TEST_SEARCH_SLEEP")
        if delay:
            time.sleep(float(delay))
    return quick_search.search(query_text, project_dir, k=MAX_HITS)


def _search_before_deadline(
    query_text: str,
    project_dir: str,
    deadline: float,
) -> tuple[str, list[dict] | None, int]:
    results: queue.Queue[tuple[str, list[dict] | None, int]] = queue.Queue(maxsize=1)

    def run() -> None:
        started = time.monotonic()
        try:
            hits = _search(query_text, project_dir)
        except Exception:  # noqa: BLE001 - retrieval failures become no note
            results.put(("error", None, round((time.monotonic() - started) * 1000)))
        else:
            results.put(("ok", hits, round((time.monotonic() - started) * 1000)))

    threading.Thread(target=run, daemon=True).start()
    wait = max(0.0, min(2.5, deadline - time.monotonic()))
    try:
        return results.get(timeout=wait)
    except queue.Empty:
        return "timeout", None, round(wait * 1000)


def _normal_hits(hits: list[dict]) -> list[dict]:
    return [
        hit
        for hit in hits
        if isinstance(hit, dict)
        and isinstance(hit.get("path"), str)
        and hit.get("path")
        and (hit.get("score") or 0) >= MIN_SCORE
    ]


def _block(tool_name: str, hits: list[dict]) -> str:
    def bullet(hit: dict) -> str:
        badge = "/".join(str(value) for value in (hit.get("tier"), hit.get("type")) if value)
        provisional = " [unconfirmed]" if hit.get("provisional") else ""
        gist = str(hit.get("gist") or "")[:200]
        return f"- [{badge}{provisional}] {hit.get('title', '')} — {gist}\n  {hit['path']}"

    return (
        f"\n[Magnolia · action-memory] {len(hits)} past learning(s) matched this "
        f"action (tool: {tool_name}). Scan these BEFORE trusting defaults — "
        "one may hold the exact pitfall or parameter for this step:\n"
        + "\n".join(bullet(hit) for hit in hits)
        + "\n(read-only auto-injection; opt out MAGNOLIA_ACTION_RETRIEVE=0)\n\n"
    )


def _sources(hits: list[dict]) -> dict[Path, list[dict]]:
    grouped: dict[Path, list[dict]] = {}
    for hit in hits:
        path = Path(hit["path"])
        store = path.parent.parent
        tier = "staging" if path.parent.name == "staging" else "project"
        grouped.setdefault(store, []).append({"id": path.name, "tier": tier})
    return grouped


def on_post_tool(
    event: HookEvent,
    paths: ProjectPaths,
    deadline: float,
) -> ActionNote | None:
    """Retrieve a note, opening state only for short read and commit sections.

    The caller must not hold this session's state while calling this function.
    """
    if paths is None or event.tool is None or event.tool.output is None:
        return None
    if str(os.environ.get("MAGNOLIA_ACTION_RETRIEVE", "")).lower() in _DISABLED:
        return None
    tool_key = _canonical_key(event)
    if tool_key not in ACTION_TOOLS:
        return None
    ledger_tool = event.tool.raw_name

    query_text = f"{event.tool.name} {args_to_query(event.tool.input)}"
    query_key = _cache_key(event.tool.name, event.tool.input)
    now = time.time()
    cached_hits: list[dict] | None = None
    with SessionState.open(paths.store, event.harness, event.session_id) as current:
        cache = current.data.get("query_cache", {})
        if isinstance(cache, dict):
            cached = cache.get(query_key)
            if (
                isinstance(cached, dict)
                and isinstance(cached.get("ts"), (int, float))
                and now - cached["ts"] < CACHE_TTL_SECONDS
                and isinstance(cached.get("hits"), list)
            ):
                cached_hits = cached["hits"]

    searched = cached_hits is None
    if searched:
        status, hits, elapsed_ms = _search_before_deadline(query_text, str(paths.project_dir), deadline)
        if status == "timeout":
            _log_skip(event, paths, ledger_tool, "timeout")
            _log(paths, _coarse_row(event, ledger_tool, 0))
            return None
        if status == "error" or hits is None:
            _log(paths, _coarse_row(event, ledger_tool, 0))
            return None
    else:
        hits = cached_hits
        elapsed_ms = 0

    selected: list[dict] = []
    skip_reason: str | None = None
    with SessionState.open(paths.store, event.harness, event.session_id) as current:
        if searched:
            cache = current.data.setdefault("query_cache", {})
            if not isinstance(cache, dict):
                cache = {}
                current.data["query_cache"] = cache
            cache.pop(query_key, None)
            cache[query_key] = {"ts": time.time(), "hits": hits}
            while len(cache) > MAX_CACHE_ENTRIES:
                cache.pop(next(iter(cache)))

        normal = _normal_hits(hits)
        entry_counts = current.data.setdefault("entry_counts", {})
        if not isinstance(entry_counts, dict):
            entry_counts = {}
            current.data["entry_counts"] = entry_counts
        fresh = [
            hit
            for hit in normal
            if entry_counts.get(hit["path"], 0) < MAX_PER_ENTRY_PER_SESSION
        ]
        used = current.data.get("injections", 0)
        used = used if isinstance(used, int) else 0
        if not hits:
            pass
        elif not fresh:
            skip_reason = "latch"
        elif used >= MAX_INJECTIONS_PER_SESSION:
            skip_reason = "budget"
        else:
            selected = fresh[:MAX_HITS]
            current.data["injections"] = used + 1
            for hit in selected:
                entry_counts[hit["path"]] = entry_counts.get(hit["path"], 0) + 1

    if skip_reason is not None:
        _log_skip(event, paths, ledger_tool, skip_reason)
    if not selected:
        _log(paths, _coarse_row(event, ledger_tool, 0))
        return None

    try:
        delivery = _Delivery(
            paths=paths,
            harness=event.harness,
            session_id=event.session_id,
            entry_paths=[hit["path"] for hit in selected],
            pending={
                "callID": str(event.tool.call_id or ""),
                "tool": ledger_tool,
                "turn_id": event.turn_id,
                "titles": [str(hit.get("title") or "") for hit in selected],
                "paths": [str(hit.get("path") or "") for hit in selected],
            },
            coarse_row=_coarse_row(event, ledger_tool, len(selected)),
            hits_row={
                "sessionID": event.session_id,
                "tool": ledger_tool,
                "callID": event.tool.call_id,
                "ms": elapsed_ms,
                "hits": len(selected),
                "titles": [hit.get("title") for hit in selected][:4],
                "entries": [
                    {
                        "path": hit["path"],
                        "tier": hit.get("tier"),
                        "score": hit.get("score"),
                        "confidence": hit.get("confidence"),
                        "provisional": bool(hit.get("provisional")),
                    }
                    for hit in selected
                ],
            },
        )
        return ActionNote(_block(event.tool.name, selected), _sources(selected), delivery)
    except Exception:
        _release(paths, event.harness, event.session_id, [hit["path"] for hit in selected])
        raise


def _distinctive_tokens(titles: list[str], paths: list[str]) -> list[str]:
    raw = re.sub(r"[^a-z]+", " ", " ".join((*titles, *paths)).lower())
    return list(
        dict.fromkeys(
            word for word in raw.split() if len(word) >= 5 and word not in _STOP
        )
    )


def _pending_for_turn(items: list[dict], turn_id: str | None) -> tuple[list[dict], list[dict]]:
    """Partition pending probes while retaining legacy items for compatibility."""
    has_turns = any(isinstance(item, dict) and item.get("turn_id") is not None for item in items)
    if turn_id is None or not has_turns:
        return items, []
    selected: list[dict] = []
    remaining: list[dict] = []
    for item in items:
        target = item.get("turn_id") if isinstance(item, dict) else None
        (selected if target in {None, turn_id} else remaining).append(item)
    return selected, remaining


def probe_application(
    paths: ProjectPaths,
    harness: str,
    session_id: str,
    reply_text: str,
    turn_id: str | None = None,
) -> None:
    """Probe pending notes in one state transaction, removing each after its row.

    An item leaves ``pending_application`` only once its ``application`` row is
    logged, so a crash cannot lose a probe; a hard crash before the state is
    saved can log an item twice on the next probe. Malformed items are dropped.
    Holding the state lock while logging keeps a concurrent probe of the same
    session from logging the same items. The caller must not hold this
    session's state while calling this function.
    """
    with SessionState.open(paths.store, harness, session_id) as state:
        pending = state.data.get("pending_application", [])
        if not isinstance(pending, list) or not pending:
            return
        selected, _ = _pending_for_turn(pending, turn_id)
        for item in probe_items(paths, harness, session_id, selected, reply_text, turn_id):
            pending.remove(item)


def probe_items(
    paths: ProjectPaths,
    harness: str,
    session_id: str,
    items: list[dict],
    reply_text: str,
    turn_id: str | None = None,
) -> list[dict]:
    """Record application telemetry for an explicit, already-owned item set.

    Returns the items that are settled: logged, or malformed and dropped. It
    stops at the first logging failure, leaving that item and later ones
    unsettled so the caller keeps them for the next probe.
    """
    reply = reply_text.lower()
    settled: list[dict] = []
    for item in list(items):
        try:
            tokens = _distinctive_tokens(item.get("titles", []), item.get("paths", []))
            matched = [token for token in tokens if token in reply]
            row = {
                "event": "application",
                "sessionID": session_id,
                "turnID": turn_id,
                "tool": item.get("tool"),
                "callID": item.get("callID"),
                "applied": bool(tokens) and len(matched) >= min(2, len(tokens)),
                "matched": matched,
                "distinct": len(tokens),
            }
        except Exception:  # noqa: BLE001 - malformed pending telemetry is dropped
            settled.append(item)
            continue
        try:
            _log(paths, row)
        except Exception:  # noqa: BLE001 - keep this and later items for the next probe
            break
        settled.append(item)
    return settled
