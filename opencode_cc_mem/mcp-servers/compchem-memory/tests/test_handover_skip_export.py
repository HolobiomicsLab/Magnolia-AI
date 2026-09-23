"""Skip-before-export for generate_handover.

2026-09-22 boot diagnostic: generate_handover ran `opencode export` for every
mapped session on every boot (~65-70 s of the ~84 s handover step at 54
sessions). It now asks opencode once (`session list --format json`) for each
session's last-updated timestamp and skips drained sessions whose seal is not
older than that timestamp. Any doubt falls back to exporting everything.
"""

import json
from pathlib import Path

import pytest

from compchem_memory import handover as hv
from compchem_memory.storage import ensure_project_store

# conftest's autouse _isolate_session_list replaces hv._list_session_updates
# with a None stub so no test shells out. Capture the real function at import
# time (before fixtures run) for the subprocess-wrapper tests.
_REAL_LIST_SESSION_UPDATES = hv._list_session_updates


@pytest.fixture
def store(tmp_path):
    ensure_project_store(str(tmp_path))
    return Path(tmp_path) / ".magnolia"


def _write_mapping(store, ids):
    p = store / "opencode-sessions.jsonl"
    p.write_text("".join(json.dumps({"opencode_session_id": i, "ts": str(n)}) + "\n"
                         for n, i in enumerate(ids)))


def _msg(mid, role, text):
    return {"info": {"id": mid, "role": role}, "parts": [{"type": "text", "text": text}]}


def _export(*texts):
    return {"info": {"id": "x"},
            "messages": [_msg(f"m{i + 1}", "user", t) for i, t in enumerate(texts)]}


_NEW_SEP = "\n\n=== NEW SESSION TRANSCRIPT (since last handover) ===\n"
_BASE_HDR = "=== CURRENT HANDOVER (base to update) ===\n"


def _merge_echo(system, user, max_tokens=2000, **kw):
    base_part, new_part = user.split(_NEW_SEP, 1)
    base_part = base_part[len(_BASE_HDR):].strip()
    if base_part.startswith("(none yet"):
        base_part = ""
    parts = [base_part] if base_part else []
    parts.append(new_part.strip())
    return "## Done\n" + "\n".join(parts)


def _record(store, sid):
    p = store / hv.HANDOVER_CURSORS_DIR / f"{sid}.json"
    return json.loads(p.read_text()) if p.exists() else None


def _seal(store, sid, cursor="m1", sealed_at_ms=1000):
    """Hand-craft a drained, sealed cursor record (as an earlier boot would)."""
    d = store / hv.HANDOVER_CURSORS_DIR
    d.mkdir(parents=True, exist_ok=True)
    (d / f"{sid}.json").write_text(json.dumps(
        {"cursor": cursor, "updated": "2026-09-22T00:00:00+00:00",
         "sealed_at_ms": sealed_at_ms}) + "\n")


def _counting_exporter(exports):
    calls = []

    def exporter(sid):
        calls.append(sid)
        return exports.get(sid)

    return exporter, calls


def _seal_via_boot(store, exports=None):
    """Run one boot that merges two sessions, sealing the non-newest one."""
    _write_mapping(store, ["ses_a", "ses_b"])
    exports = exports or {"ses_a": _export("old work"), "ses_b": _export("live work")}
    exporter, _ = _counting_exporter(exports)
    hv.generate_handover(str(store.parent), exporter=exporter, llm=_merge_echo,
                         session_updates=lambda: {"ses_a": 1, "ses_b": 1})
    return exports


# ---- _parse_session_list_json ------------------------------------------------

def test_parse_session_list_json_list():
    text = json.dumps([{"id": "ses_a", "updated": 1000},
                       {"id": "ses_b", "updated": 2000}])
    assert hv._parse_session_list_json(text) == {"ses_a": 1000, "ses_b": 2000}


