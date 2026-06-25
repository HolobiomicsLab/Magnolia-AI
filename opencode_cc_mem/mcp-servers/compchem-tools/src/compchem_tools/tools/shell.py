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
from typing import Any

_DEFAULT_TIMEOUT = 600
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
            'run in the background via submit_job(scheduler="local"). Do NOT '
            "re-run this via run_shell; it will time out again. Relaunch using "
            "the suggested_action below; set ncores/memory appropriate to the "
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
) -> dict[str, Any]:
    """Run a shell command via magnolia-run. magnolia-run writes the JSONL entries
    (single-writer invariant). This tool is a thin proxy.

    Returns on success: ``{"exit_code": int, "stdout": str (<=4KB tail), "stderr": str (<=4KB tail)}``.

    Returns on failure (NEVER raises):
        ``{"exit_code": -1, "stdout": <partial>, "stderr": <partial>, "error_kind": <str>, "error": <str>}``
        where ``error_kind`` is one of:
            - ``"file_not_found"`` — magnolia-run wrapper is missing
            - ``"timeout"``        — command exceeded the timeout (default 600s)
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
    try:
        proc = subprocess.run(
            [magnolia_run] + ["bash", "-c", cmd],
            cwd=cwd or os.getcwd(),
            env=env,
            capture_output=True,
            text=True,
            timeout=_DEFAULT_TIMEOUT,
        )
    except subprocess.TimeoutExpired as e:
        result = {
            "exit_code": -1,
            "stdout": _truncate(e.stdout),
            "stderr": _truncate(e.stderr),
            "error_kind": "timeout",
        }
        # Turn the dead-end timeout into an actionable redirect: long runs
        # belong in the background via submit_job(scheduler="local").
        result.update(_build_local_redirect(cmd, cwd, _DEFAULT_TIMEOUT))
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
        "stdout": _truncate(proc.stdout),
        "stderr": _truncate(proc.stderr),
    }
