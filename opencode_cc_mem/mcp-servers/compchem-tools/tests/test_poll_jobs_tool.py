"""The compchem-tools MCP server must expose a poll_jobs() tool."""
import json
from pathlib import Path

import pytest


@pytest.fixture(autouse=True)
def _isolate_compchem_modules():
    """These tests del+reimport compchem_tools to exercise import-time behavior.
    Snapshot and restore sys.modules so the reload never leaks to other test
    files: e.g. test_ssh_slurm binds the original ssh_slurm via a top-level
    import, but its monkeypatch.setattr("compchem_tools.tools.ssh_slurm....")
    resolves through sys.modules — a leaked reload makes the patch target a
    different module than the one the test calls, silently disabling it."""
    import sys
    saved = {k: v for k, v in sys.modules.items() if k.startswith("compchem_tools")}
    yield
    for k in [x for x in list(sys.modules) if x.startswith("compchem_tools")]:
        del sys.modules[k]
    sys.modules.update(saved)


def test_poll_jobs_tool_returns_json_summary(tmp_path, monkeypatch):
    pd = tmp_path / "proj"
    (pd / ".magnolia" / "runs").mkdir(parents=True)
    monkeypatch.setenv("MAGNOLIA_PROJECT_DIR", str(pd))
    # Force a fresh import so the env var is picked up
    import importlib, sys
    for m in [x for x in list(sys.modules) if x.startswith("compchem_tools")]:
        del sys.modules[m]
    # Don't start the real background poller daemon on import — its startup sweep
    # grabs the module-level _SWEEP_LOCK and would make this poll_jobs() call
    # return {"skipped": "busy"} (a race that loses under full-suite load).
    import compchem_tools.tools.poller as poller_mod
    monkeypatch.setattr(poller_mod, "run_poll_timer_background", lambda pd: None)
    server = importlib.import_module("compchem_tools.server")
    out = server.poll_jobs(str(pd))
    payload = json.loads(out)
    assert "polled" in payload
    assert payload["polled"] == 0


def test_timer_started_at_server_import(tmp_path, monkeypatch):
    """Importing server.py must call poller.run_poll_timer_background."""
    monkeypatch.setenv("MAGNOLIA_PROJECT_DIR", str(tmp_path))
    started: list[str] = []
    import importlib, sys
    for m in [x for x in list(sys.modules) if x.startswith("compchem_tools")]:
        del sys.modules[m]
    # Patch the poller BEFORE server imports it
    import compchem_tools.tools.poller as poller_mod
    monkeypatch.setattr(poller_mod, "run_poll_timer_background",
                        lambda pd: started.append(pd))
    importlib.import_module("compchem_tools.server")
    assert started == [str(tmp_path)]