def test_parse_session_list_json_dict_envelope():
    text = json.dumps({"sessions": [{"id": "ses_a", "updated": 5}]})
    assert hv._parse_session_list_json(text) == {"ses_a": 5}


def test_parse_session_list_json_float_updated_truncates_to_int():
    text = json.dumps([{"id": "ses_a", "updated": 5.9}])
    assert hv._parse_session_list_json(text) == {"ses_a": 5}


@pytest.mark.parametrize("text", [
    "not json",
    "",
    "{}",                                           # dict without sessions
    json.dumps({"sessions": "nope"}),
    json.dumps([]),                                 # empty listing
    json.dumps([{"id": "ses_a"}]),                  # no updated
    json.dumps([{"updated": 1}]),                   # no id
    json.dumps([{"id": 3, "updated": 1}]),          # id not a string
    json.dumps([{"id": "ses_a", "updated": None}]),
    json.dumps([{"id": "ses_a", "updated": True}]),  # bool is not a timestamp
])
def test_parse_session_list_json_rejects_bad_payloads(text):
    assert hv._parse_session_list_json(text) is None


# ---- _list_session_updates (subprocess wrapper) ------------------------------

class _Proc:
    def __init__(self, returncode=0):
        self.returncode = returncode


def test_list_session_updates_runs_cli_and_parses(monkeypatch):
    seen = {}

    def fake_run(cmd, stdout=None, stderr=None, timeout=None):
        seen["cmd"] = cmd
        seen["timeout"] = timeout
        stdout.write(json.dumps([{"id": "ses_a", "updated": 42}]))
        return _Proc(0)

    monkeypatch.setattr(hv.subprocess, "run", fake_run)
    assert _REAL_LIST_SESSION_UPDATES() == {"ses_a": 42}
    assert seen["cmd"][:3] == ["opencode", "session", "list"]
    assert "--format" in seen["cmd"] and "json" in seen["cmd"]
    assert "-n" in seen["cmd"]
    assert str(hv.HANDOVER_SESSION_LIST_LIMIT) in seen["cmd"]
    assert seen["timeout"] == hv.HANDOVER_SESSION_LIST_TIMEOUT


def test_list_session_updates_nonzero_returncode_returns_none(monkeypatch):
    monkeypatch.setattr(hv.subprocess, "run",
                        lambda *a, **k: _Proc(3))
    assert _REAL_LIST_SESSION_UPDATES() is None


def test_list_session_updates_garbage_output_returns_none(monkeypatch):
    def fake_run(cmd, stdout=None, stderr=None, timeout=None):
        stdout.write("not json")
        return _Proc(0)

    monkeypatch.setattr(hv.subprocess, "run", fake_run)
    assert _REAL_LIST_SESSION_UPDATES() is None


def test_list_session_updates_exception_returns_none(monkeypatch):
    def boom(*a, **k):
        raise OSError("opencode not installed")

    monkeypatch.setattr(hv.subprocess, "run", boom)
    assert _REAL_LIST_SESSION_UPDATES() is None


# ---- _should_skip_export truth table -----------------------------------------

def _rec(cursor="m1", sealed=1000):
    record = {"cursor": cursor, "updated": "2026-09-22T00:00:00+00:00"}
    if sealed is not None:
        record["sealed_at_ms"] = sealed
    return record


def test_should_skip_true_when_sealed_and_unchanged():
    assert hv._should_skip_export(
        "ses_a", _rec(), {"ses_a": 1000, "ses_b": 5}, "ses_b") is True


