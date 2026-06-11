"""Incremental, cursor-based dialogue ingest.

The dialogue path must distill a live session repeatedly as it grows — only the
NEW messages each sweep — instead of once-and-done. The active session (latest in
the mapping) is never sealed; an older/superseded session is sealed (done) and
then skipped.
"""

import json
from pathlib import Path

import pytest

from compchem_memory import opencode_ingest as oi
from compchem_memory.storage import ensure_project_store


@pytest.fixture
def store(tmp_path):
    ensure_project_store(str(tmp_path))
    return Path(tmp_path) / ".magnolia"


def _write_mapping(store, ids):
    p = store / "opencode-sessions.jsonl"
    p.write_text("".join(json.dumps({"opencode_session_id": i, "ts": str(n)}) + "\n"
                         for n, i in enumerate(ids)))
    return p


def _msg(mid, role, text):
    return {"info": {"id": mid, "role": role}, "parts": [{"type": "text", "text": text}]}


def test_second_sweep_distills_only_new_messages(store):
    _write_mapping(store, ["ses_live"])
    msgs = [_msg("m1", "user", "first")]
    seen = []

    def exporter(sid):
        return {"info": {"id": sid}, "messages": list(msgs)}

    def distiller(t):
        seen.append(t)
        return [{"title": "f", "content": t, "type": "note"}]

    oi.ingest_opencode_sessions(str(store), exporter=exporter, distiller=distiller)
    assert "first" in seen[-1]

    msgs.append(_msg("m2", "assistant", "second"))
    oi.ingest_opencode_sessions(str(store), exporter=exporter, distiller=distiller)

    assert "second" in seen[-1]
    assert "first" not in seen[-1]   # only the NEW slice was distilled


def test_active_session_record_not_done(store):
    _write_mapping(store, ["ses_live"])

    oi.ingest_opencode_sessions(
        str(store),
        exporter=lambda s: {"info": {"id": s}, "messages": [_msg("m1", "user", "hi there")]},
        distiller=lambda t: [{"title": "f", "content": t}],
    )
    rec = json.loads((store / "opencode-distilled" / "ses_live.json").read_text())
    assert rec["done"] is False
    assert rec["cursor"] == "m1"


def test_superseded_session_is_sealed_then_skipped(store):
    msgs_live = [_msg("m1", "user", "hi there")]

    def exporter(sid):
        if sid == "ses_live":
            return {"info": {"id": sid}, "messages": msgs_live}
        return {"info": {"id": sid}, "messages": [_msg("n1", "user", "new work")]}

    distiller = lambda t: [{"title": "f", "content": t}]

    # ses_live active first
    _write_mapping(store, ["ses_live"])
    oi.ingest_opencode_sessions(str(store), exporter=exporter, distiller=distiller)

    # a newer session supersedes it -> ses_live is now closed
    _write_mapping(store, ["ses_live", "ses_new"])
    oi.ingest_opencode_sessions(str(store), exporter=exporter, distiller=distiller)
    rec = json.loads((store / "opencode-distilled" / "ses_live.json").read_text())
    assert rec["done"] is True

    # sealed -> not even exported on the next sweep
    calls = []

    def counting_exporter(sid):
        calls.append(sid)
        return exporter(sid)

    oi.ingest_opencode_sessions(str(store), exporter=counting_exporter, distiller=distiller)
    assert "ses_live" not in calls
    assert "ses_new" in calls
