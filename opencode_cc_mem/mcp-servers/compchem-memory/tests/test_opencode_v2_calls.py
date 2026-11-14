"""The memory server's OpenCode calls under v2 (observed on OpenCode 2.3.0).

- `opencode export <id>` does not exist on 2.3.0: it exits 1 with
  "Unexpected positional argument". Only `opencode session export <id>` does.
- A CLI call with neither --standalone nor --server starts a shared background
  service that outlives the call and keeps the first caller's OPENCODE_DB and
  environment. Started from inside a session, it resumed that session.

So under v2 the server reads the session database directly where it can, and
any CLI call uses the v2 binary with --standalone. v1 stays bare `opencode`.
"""

import json
import sqlite3
from pathlib import Path

from compchem_memory import handover as hv
from compchem_memory import opencode_cli, startup_scan
from compchem_memory import opencode_ingest as oi

# conftest's autouse fixture stubs hv._list_session_updates; keep the real one.
_REAL_LIST_SESSION_UPDATES = hv._list_session_updates


class _Proc:
    def __init__(self, returncode=0):
        self.returncode = returncode


def _v2(monkeypatch, tmp_path, *, db=None):
    binary = tmp_path / "v2bin" / "opencode"
    binary.parent.mkdir(exist_ok=True)
    binary.write_text("#!/bin/sh\n")
    binary.chmod(0o755)
    monkeypatch.setenv("MAGNOLIA_OPENCODE_V2", "1")
    monkeypatch.setenv("MAGNOLIA_OPENCODE_V2_BIN", str(binary))
    if db is not None:
        monkeypatch.setenv("OPENCODE_DB", str(db))
    return str(binary)


def _session_v2_db(path: Path, rows):
    """A minimal 2.3.0-shaped database: the columns the listing reads."""
    con = sqlite3.connect(path)
    con.execute("CREATE TABLE session_v2 (id TEXT PRIMARY KEY, title TEXT, "
                "time_created INTEGER, time_updated INTEGER)")
    con.executemany("INSERT INTO session_v2 VALUES (?, 't', ?, ?)",
                    [(sid, upd, upd) for sid, upd in rows])
    con.commit()
    con.close()


# ---- command selection ------------------------------------------------------

def test_command_v1_is_bare_opencode(monkeypatch):
    monkeypatch.delenv("MAGNOLIA_OPENCODE_V2", raising=False)
    assert opencode_cli.command("session", "list") == ["opencode", "session", "list"]


def test_command_v2_uses_v2_binary_and_standalone(monkeypatch, tmp_path):
    binary = _v2(monkeypatch, tmp_path)
    assert opencode_cli.command("session", "list") == [
        binary, "--standalone", "session", "list"]


def test_command_v2_default_binary_is_the_launchers(monkeypatch):
    monkeypatch.setenv("MAGNOLIA_OPENCODE_V2", "1")
    monkeypatch.delenv("MAGNOLIA_OPENCODE_V2_BIN", raising=False)
    cmd = opencode_cli.command("x")
    assert cmd[0].endswith("/.local/share/magnolia-v2/bin/opencode")
    assert cmd[1] == "--standalone"


def test_command_v2_off_values_keep_v1(monkeypatch):
    monkeypatch.setenv("MAGNOLIA_OPENCODE_V2", "0")
    assert opencode_cli.command("x") == ["opencode", "x"]


# ---- export ------------------------------------------------------------------

def test_export_session_v2_never_uses_v1_export_form(monkeypatch, tmp_path):
    import subprocess as sp
    binary = _v2(monkeypatch, tmp_path)
    calls = []

    def fake_run(cmd, stdout=None, stderr=None, timeout=None, stdin=None):
        calls.append(cmd)
        Path(stdout.name).write_text('{"messages": []}')
        return _Proc(0)

    monkeypatch.setattr(sp, "run", fake_run)
    assert oi.export_session("ses_v2") == {"messages": []}
    assert calls == [[binary, "--standalone", "session", "export", "ses_v2"]]


def test_export_session_v1_unchanged(monkeypatch):
    import subprocess as sp
    monkeypatch.delenv("MAGNOLIA_OPENCODE_V2", raising=False)
    calls = []

    def fake_run(cmd, stdout=None, stderr=None, timeout=None, stdin=None):
        calls.append(cmd)
        return _Proc(1)

    monkeypatch.setattr(sp, "run", fake_run)
    assert oi.export_session("ses_v1") is None
    assert calls == [["opencode", "export", "ses_v1"],
                     ["opencode", "session", "export", "ses_v1"]]


# ---- handover session listing -----------------------------------------------

def test_list_session_updates_v2_reads_db_without_cli(monkeypatch, tmp_path):
    db = tmp_path / "opencode-v2.db"
    _session_v2_db(db, [("ses_a", 1794664657164), ("ses_b", 1794664700000)])
    _v2(monkeypatch, tmp_path, db=db)

    def no_cli(*a, **k):
        raise AssertionError("the v2 listing must not call the OpenCode CLI")

    monkeypatch.setattr(hv.subprocess, "run", no_cli)
    assert _REAL_LIST_SESSION_UPDATES() == {
        "ses_a": 1794664657164, "ses_b": 1794664700000}


def test_list_session_updates_v2_unreadable_db_returns_none(monkeypatch, tmp_path):
    db = tmp_path / "opencode-v2.db"
    sqlite3.connect(db).close()                    # exists, no session_v2 table
    _v2(monkeypatch, tmp_path, db=db)
    monkeypatch.setattr(hv.subprocess, "run",
                        lambda *a, **k: (_ for _ in ()).throw(AssertionError("cli")))
    assert _REAL_LIST_SESSION_UPDATES() is None


def test_list_session_updates_v2_without_db_uses_standalone(monkeypatch, tmp_path):
    monkeypatch.delenv("OPENCODE_DB", raising=False)
    binary = _v2(monkeypatch, tmp_path)
    seen = {}

    def fake_run(cmd, stdout=None, stderr=None, timeout=None, stdin=None):
        seen["cmd"] = cmd
        stdout.write(json.dumps([{"id": "ses_a", "updated": 7}]))
        return _Proc(0)

    monkeypatch.setattr(hv.subprocess, "run", fake_run)
    assert _REAL_LIST_SESSION_UPDATES() == {"ses_a": 7}
    assert seen["cmd"][:4] == [binary, "--standalone", "session", "list"]


# ---- availability -------------------------------------------------------------

def test_opencode_available_v2_ignores_path(monkeypatch, tmp_path):
    _v2(monkeypatch, tmp_path)
    monkeypatch.setattr(opencode_cli.shutil, "which", lambda name: None)
    assert startup_scan._opencode_available() is True


def test_opencode_available_v2_missing_binary_and_db(monkeypatch, tmp_path):
    monkeypatch.setenv("MAGNOLIA_OPENCODE_V2", "1")
    monkeypatch.setenv("MAGNOLIA_OPENCODE_V2_BIN", str(tmp_path / "absent"))
    monkeypatch.delenv("OPENCODE_DB", raising=False)
    monkeypatch.setattr(opencode_cli.shutil, "which", lambda name: "/usr/bin/opencode")
    assert startup_scan._opencode_available() is False


def test_opencode_available_v1_uses_path(monkeypatch):
    monkeypatch.delenv("MAGNOLIA_OPENCODE_V2", raising=False)
    monkeypatch.setattr(opencode_cli.shutil, "which", lambda name: "/usr/bin/opencode")
    assert startup_scan._opencode_available() is True
