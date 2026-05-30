"""The compchem-tools MCP server must expose a poll_jobs() tool."""
import json
from pathlib import Path


def test_poll_jobs_tool_returns_json_summary(tmp_path, monkeypatch):
    pd = tmp_path / "proj"
    (pd / ".magnolia" / "runs").mkdir(parents=True)
    monkeypatch.setenv("MAGNOLIA_PROJECT_DIR", str(pd))
    # Force a fresh import so the env var is picked up
    import importlib, sys
    for m in [x for x in list(sys.modules) if x.startswith("compchem_tools")]:
        del sys.modules[m]
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
