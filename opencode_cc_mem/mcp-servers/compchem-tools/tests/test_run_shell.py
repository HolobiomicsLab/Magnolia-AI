"""run_shell must NEVER raise — every failure path returns a structured dict.

Background: a TimeoutExpired escaping into fastmcp's anyio loop cascades through
BaseExceptionGroup and kills the entire MCP subprocess (taking the poller daemon
with it). Verified live: 2026-05-30 16:45 crash from an agent-induced run_shell
timeout. These tests pin the no-raise contract.
"""
import subprocess
from compchem_tools.tools import shell as shell_mod
from compchem_tools.tools.shell import run_shell


def test_happy_path_returns_success_dict(monkeypatch, tmp_path):
    """Sanity-check the happy path still works after hardening."""
    def fake_run(cmd, *, cwd=None, env=None, capture_output=True, text=True, timeout=None):
        return subprocess.CompletedProcess(cmd, 0, "hello\n", "")
    monkeypatch.setattr(shell_mod.subprocess, "run", fake_run)
    monkeypatch.setattr(shell_mod.shutil, "which", lambda _: "/fake/magnolia-run")
    monkeypatch.setattr(shell_mod.os.path, "isfile", lambda _: True)
    out = run_shell("echo hello")
    assert out == {"exit_code": 0, "stdout": "hello\n", "stderr": ""}


def test_timeout_returns_dict_does_not_raise(monkeypatch):
    """TimeoutExpired must be caught and surfaced as error_kind='timeout'."""
    def fake_run(cmd, **kw):
        raise subprocess.TimeoutExpired(cmd=cmd, timeout=600, output="partial out", stderr="partial err")
    monkeypatch.setattr(shell_mod.subprocess, "run", fake_run)
    monkeypatch.setattr(shell_mod.shutil, "which", lambda _: "/fake/magnolia-run")
    monkeypatch.setattr(shell_mod.os.path, "isfile", lambda _: True)
    out = run_shell("sleep 9999")
    assert out["exit_code"] == -1
    assert out["error_kind"] == "timeout"
    assert "600s" in out["error"]
    assert out["stdout"] == "partial out"
    assert out["stderr"] == "partial err"


def test_timeout_with_bytes_partial_output_decodes(monkeypatch):
    """TimeoutExpired's stdout/stderr can be bytes (text=False mode) or even
    invalid UTF-8 — _truncate must decode robustly."""
    bad = b"good\xff\xfe still bad"
    def fake_run(cmd, **kw):
        raise subprocess.TimeoutExpired(cmd=cmd, timeout=600, output=bad, stderr=None)
    monkeypatch.setattr(shell_mod.subprocess, "run", fake_run)
    monkeypatch.setattr(shell_mod.shutil, "which", lambda _: "/fake/magnolia-run")
    monkeypatch.setattr(shell_mod.os.path, "isfile", lambda _: True)
    out = run_shell("anything")  # must not raise
    assert out["error_kind"] == "timeout"
    assert "good" in out["stdout"]  # decoded with errors='replace'
    assert out["stderr"] == ""


def test_magnolia_run_missing_returns_dict_does_not_raise(monkeypatch):
    """Old behavior raised RuntimeError — must now return file_not_found dict."""
    monkeypatch.setattr(shell_mod.shutil, "which", lambda _: None)
    monkeypatch.setattr(shell_mod.os.path, "isfile", lambda _: False)
    out = run_shell("echo hi")
    assert out["exit_code"] == -1
    assert out["error_kind"] == "file_not_found"
    assert "magnolia-run not found" in out["error"]
    assert out["stdout"] == ""
    assert out["stderr"] == ""


def test_oserror_returns_dict_does_not_raise(monkeypatch):
    def fake_run(cmd, **kw):
        raise OSError(13, "Permission denied")
    monkeypatch.setattr(shell_mod.subprocess, "run", fake_run)
    monkeypatch.setattr(shell_mod.shutil, "which", lambda _: "/fake/magnolia-run")
    monkeypatch.setattr(shell_mod.os.path, "isfile", lambda _: True)
    out = run_shell("anything")
    assert out["exit_code"] == -1
    assert out["error_kind"] == "oserror"
    assert "Permission denied" in out["error"]


def test_arbitrary_exception_caught(monkeypatch):
    """Any exception not in the named buckets must still be caught."""
    def fake_run(cmd, **kw):
        raise ValueError("totally unexpected")
    monkeypatch.setattr(shell_mod.subprocess, "run", fake_run)
    monkeypatch.setattr(shell_mod.shutil, "which", lambda _: "/fake/magnolia-run")
    monkeypatch.setattr(shell_mod.os.path, "isfile", lambda _: True)
    out = run_shell("anything")
    assert out["exit_code"] == -1
    assert out["error_kind"] == "exception"
    assert "ValueError" in out["error"]
    assert "totally unexpected" in out["error"]


def test_stdout_truncated_to_4kb(monkeypatch):
    big = "a" * 10_000  # 10KB
    def fake_run(cmd, **kw):
        return subprocess.CompletedProcess(cmd, 0, big, "")
    monkeypatch.setattr(shell_mod.subprocess, "run", fake_run)
    monkeypatch.setattr(shell_mod.shutil, "which", lambda _: "/fake/magnolia-run")
    monkeypatch.setattr(shell_mod.os.path, "isfile", lambda _: True)
    out = run_shell("anything")
    assert len(out["stdout"]) == 4096
    assert out["stdout"] == big[-4096:]  # tail kept