@pytest.mark.parametrize("sid,record,updates,latest", [
    ("ses_b", _rec(), {"ses_a": 1, "ses_b": 1}, "ses_b"),    # newest never skipped
    ("ses_a", None, {"ses_a": 1, "ses_b": 1}, "ses_b"),      # no record
    ("ses_a", _rec(cursor=None), {"ses_a": 1, "ses_b": 1}, "ses_b"),  # never merged
    ("ses_a", _rec(sealed=None), {"ses_a": 1, "ses_b": 1}, "ses_b"),  # legacy, no seal
    ("ses_a", _rec(sealed=True), {"ses_a": 1, "ses_b": 1}, "ses_b"),  # bool != seal
    ("ses_a", _rec(), {"ses_b": 1}, "ses_b"),                # sid missing from listing
    ("ses_a", _rec(), None, "ses_b"),                        # listing unavailable
    ("ses_a", _rec(), {"ses_a": 1001, "ses_b": 1}, "ses_b"),  # resumed: newer updated
    ("ses_a", _rec(), {}, "ses_b"),                          # empty listing
])
def test_should_skip_export_false_cases(sid, record, updates, latest):
    assert hv._should_skip_export(sid, record, updates, latest) is False


# ---- sealing lifecycle -------------------------------------------------------

def test_first_boot_seals_drained_non_newest_session_only(store):
    _seal_via_boot(store)
    rec_a, rec_b = _record(store, "ses_a"), _record(store, "ses_b")
    assert rec_a["cursor"] == "m1" and isinstance(rec_a["sealed_at_ms"], int)
    assert rec_b["cursor"] == "m1" and "sealed_at_ms" not in rec_b


def test_second_boot_skips_sealed_export(store):
    exports = _seal_via_boot(store)
    seal = _record(store, "ses_a")["sealed_at_ms"]
    exporter, calls = _counting_exporter(exports)
    llm_calls = []

    def llm(system, user, **kw):
        llm_calls.append(user)
        return _merge_echo(system, user, **kw)

    result = hv.generate_handover(
        str(store.parent), exporter=exporter, llm=llm,
        session_updates=lambda: {"ses_a": seal, "ses_b": 1})
    assert calls == ["ses_b"]            # ses_a skipped, newest still exported
    assert llm_calls == []               # nothing new anywhere
    assert result is None


def test_resumed_session_is_re_exported_and_merged(store):
    exports = _seal_via_boot(store)
    old_seal = _record(store, "ses_a")["sealed_at_ms"]
    exports["ses_a"] = _export("old work", "resumed follow-up")
    exporter, calls = _counting_exporter(exports)
    hv.generate_handover(
        str(store.parent), exporter=exporter, llm=_merge_echo,
        session_updates=lambda: {"ses_a": old_seal + 1, "ses_b": 1})
    assert "ses_a" in calls
    rec = _record(store, "ses_a")
    assert rec["cursor"] == "m2"                     # resumed message merged
    assert rec["sealed_at_ms"] >= old_seal           # re-sealed for the new state


def test_newest_session_is_never_skipped_even_if_sealed(store):
    _write_mapping(store, ["ses_a"])
    _seal(store, "ses_a", sealed_at_ms=10 ** 15)
    exporter, calls = _counting_exporter({"ses_a": _export("live")})
    hv.generate_handover(str(store.parent), exporter=exporter, llm=_merge_echo,
                         session_updates=lambda: {"ses_a": 1})
    assert calls == ["ses_a"]


def test_legacy_record_without_seal_is_exported_then_sealed(store):
    _write_mapping(store, ["ses_a", "ses_b"])
    d = store / hv.HANDOVER_CURSORS_DIR
    d.mkdir(parents=True, exist_ok=True)
    (d / "ses_a.json").write_text(json.dumps({"cursor": "m1", "updated": "old"}))
    exports = {"ses_a": _export("old work"), "ses_b": _export("live")}
    exporter, calls = _counting_exporter(exports)

    hv.generate_handover(str(store.parent), exporter=exporter, llm=_merge_echo,
                         session_updates=lambda: {"ses_a": 1, "ses_b": 1})
    assert calls == ["ses_a", "ses_b"]               # legacy record has no seal yet
    seal = _record(store, "ses_a")["sealed_at_ms"]
    assert isinstance(seal, int)

    exporter2, calls2 = _counting_exporter(exports)
    hv.generate_handover(str(store.parent), exporter=exporter2, llm=_merge_echo,
                         session_updates=lambda: {"ses_a": seal, "ses_b": 1})
    assert calls2 == ["ses_b"]                       # now sealed and skipped


