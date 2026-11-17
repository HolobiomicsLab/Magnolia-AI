"""Regression tests for repository-isolated hook runner modes."""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest


SOURCE = Path(__file__).resolve().parents[2] / "src"
RUNNER = SOURCE / "compchem_memory" / "hook" / "runner.py"


@pytest.mark.parametrize("mode", ("runner", "module"))
def test_memory_server_start_keeps_stdout_clean_and_installs_crash_guards(
    tmp_path, mode
):
    project = tmp_path / "project"
    project.mkdir()
    probe = r'''
import faulthandler, importlib.util, json, os, runpy, sys, threading
from fastmcp import FastMCP

mode, runner, source, project = sys.argv[1:]
os.environ["MAGNOLIA_PROJECT_DIR"] = project

def fake_run(self, *args, **kwargs):
    print("server-path diagnostic")
    sys.__stderr__.write("GUARDS=" + json.dumps({
        "faulthandler": faulthandler.is_enabled(),
        "thread_hook": threading.excepthook is not threading.__excepthook__,
    }) + "\n")

FastMCP.run = fake_run
if mode == "runner":
    spec = importlib.util.spec_from_file_location("magnolia_runner", runner)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    sys.argv = [runner, "--memory-server"]
    module.main()
else:
    sys.path.insert(0, source)
    runpy.run_module("compchem_memory.server", run_name="__main__")
'''
    completed = subprocess.run(
        [sys.executable, "-I", "-c", probe, mode, str(RUNNER), str(SOURCE), str(project)],
        cwd=project,
        env={"HOME": str(tmp_path), "PATH": os.environ.get("PATH", "")},
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
    )

    assert completed.returncode == 0, completed.stderr
    assert completed.stdout == ""
    assert "server-path diagnostic" in completed.stderr
    guard_line = next(
        line for line in completed.stderr.splitlines() if line.startswith("GUARDS=")
    )
    assert json.loads(guard_line.removeprefix("GUARDS=")) == {
        "faulthandler": True,
        "thread_hook": True,
    }
    assert (project / ".magnolia" / "crash.log").exists()


def test_campaign_stdio_memory_server_is_strictly_project_bound(tmp_path):
    project = tmp_path / "project"
    other = tmp_path / "other"
    project.mkdir()
    other.mkdir()
    probe = r'''
import importlib.util, json, os, sys
from fastmcp import FastMCP

runner, project, other = sys.argv[1:]
os.environ["MAGNOLIA_PROJECT_DIR"] = project

def fake_run(self, *args, **kwargs):
    from compchem_memory.server import ProjectBindingError, _enforce_http_project_binding
    refused = False
    try:
        _enforce_http_project_binding(other)
    except ProjectBindingError:
        refused = True
    sys.__stderr__.write("BOUND=" + json.dumps({
        "strict": os.environ.get("MAGNOLIA_PROJECT_BOUND"),
        "cross_project_refused": refused,
    }) + "\n")

FastMCP.run = fake_run
spec = importlib.util.spec_from_file_location("magnolia_runner", runner)
module = importlib.util.module_from_spec(spec)
spec.loader.exec_module(module)
sys.argv = [
    runner,
    "--memory-server",
    "--tool-allowlist-json",
    json.dumps(["mcp:compchem-memory.memory_get_context"]),
]
module.main()
'''
    completed = subprocess.run(
        [sys.executable, "-I", "-c", probe, str(RUNNER), str(project), str(other)],
        cwd=project,
        env={"HOME": str(tmp_path), "PATH": os.environ.get("PATH", "")},
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
    )

    assert completed.returncode == 0, completed.stderr
    bound_line = next(
        line for line in completed.stderr.splitlines() if line.startswith("BOUND=")
    )
    assert json.loads(bound_line.removeprefix("BOUND=")) == {
        "strict": "1",
        "cross_project_refused": True,
    }
