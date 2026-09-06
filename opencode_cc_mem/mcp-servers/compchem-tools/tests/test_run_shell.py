"""run_shell must NEVER raise — every failure path returns a structured dict.

Background: a TimeoutExpired escaping into fastmcp's anyio loop cascades through
BaseExceptionGroup and kills the entire MCP subprocess (taking the poller daemon
with it). Verified live: 2026-05-30 16:45 crash from an agent-induced run_shell
timeout. These tests pin the no-raise contract.

2026-08-19 hardening: foreground default lowered 600s -> 90s with a 110s hard
cap (below the MCP client's ~2min call-abort window) and a background=True
detached mode. Implementation moved from subprocess.run to Popen+communicate
so the process group can be killed on timeout.
"""
import subprocess
from compchem_tools.tools import shell as shell_mod
from compchem_tools.tools.shell import (
    run_shell,
    _build_local_redirect,
    _DEFAULT_TIMEOUT,
    _HARD_CAP_TIMEOUT,
)


class FakeProc:
    """Minimal stand-in for subprocess.Popen."""

    def __init__(self, stdout="", stderr="", returncode=0, timeout_exc=None, spawn_exc=None):
        self._stdout = stdout
        self._stderr = stderr
        self.returncode = returncode
        self.timeout_exc = timeout_exc
        self.spawn_exc = spawn_exc
        self.pid = 4242
        self.kwargs = None
        self.communicate_timeout = None

    def __call__(self, argv, **kwargs):
        self.kwargs = kwargs
        if self.spawn_exc is not None:
            raise self.spawn_exc
        return self

    def communicate(self, timeout=None):
        self.communicate_timeout = timeout
        if self.timeout_exc is not None:
            raise self.timeout_exc
        return self._stdout, self._stderr


def _patch(monkeypatch, fake):
    monkeypatch.setattr(shell_mod.subprocess, "Popen", fake)
    monkeypatch.setattr(shell_mod.shutil, "which", lambda _: "/fake/magnolia-run")
    monkeypatch.setattr(shell_mod.os.path, "isfile", lambda _: True)
    return fake


def test_happy_path_returns_success_dict(monkeypatch):
    """Sanity-check the happy path still works after hardening."""
    fake = _patch(monkeypatch, FakeProc(stdout="hello\n"))
    out = run_shell("echo hello")
    assert out == {"exit_code": 0, "stdout": "hello\n", "stderr": ""}
    assert fake.kwargs["start_new_session"] is True
    assert fake.communicate_timeout == _DEFAULT_TIMEOUT


def test_timeout_returns_dict_does_not_raise(monkeypatch):
    """TimeoutExpired must be caught and surfaced as error_kind='timeout'."""
    exc = subprocess.TimeoutExpired(cmd="x", timeout=_DEFAULT_TIMEOUT, output="partial out", stderr="partial err")
    _patch(monkeypatch, FakeProc(timeout_exc=exc))
    monkeypatch.setattr(shell_mod.os, "killpg", lambda *a: None)
    out = run_shell("sleep 9999")
    assert out["exit_code"] == -1
    assert out["error_kind"] == "timeout"
    assert f"{_DEFAULT_TIMEOUT}s" in out["error"]
    assert out["stdout"] == "partial out"
    assert out["stderr"] == "partial err"


def test_timeout_with_bytes_partial_output_decodes(monkeypatch):
    """TimeoutExpired's stdout/stderr can be bytes — _truncate must decode robustly."""
    bad = b"good\xff\xfe still bad"
    exc = subprocess.TimeoutExpired(cmd="x", timeout=_DEFAULT_TIMEOUT, output=bad, stderr=None)
    _patch(monkeypatch, FakeProc(timeout_exc=exc))
    monkeypatch.setattr(shell_mod.os, "killpg", lambda *a: None)
    out = run_shell("anything")  # must not raise
    assert out["error_kind"] == "timeout"
    assert "good" in out["stdout"]  # decoded with errors='replace'
    assert out["stderr"] == ""


def test_timeout_returns_submit_job_redirect(monkeypatch):
    """On timeout, run_shell must redirect to submit_job(scheduler=local)
    while preserving the timeout schema and never raising."""
    exc = subprocess.TimeoutExpired(cmd="x", timeout=_DEFAULT_TIMEOUT, output="partial", stderr="")
    _patch(monkeypatch, FakeProc(timeout_exc=exc))
    monkeypatch.setattr(shell_mod.os, "killpg", lambda *a: None)

    out = run_shell("python big_analysis.py", cwd="/runs/x")

    # schema preserved
    assert out["exit_code"] == -1
    assert out["error_kind"] == "timeout"
    assert out["stdout"] == "partial"
    assert f"{_DEFAULT_TIMEOUT}s" in out["error"]  # keeps existing consumers happy
    # new redirect
    sa = out["suggested_action"]
    assert sa["tool"] == "submit_job"
    assert sa["args"]["command"] == "python big_analysis.py"
    assert sa["args"]["working_dir"] == "/runs/x"
    assert sa["args"]["scheduler"] == "local"