def test_drained_but_unsealed_record_gets_sealed(store):
    _write_mapping(store, ["ses_a", "ses_b"])
    d = store / hv.HANDOVER_CURSORS_DIR
    d.mkdir(parents=True, exist_ok=True)
    (d / "ses_a.json").write_text(json.dumps({"cursor": "m1", "updated": "old"}))
    exports = {"ses_a": _export("old work"), "ses_b": _export("live")}
    exporter, _ = _counting_exporter(exports)
    hv.generate_handover(str(store.parent), exporter=exporter, llm=_merge_echo,
                         session_updates=lambda: {"ses_a": 1, "ses_b": 1})
    assert isinstance(_record(store, "ses_a")["sealed_at_ms"], int)


def test_empty_transcript_session_is_sealed(store):
    _write_mapping(store, ["ses_a", "ses_b"])
    empty = {"info": {"id": "ses_a"},
             "messages": [{"info": {"id": "m1", "role": "assistant"}, "parts": []}]}
    exports = {"ses_a": empty, "ses_b": _export("live")}
    exporter, _ = _counting_exporter(exports)
    hv.generate_handover(str(store.parent), exporter=exporter, llm=_merge_echo,
                         session_updates=lambda: {"ses_a": 1, "ses_b": 1})
    assert isinstance(_record(store, "ses_a")["sealed_at_ms"], int)


# ---- fallbacks: never lose a session -----------------------------------------

def test_listing_unavailable_exports_everything(store):
    _write_mapping(store, ["ses_a", "ses_b"])
    _seal(store, "ses_a", sealed_at_ms=10 ** 15)     # would skip if listing usable
    exports = {"ses_a": _export("old"), "ses_b": _export("live")}
    exporter, calls = _counting_exporter(exports)
    hv.generate_handover(str(store.parent), exporter=exporter, llm=_merge_echo,
                         session_updates=lambda: None)
    assert calls == ["ses_a", "ses_b"]


def test_listing_missing_newest_sid_is_untrusted(store):
    exports = _seal_via_boot(store)
    seal = _record(store, "ses_a")["sealed_at_ms"]
    exporter, calls = _counting_exporter(exports)
    hv.generate_handover(
        str(store.parent), exporter=exporter, llm=_merge_echo,
        session_updates=lambda: {"ses_a": seal})     # newest sid absent
    assert calls == ["ses_a", "ses_b"]               # listing distrusted entirely


def test_sid_missing_from_listing_is_exported(store):
    exports = _seal_via_boot(store)
    exporter, calls = _counting_exporter(exports)
    hv.generate_handover(
        str(store.parent), exporter=exporter, llm=_merge_echo,
        session_updates=lambda: {"ses_b": 1})        # newest present, ses_a absent
    assert "ses_a" in calls                          # unknown freshness -> export


def test_export_failure_does_not_seal_or_touch_record(store):
    _write_mapping(store, ["ses_a", "ses_b"])
    d = store / hv.HANDOVER_CURSORS_DIR
    d.mkdir(parents=True, exist_ok=True)
    rec_path = d / "ses_a.json"
    rec_path.write_text(json.dumps({"cursor": "m1", "updated": "old"}))
    before = rec_path.read_text()
    hv.generate_handover(str(store.parent), exporter=lambda s: None, llm=_merge_echo,
                         session_updates=lambda: {"ses_a": 1, "ses_b": 1})
    assert rec_path.read_text() == before
    assert _record(store, "ses_b") is None


