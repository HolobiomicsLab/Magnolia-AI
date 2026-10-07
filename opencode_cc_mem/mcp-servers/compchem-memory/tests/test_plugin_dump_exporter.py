# tests/test_plugin_dump_exporter.py
# In-process transcript capture (2026-10-07): under opencode v2 the export CLI
# is user-text-only, so the session-capture plugin dumps the full message list
# via ctx.session.context() to <store>/opencode-transcripts/<sid>.json. The
# default exporter prefers that dump; the CLI (v1 form first, then the v2
# `session export` fallback) serves sessions without a dump.
import json
from pathlib import Path

from compchem_memory.opencode_ingest import (
    export_session,
    ingest_opencode_sessions,
    plugin_dump_exporter,
)


def _store(tmp_path):
    store = tmp_path / ".magnolia"
    store.mkdir()
    return store


def test_dump_preferred_over_cli(tmp_path, monkeypatch):
    store = _store(tmp_path)
    d = store / "opencode-transcripts"
    d.mkdir()
    (d / "ses_a.json").write_text(json.dumps({
        "ts": "t", "sessionID": "ses_a", "source": "plugin-v2-ctx.session.context",
        "messages": [{"info": {"role": "user", "id": "m1"}, "parts": [{"type": "text", "text": "hi"}]}],
    }))
    called = {"cli": 0}

    def fake_cli(sid):
        called["cli"] += 1
        return {"messages": []}

    monkeypatch.setattr("compchem_memory.opencode_ingest.export_session", fake_cli)
    out = plugin_dump_exporter(str(store))("ses_a")
    assert out["sessionID"] == "ses_a"
    assert called["cli"] == 0                      # dump answered, CLI untouched


def test_corrupt_dump_falls_back_to_cli(tmp_path, monkeypatch):
    store = _store(tmp_path)
    d = store / "opencode-transcripts"
    d.mkdir()
    (d / "ses_b.json").write_text("{not json")
    monkeypatch.setattr("compchem_memory.opencode_ingest.export_session",
                        lambda sid: {"messages": [{"info": {"role": "user", "id": "m1"},
                                                   "parts": [{"type": "text", "text": "cli"}]}]})
    out = plugin_dump_exporter(str(store))("ses_b")
    assert out["messages"][0]["parts"][0]["text"] == "cli"


def test_no_dump_uses_cli(tmp_path, monkeypatch):
    store = _store(tmp_path)
    monkeypatch.setattr("compchem_memory.opencode_ingest.export_session",
                        lambda sid: {"messages": []})
    assert plugin_dump_exporter(str(store))("ses_c") == {"messages": []}


def test_export_session_tries_v2_form_after_v1(tmp_path, monkeypatch):
    import subprocess as sp

    calls: list[list[str]] = []

    class FakeProc:
        returncode = 1

    def fake_run(cmd, stdout=None, stderr=None, timeout=None):
        calls.append(cmd)
        if cmd[1] == "export":
            return FakeProc()                      # v1 form fails
        Path(stdout.name).write_text('{"messages": [{"info": {"role": "user"}, "parts": []}]}')
        class P: returncode = 0
        return P()

    monkeypatch.setattr(sp, "run", fake_run)
    out = export_session("ses_d")
    assert out is not None and out["messages"][0]["info"]["role"] == "user"
    assert calls[0][:2] == ["opencode", "export"]
    assert calls[1][:3] == ["opencode", "session", "export"]


