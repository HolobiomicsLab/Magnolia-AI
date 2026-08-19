"""run_shell MCP tool: shell access via magnolia-run, which writes session JSONL.

ROBUSTNESS CONTRACT: this function MUST NOT raise. Any exception (timeout,
missing magnolia-run, encoding error, generic subprocess failure) escaping
into the fastmcp tool layer cascades through anyio.BaseExceptionGroup and
takes down the entire MCP server (`mcp.run()`), killing the poller daemon
with it. Every error path returns a structured dict with `error_kind` set.
"""

import os
import shutil
import subprocess
import time
import uuid
from typing import Any

# Foreground commands must return BEFORE the MCP client's own call-abort fires
# (~2 min in opencode), otherwise the abort surfaces as a client disconnect and
# can wedge/kill the serve loop. The default and the hard cap both stay below
# that window. Anything longer must use background=True or submit_job.
_DEFAULT_TIMEOUT = 90
_HARD_CAP_TIMEOUT = 110
_BG_LOG_DIR = "/tmp/magnolia-shell-bg"
_OUTPUT_TAIL = 4096


def _truncate(s: str | bytes | None) -> str:
    if s is None:
        return ""
    if isinstance(s, bytes):
        try:
            s = s.decode("utf-8", errors="replace")
        except Exception:
            s = repr(s)
    return s[-_OUTPUT_TAIL:]


def _build_local_redirect(cmd: str, cwd: str | None, timeout: int) -> dict[str, Any]:
    """Fields to merge into a timeout result that redirect a long foreground
    command to submit_job(scheduler="local").

    Pure apart from the os.getcwd() fallback when cwd is None. `timeout` is
    passed in (not read from the module constant) so the message and tests stay
    in lockstep under monkeypatch.
    """
    working_dir = cwd or os.getcwd()
    return {
        "error": (
            f"command exceeded the {timeout}s foreground limit. Long runs — "
            "including batches of many small jobs, e.g. a docking loop — must "
            "not block the tool call. Either re-run with background=True "
            "(detached, log at /tmp/magnolia-shell-bg/) or submit via "
            'submit_job(scheduler="local"). Do NOT re-run this via run_shell '
            "in the foreground; it will time out again. Relaunch using the "
            "suggested_action below; set ncores/memory appropriate to the "
            "workload."
        ),
        "suggested_action": {
            "tool": "submit_job",
            "args": {
                "command": cmd,
                "working_dir": working_dir,
                "scheduler": "local",
            },
        },
    }


def run_shell(
    cmd: str,
    cwd: str | None = None,
    project_dir: str | None = None,
    timeout: int | None = None,
    background: bool = False,
) -> dict[str, Any]:
    """Run a shell command via magnolia-run. magnolia-run writes the JSONL entries
    (single-writer invariant). This tool is a thin proxy.

    ``timeout`` caps the foreground wall-clock; values above 110 s are clamped
    (the MCP client aborts calls around 2 min, so the server must answer first).
    Default is 90 s.

    ``background=True`` detaches the command (new session, own process group),
    redirects stdout/stderr to a log file under ``/tmp/magnolia-shell-bg/``, and
    returns immediately with ``{"background": true, "pid": ..., "log_file": ...}``.
    Use it for anything long-running instead of blocking the call.

    Returns on success: ``{"exit_code": int, "stdout": str (<=4KB tail), "stderr": str (<=4KB tail)}``.

    Returns on failure (NEVER raises):
        ``{"exit_code": -1, "stdout": <partial>, "stderr": <partial>, "error_kind": <str>, "error": <str>}``
        where ``error_kind`` is one of:
            - ``"file_not_found"`` — magnolia-run wrapper is missing
            - ``"timeout"``        — command exceeded the timeout (default 90s)
            - ``"oserror"``        — subprocess invocation failed (e.g. spawn error)
            - ``"exception"``      — any other unhandled exception

    Call this when: you need to execute any shell command. Opencode's bash tool is disabled.
    """
    magnolia_root = os.environ.get("MAGNOLIA_ROOT", "")
    magnolia_run = shutil.which("magnolia-run") or (os.path.join(magnolia_root, "softwares/bin/magnolia-run") if magnolia_root else None)
    if not os.path.isfile(magnolia_run):
        return {
            "exit_code": -1,
            "stdout": "",
            "stderr": "",
            "error_kind": "file_not_found",
            "error": f"magnolia-run not found at {magnolia_run}; cannot execute shell commands.",
        }

    env = os.environ.copy()
    argv = [magnolia_run] + ["bash", "-c", cmd]

    if background:
        try:
            os.makedirs(_BG_LOG_DIR, exist_ok=True)
            log_file = os.path.join(
                _BG_LOG_DIR,
                f"{time.strftime('%Y%m%d_%H%M%S')}_{uuid.uuid4().hex[:6]}.log",
            )
            with open(log_file, "w") as lf:
                proc = subprocess.Popen(
                    argv,
                    cwd=cwd or os.getcwd(),
                    env=env,
                    start_new_session=True,
                    stdout=lf,
                    stderr=subprocess.STDOUT,
                )
        except Exception as e:
            return {
                "exit_code": -1,
                "stdout": "",
                "stderr": "",
                "error_kind": "exception",
                "error": f"{type(e).__name__}: {e}",
            }
        return {
            "background": True,
            "pid": proc.pid,
            "log_file": log_file,
            "note": (
                "Detached; stdout/stderr stream to log_file. Check progress by "
                "reading the log file (tail). The process is in its own session "
                "and survives this call."
            ),
        }

    effective_timeout = min(timeout if timeout is not None else _DEFAULT_TIMEOUT, _HARD_CAP_TIMEOUT)
    try:
        # Popen + communicate (not subprocess.run) so the pid is available for
        # process-group kill on timeout: magnolia-run spawns `bash -c`, and the
        # group kill ensures the whole tree dies, not just the wrapper.
        proc = subprocess.Popen(
            argv,
            cwd=cwd or os.getcwd(),
            env=env,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            start_new_session=True,
        )
        stdout, stderr = proc.communicate(timeout=effective_timeout)
    except subprocess.TimeoutExpired as e:
        try:
            os.killpg(proc.pid, 9)
        except Exception:
            pass
        result = {
            "exit_code": -1,
            "stdout": _truncate(e.stdout),
            "stderr": _truncate(e.stderr),
            "error_kind": "timeout",
        }
        # Turn the dead-end timeout into an actionable redirect: long runs
        # belong in the background via submit_job(scheduler="local") or
        # background=True on this tool.
        result.update(_build_local_redirect(cmd, cwd, effective_timeout))
        return result
    except OSError as e:
        return {
            "exit_code": -1,
            "stdout": "",
            "stderr": "",
            "error_kind": "oserror",
            "error": f"subprocess invocation failed: {e}",
        }
    except Exception as e:
        return {
            "exit_code": -1,
            "stdout": "",
            "stderr": "",
            "error_kind": "exception",
            "error": f"{type(e).__name__}: {e}",
        }

    return {
        "exit_code": proc.returncode,
        "stdout": _truncate(stdout),
        "stderr": _truncate(stderr),
    }