def test_llm_failure_does_not_seal_or_write_state(store):
    _write_mapping(store, ["ses_a", "ses_b"])
    exports = {"ses_a": _export("work"), "ses_b": _export("live")}
    exporter, _ = _counting_exporter(exports)
    result = hv.generate_handover(str(store.parent), exporter=exporter,
                                  llm=lambda *a, **k: None,
                                  session_updates=lambda: {"ses_a": 1, "ses_b": 1})
    assert result is None
    assert _record(store, "ses_a") is None
    assert not (store / hv.HANDOVER_STATE_FILE).exists()


# ---- kill switch / default wiring --------------------------------------------

def test_kill_switch_disables_skip_and_probe(store, monkeypatch):
    _seal_via_boot(store)
    monkeypatch.setenv("MAGNOLIA_HANDOVER_SKIP_EXPORT", "0")
    probe_calls = []

    def probe():
        probe_calls.append(1)
        return {"ses_a": 10 ** 15, "ses_b": 1}

    monkeypatch.setattr(hv, "_list_session_updates", probe)
    exports = {"ses_a": _export("old work"), "ses_b": _export("live")}
    exporter, calls = _counting_exporter(exports)
    hv.generate_handover(str(store.parent), exporter=exporter, llm=_merge_echo)
    assert probe_calls == []                         # probe never invoked
    assert calls == ["ses_a", "ses_b"]               # legacy blind export


def test_skip_uses_live_probe_by_default(store, monkeypatch):
    _seal_via_boot(store)
    seal = _record(store, "ses_a")["sealed_at_ms"]
    monkeypatch.setattr(hv, "_list_session_updates",
                        lambda: {"ses_a": seal, "ses_b": 1})
    exports = {"ses_a": _export("old work"), "ses_b": _export("live")}
    exporter, calls = _counting_exporter(exports)
    hv.generate_handover(str(store.parent), exporter=exporter, llm=_merge_echo)
    assert calls == ["ses_b"]


# ---- ordering + telemetry ----------------------------------------------------

def test_merge_order_preserved_when_some_sessions_skipped(store):
    _write_mapping(store, ["ses_a", "ses_b", "ses_c"])
    _seal(store, "ses_a", sealed_at_ms=1000)
    exports = {"ses_a": _export("old"), "ses_b": _export("b work"),
               "ses_c": _export("c live")}
    exporter, calls = _counting_exporter(exports)
    llm_calls = []

    def llm(system, user, **kw):
        llm_calls.append(user)
        return _merge_echo(system, user, **kw)

    hv.generate_handover(str(store.parent), exporter=exporter, llm=llm,
                         session_updates=lambda: {"ses_a": 1000, "ses_b": 1,
                                                  "ses_c": 1})
    assert calls == ["ses_b", "ses_c"]
    assert len(llm_calls) == 2
    assert "b work" in llm_calls[0] and "c live" in llm_calls[1]


def test_skip_summary_printed_to_stderr(store, capsys):
    _seal_via_boot(store)
    capsys.readouterr()                              # drop first-boot output
    seal = _record(store, "ses_a")["sealed_at_ms"]
    exports = {"ses_a": _export("old work"), "ses_b": _export("live")}
    exporter, _ = _counting_exporter(exports)
    hv.generate_handover(str(store.parent), exporter=exporter, llm=_merge_echo,
                         session_updates=lambda: {"ses_a": seal, "ses_b": 1})
    err = capsys.readouterr().err
    assert "skip-export: 1/2" in err


def test_no_skip_summary_when_nothing_skipped(store, capsys):
    _write_mapping(store, ["ses_a", "ses_b"])
    exports = {"ses_a": _export("a work"), "ses_b": _export("b work")}
    exporter, _ = _counting_exporter(exports)
    hv.generate_handover(str(store.parent), exporter=exporter, llm=_merge_echo,
                         session_updates=lambda: {"ses_a": 1, "ses_b": 1})
    assert "skip-export:" not in capsys.readouterr().err