def test_foreground_timeout_param_is_capped(monkeypatch):
    """timeout=999 must be clamped to the hard cap so the server always answers
    before the MCP client aborts."""
    fake = _patch(monkeypatch, FakeProc(stdout="ok"))
    run_shell("echo ok", timeout=999)
    assert fake.communicate_timeout == _HARD_CAP_TIMEOUT


def test_background_returns_pid_and_log_file(monkeypatch, tmp_path):
    """background=True must detach (start_new_session) and return immediately.

    Regression (issue #4): the log dir is redirected to tmp_path. Previously
    _patch stubbed os.makedirs to a no-op, so the real _BG_LOG_DIR was never
    created and the test only passed when /tmp/magnolia-shell-bg already
    existed on the machine.
    """
    monkeypatch.setattr(shell_mod, "_BG_LOG_DIR", str(tmp_path / "bg"))
    fake = _patch(monkeypatch, FakeProc())
    out = run_shell("sleep 9999", background=True)
    assert out["background"] is True
    assert out["pid"] == 4242
    assert out["log_file"].startswith(str(tmp_path))
    assert fake.kwargs["start_new_session"] is True


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


def test_missing_wrapper_not_on_path_and_no_root_returns_dict(monkeypatch):
    """Regression (issue #3): which() -> None AND MAGNOLIA_ROOT unset left
    magnolia_run = None, and os.path.isfile(None) raised TypeError straight
    through the no-raise contract. Must return the file_not_found dict."""
    monkeypatch.setattr(shell_mod.shutil, "which", lambda _: None)
    monkeypatch.delenv("MAGNOLIA_ROOT", raising=False)
    out = run_shell("echo hi")  # must not raise
    assert out["exit_code"] == -1
    assert out["error_kind"] == "file_not_found"
    assert "MAGNOLIA_ROOT" in out["error"]


def test_oserror_returns_dict_does_not_raise(monkeypatch):
    _patch(monkeypatch, FakeProc(spawn_exc=OSError(13, "Permission denied")))
    out = run_shell("anything")
    assert out["exit_code"] == -1
    assert out["error_kind"] == "oserror"
    assert "Permission denied" in out["error"]


def test_arbitrary_exception_caught(monkeypatch):
    """Any exception not in the named buckets must still be caught."""
    _patch(monkeypatch, FakeProc(spawn_exc=ValueError("totally unexpected")))
    out = run_shell("anything")
    assert out["exit_code"] == -1
    assert out["error_kind"] == "exception"
    assert "ValueError" in out["error"]
    assert "totally unexpected" in out["error"]


def test_stdout_truncated_to_4kb(monkeypatch):
    big = "a" * 10_000  # 10KB
    _patch(monkeypatch, FakeProc(stdout=big))
    out = run_shell("anything")
    assert len(out["stdout"]) == 4096
    assert out["stdout"] == big[-4096:]  # tail kept


def test_build_local_redirect_shape_and_content():
    out = _build_local_redirect("python analyze.py", "/runs/dock1", _DEFAULT_TIMEOUT)
    # actionable error mentions the limit and the right tools, and warns off re-running
    assert f"{_DEFAULT_TIMEOUT}s" in out["error"]
    assert "submit_job" in out["error"]
    assert "background=True" in out["error"]
    assert "run_shell" in out["error"]  # the "do NOT re-run via run_shell" warning
    # structured, ready-to-issue call
    sa = out["suggested_action"]
    assert sa["tool"] == "submit_job"
    assert sa["args"]["command"] == "python analyze.py"
    assert sa["args"]["working_dir"] == "/runs/dock1"
    assert sa["args"]["scheduler"] == "local"
    # ncores/memory deliberately omitted — agent sets them per workload
    assert "ncores" not in sa["args"]


def test_build_local_redirect_cwd_none_falls_back_to_getcwd(monkeypatch):
    monkeypatch.setattr("compchem_tools.tools.shell.os.getcwd", lambda: "/here")
    out = _build_local_redirect("ls", None, _DEFAULT_TIMEOUT)
    assert out["suggested_action"]["args"]["working_dir"] == "/here"
