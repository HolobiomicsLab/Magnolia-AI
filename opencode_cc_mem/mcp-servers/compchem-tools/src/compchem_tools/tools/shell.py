"""run_shell MCP tool: shell access via magnolia-run, which writes session JSONL."""

import os
import shutil
import subprocess
from typing import Any


def run_shell(
    cmd: str,
    cwd: str | None = None,
    project_dir: str | None = None,
) -> dict[str, Any]:
    """Run a shell command via magnolia-run. magnolia-run writes the JSONL entries
    (single-writer invariant). This tool is a thin proxy.

    Returns: {"exit_code": int, "stdout": str (truncated to 4KB), "stderr": str (truncated to 4KB)}.

    Call this when: you need to execute any shell command. Opencode's bash tool is disabled.
    """
    magnolia_run = shutil.which("magnolia-run")
    if not magnolia_run:
        raise RuntimeError(
            "magnolia-run not found on PATH; cannot execute shell commands. "
            "Ensure softwares/bin is on PATH."
        )

    env = os.environ.copy()
    proc = subprocess.run(
        [magnolia_run] + ["bash", "-c", cmd],
        cwd=cwd or os.getcwd(),
        env=env,
        capture_output=True,
        text=True,
        timeout=600,
    )

    return {
        "exit_code": proc.returncode,
        "stdout": proc.stdout[-4096:],
        "stderr": proc.stderr[-4096:],
    }
