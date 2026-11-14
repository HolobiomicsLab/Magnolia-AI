"""OpenCode 2.3.0 database shapes the memory server reads.

Observed on OpenCode 2.3.0:
- there is no `session` table; sessions are in `session_v2`, whose `model`
  column holds JSON {"id", "providerID"};
- `session_message` has rows of type `idle` (end of a turn: `outcome`
  succeeded or failed, with `error`) and `synthetic` (notices such as "The
  server restarted while you were working..."), besides user and assistant;
- an assistant tool part carries its tool name under `name` (Code Mode:
  `execute`) and `state{status,input,output}`, with no `tool` key.
"""

import json
import sqlite3

from compchem_memory.opencode_ingest import reconstruct_transcript, v2_db_exporter


def _messages_db(path, rows):
    con = sqlite3.connect(str(path))
    con.execute("CREATE TABLE session_message (id TEXT, session_id TEXT, type TEXT, "
                "seq INT, time_created INT, time_updated INT, data TEXT)")
    con.executemany("INSERT INTO session_message VALUES (?,?,?,?,?,?,?)",
                    [(mid, "ses_v2", typ, i, i, i, json.dumps(data))
                     for i, (mid, typ, data) in enumerate(rows, 1)])
    con.commit()
    con.close()


def test_idle_and_synthetic_rows_are_not_dialogue(tmp_path):
    db = tmp_path / "v2.db"
    _messages_db(db, [
        ("m1", "user", {"text": "dock the peptide"}),
        ("m2", "synthetic", {"text": "The server restarted while you were working.",
                             "metadata": {"notice": "restart"}}),
        ("m3", "idle", {"outcome": "failed", "error": {"type": "provider.no-route"}}),
        ("m4", "assistant", {"content": [{"type": "text", "text": "Docked."}]}),
        ("m5", "idle", {"outcome": "succeeded"}),
    ])
    out = v2_db_exporter(str(db))("ses_v2")
    assert [m["info"]["id"] for m in out["messages"]] == ["m1", "m4"]
    transcript = reconstruct_transcript(out)
    assert "restarted" not in transcript
    assert "SYNTHETIC" not in transcript and "IDLE" not in transcript


def test_session_of_only_idle_rows_exports_nothing(tmp_path):
    db = tmp_path / "v2.db"
    _messages_db(db, [("m1", "idle", {"outcome": "succeeded"})])
    assert v2_db_exporter(str(db))("ses_v2") is None


def test_tool_name_under_name_with_state(tmp_path):
    db = tmp_path / "v2.db"
    _messages_db(db, [
        ("m1", "assistant", {"content": [
            {"type": "tool", "name": "execute",
             "state": {"status": "completed", "input": {"code": "x"},
                       "output": "result 42"}},
        ]}),
    ])
    out = v2_db_exporter(str(db))("ses_v2")
    part = out["messages"][0]["parts"][0]
    assert part["tool"] == "execute"
    assert part["state"]["output"] == "result 42"
    assert "(tool:execute): result 42" in reconstruct_transcript(out, tool_chars=100)


# ---- newest session's main model (boot-timing stamp) --------------------------

def _session_v2_db(path, rows):
    con = sqlite3.connect(str(path))
    con.execute("CREATE TABLE session_v2 (id TEXT, model TEXT, time_updated INT)")
    con.executemany("INSERT INTO session_v2 VALUES (?,?,?)", rows)
    con.commit()
    con.close()


def test_main_model_read_from_session_v2(tmp_path):
    from compchem_memory.server import _newest_session_main_model

    db = tmp_path / "v2.db"
    _session_v2_db(db, [
        ("ses_old", json.dumps({"id": "old-model", "providerID": "p"}), 1),
        ("ses_new", json.dumps({"id": "vendor/new-model", "providerID": "openrouter"}), 2),
    ])
    assert _newest_session_main_model(str(db)) == {
        "main_model": "openrouter/vendor/new-model"}


def test_main_model_v2_default_db_is_opencode_db(tmp_path, monkeypatch):
    from compchem_memory.server import _newest_session_main_model

    db = tmp_path / "v2.db"
    _session_v2_db(db, [("ses_a", json.dumps({"id": "m", "providerID": "p"}), 1)])
    monkeypatch.setenv("MAGNOLIA_OPENCODE_V2", "1")
    monkeypatch.setenv("OPENCODE_DB", str(db))
    assert _newest_session_main_model() == {"main_model": "p/m"}


def test_main_model_v1_session_table_unchanged(tmp_path):
    from compchem_memory.server import _newest_session_main_model

    db = tmp_path / "v1.db"
    con = sqlite3.connect(str(db))
    con.execute("CREATE TABLE session (id TEXT, model TEXT, time_updated INT)")
    con.execute("INSERT INTO session VALUES ('s', ?, 1)",
                (json.dumps({"providerID": "p", "id": "m", "variant": "high"}),))
    con.commit()
    con.close()
    assert _newest_session_main_model(str(db)) == {
        "main_model": "p/m", "main_model_variant": "high"}