def test_ingest_end_to_end_from_dump(tmp_path, monkeypatch):
    store = _store(tmp_path)
    (store / "opencode-sessions.jsonl").write_text(
        json.dumps({"opencode_session_id": "ses_v2"}) + "\n")
    d = store / "opencode-transcripts"
    d.mkdir()
    (d / "ses_v2.json").write_text(json.dumps({
        "ts": "t", "sessionID": "ses_v2", "source": "plugin-v2-ctx.session.context",
        "messages": [
            {"info": {"role": "user", "id": "m1"},
             "parts": [{"type": "text", "text": "we docked KFERQ to 4PO2, score -55.3, 4 clusters"}]},
            {"info": {"role": "assistant", "id": "m2"},
             "parts": [{"type": "text", "text": "Docking succeeded."}]},
        ],
    }))

    def distiller(transcript):
        return [{"type": "scientific_finding", "title": "v2 dump distilled",
                 "content": transcript[:100], "tags": [], "tools": [], "confidence": 0.6}]

    saved = ingest_opencode_sessions(str(store), distiller=distiller)
    assert len(saved) == 1                          # dump fed distillation end-to-end
    assert "v2 dump distilled" in Path(saved[0]).read_text()
    # cursor advanced to the last dumped message id
    rec = json.loads((store / "opencode-distilled" / "ses_v2.json").read_text())
    assert rec["cursor"] == "m2"


def test_v2_db_exporter_full_content(tmp_path, monkeypatch):
    """The v2 content source: session_message rows carry assistant content[]
    (reasoning/text/tool parts) — unlike exports and ctx.session.context()."""
    import sqlite3

    db = tmp_path / "v2.db"
    con = sqlite3.connect(str(db))
    con.execute("CREATE TABLE session_message (id TEXT, session_id TEXT, type TEXT, seq INT, time_created INT, time_updated INT, data TEXT)")
    con.executemany("INSERT INTO session_message VALUES (?,?,?,?,?,?,?)", [
        ("m1", "ses_x", "user", 1, 1, 1,
         json.dumps({"text": "dock KFERQ to 4PO2", "time": {}})),
        ("m2", "ses_x", "assistant", 2, 2, 2, json.dumps({
            "content": [
                {"type": "reasoning", "text": "I will run the docking."},
                {"type": "tool", "tool": "compchem-tools_gnina_dock",
                 "output": "score -7.2", "input": {"ligand": "x"}},
            ], "model": {}, "cost": 1})),
        ("m3", "ses_x", "assistant", 3, 3, 3, json.dumps({
            "content": [{"type": "text", "text": "Docking done, score -7.2"}]})),
    ])
    con.commit(); con.close()

    from compchem_memory.opencode_ingest import reconstruct_transcript, v2_db_exporter
    out = v2_db_exporter(str(db))("ses_x")
    assert out and len(out["messages"]) == 3
    t = reconstruct_transcript(out, tool_chars=4000)
    assert "USER: dock KFERQ" in t
    assert "ASSISTANT (reasoning): I will run the docking." in t
    assert "(tool:compchem-tools_gnina_dock): score -7.2" in t
    assert "Docking done, score -7.2" in t
    # flat tool parts normalized to the state family reconstruct reads
    tool_part = out["messages"][1]["parts"][1]
    assert tool_part["state"]["output"] == "score -7.2"


def test_chain_prefers_v2_db_over_dump_and_cli(tmp_path, monkeypatch):
    monkeypatch.setenv("OPENCODE_DB", str(tmp_path / "v2.db"))
    import sqlite3
    db = tmp_path / "v2.db"
    con = sqlite3.connect(str(db))
    con.execute("CREATE TABLE session_message (id TEXT, session_id TEXT, type TEXT, seq INT, time_created INT, time_updated INT, data TEXT)")
    con.execute("INSERT INTO session_message VALUES ('m1','ses_y','user',1,1,1,?)",
                (json.dumps({"text": "from db"}),))
    con.commit(); con.close()
    store = tmp_path / ".magnolia"; store.mkdir()
    d = store / "opencode-transcripts"; d.mkdir()
    (d / "ses_y.json").write_text(json.dumps({"messages": [{"type": "user", "text": "from dump"}]}))
    out = plugin_dump_exporter(str(store))("ses_y")
    assert out["messages"][0]["parts"][0]["text"] == "from db"   # db wins
