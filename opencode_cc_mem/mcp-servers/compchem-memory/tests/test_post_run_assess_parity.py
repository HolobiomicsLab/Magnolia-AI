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
    # Force a fresh server import that picks up the env var.
    # Snapshot compchem_memory.* so we can restore after the forced reimport;
    # without this, later tests see different class identities and their
    # monkeypatches break (sys.modules pollution).
    import importlib, sys
    saved = {k: v for k, v in sys.modules.items() if k.startswith("compchem_memory")}
    for mod in list(saved):
        del sys.modules[mod]
    try:
        server = importlib.import_module("compchem_memory.server")
        out = server.post_run_assess(str(run_dir), "xtb", 0, project_dir=str(pd))
        payload = json.loads(out)
        assert payload["overall"] in ("pass", "warning", "fail")
        assert "metrics" in payload
        assert "quality_flags" in payload
        # And a run YAML exists
        assert list((pd / ".magnolia" / "runs").glob("*xtb_demo*.yaml"))
    finally:
        # Restore the original modules so subsequent tests see the same
        # class identities they were collected against.
        for mod in [m for m in list(sys.modules) if m.startswith("compchem_memory")]:
            del sys.modules[mod]
        sys.modules.update(saved)


def test_post_run_assess_joins_existing_run_record(tmp_path, monkeypatch):
    """No run_id passed: the tool must reuse the submit record's run_id
    (pinned via remote.local_run_dir) instead of forking a twin."""
    from pathlib import Path

    import yaml

    pd = tmp_path / "proj"
    pd.mkdir()
    run_dir = pd / "runs" / "2026-09-23_demo"
    run_dir.mkdir(parents=True)
    (run_dir / "xtbopt.xyz").write_text("1\nH 0 0 0\n")

    from compchem_memory.tiers.project import ProjectManager

    pm = ProjectManager(Path.home() / ".magnolia")
    pm.record_run(
        str(pd), "xtb_20260923_120000_abc123", "xtb", None,
        lifecycle="running",
        remote={"scheduler": "local", "local_run_dir": str(run_dir)},
    )

    monkeypatch.setenv("MAGNOLIA_PROJECT_DIR", str(pd))
    import importlib, sys
    saved = {k: v for k, v in sys.modules.items() if k.startswith("compchem_memory")}
    for mod in list(saved):
        del sys.modules[mod]
    try:
        server = importlib.import_module("compchem_memory.server")
        out = server.post_run_assess(str(run_dir), "xtb", 0, project_dir=str(pd))
        payload = json.loads(out)
        assert payload["overall"] in ("pass", "warning", "fail")
    finally:
        for mod in [m for m in list(sys.modules) if m.startswith("compchem_memory")]:
            del sys.modules[mod]
        sys.modules.update(saved)

    runs = [
        f for f in (pd / ".magnolia" / "runs").glob("*.yaml")
        if f.name != "INDEX.yaml"
    ]
    assert len(runs) == 1, f"twin record forked: {[f.name for f in runs]}"
    assert yaml.safe_load(runs[0].read_text())["run_id"] == "xtb_20260923_120000_abc123"
