"""post_run_assess MCP tool must still record the run + return a JSON dict
with the same overall/metrics/quality_flags after delegation."""
import json


def test_post_run_assess_still_records_and_returns_json(tmp_path, monkeypatch):
    # Pin PROJECT_DIR so server's guards/resolvers behave as in production
    pd = tmp_path / "proj"
    pd.mkdir()
    run_dir = pd / ".magnolia" / "runs" / "xtb_demo"
    run_dir.mkdir(parents=True)
    (run_dir / "xtbopt.xyz").write_text("1\nH 0 0 0\n")

    monkeypatch.setenv("MAGNOLIA_PROJECT_DIR", str(pd))
    # Force a fresh server import that picks up the env var
    import importlib, sys
    for mod in [m for m in list(sys.modules) if m.startswith("compchem_memory")]:
        del sys.modules[mod]
    server = importlib.import_module("compchem_memory.server")

    out = server.post_run_assess(str(run_dir), "xtb", 0, str(pd))
    payload = json.loads(out)
    assert payload["overall"] in ("pass", "warning", "fail")
    assert "metrics" in payload
    assert "quality_flags" in payload
    # And a run YAML exists
    assert list((pd / ".magnolia" / "runs").glob("*xtb_demo*.yaml"))
