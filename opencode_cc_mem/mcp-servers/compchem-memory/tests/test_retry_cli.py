"""The documented manual retry command clears one exhausted session."""

import json
import os

import yaml

from compchem_memory import retry_cli


def test_retry_cli_uses_configured_memory_route(tmp_path, monkeypatch):
    from compchem_memory.harness.memory_service import SERVICE_ROUTE_ENV

    project = tmp_path / "project"
    project.mkdir()
    setup = tmp_path / "llm-setup.json"
    setup.write_text(json.dumps({
        "memory_enabled": True,
        "memory_model": "deepseek/deepseek-v4.1-flash",
        "memory_provider": "openrouter",
    }))
    monkeypatch.setenv("OPENROUTER_API_KEY", "route-key")
    monkeypatch.setenv("DEEPSEEK_API_KEY", "unselected-key")
    monkeypatch.setenv("MAGNOLIA_PROJECT_DIR", "before-retry")
    route_names = SERVICE_ROUTE_ENV | {"MAGNOLIA_PROJECT_DIR"}
    before = {name: os.environ.get(name) for name in route_names}
    observed = {}

    def retry_once(project_dir, session_id):
        observed.update(os.environ)
        return {"status": "committed", "session": session_id, "saved": []}

    monkeypatch.setattr(retry_cli, "retry_session", retry_once)

    status = retry_cli.main([
        "closed.jsonl", "--project-dir", str(project), "--setup", str(setup),
    ])

    assert status == 0
    assert observed["MAGNOLIA_PROJECT_DIR"] == str(project.resolve())
    assert observed["MAGNOLIA_MEMORY_PROVIDER"] == "openrouter"
    assert observed["MAGNOLIA_MEMORY_MODEL"] == "deepseek/deepseek-v4.1-flash"
    assert observed["OPENROUTER_API_KEY"] == "route-key"
    assert "DEEPSEEK_API_KEY" not in observed
    assert {name: os.environ.get(name) for name in route_names} == before


def test_retry_cli_clears_tool_event_cap_and_retries_once(
    tmp_path, monkeypatch, capsys,
):
    project = tmp_path / "project"
    session = project / ".magnolia" / "sessions" / "closed.jsonl"
    session.parent.mkdir(parents=True)
    session.write_text(
        json.dumps({
            "timestamp": "2026-11-17T08:30:38Z",
            "event_type": "tool_success",
            "tool": "synthetic-tool",
            "result_summary": "synthetic result",
        }) + "\n"
    )
    (project / "extraction-state.yaml").write_text(yaml.safe_dump({
        "last_cursor": "",
        "session_retries": {
            "closed.jsonl": {
                "attempts": 5,
                "next_retry_at": None,
                "exhausted": True,
            },
        },
    }))
    calls = []
    monkeypatch.setattr(
        "compchem_memory.extraction.AutomaticMemoryExtractor._generate_candidates",
        lambda _self, events: calls.append(events) or [],
    )
    monkeypatch.setattr(retry_cli, "is_llm_available", lambda: True)

    status = retry_cli.main([
        "closed.jsonl", "--project-dir", str(project),
    ])

    assert status == 0
    assert len(calls) == 1
    assert "session_retries" not in yaml.safe_load(
        (project / "extraction-state.yaml").read_text()
    )
    assert '"status": "committed"' in capsys.readouterr().out


def test_retry_cli_refuses_missing_route_without_sealing(
    tmp_path, monkeypatch, capsys,
):
    from compchem_memory.harness.memory_service import SERVICE_ROUTE_ENV

    project = tmp_path / "project"
    session = project / ".magnolia" / "sessions" / "closed.jsonl"
    session.parent.mkdir(parents=True)
    session.write_text(json.dumps({
        "timestamp": "2026-11-17T08:30:38Z",
        "event_type": "tool_success",
        "tool": "synthetic-tool",
        "result_summary": "synthetic result",
    }) + "\n")
    state = {
        "last_cursor": "unchanged-cursor",
        "session_retries": {
            "closed.jsonl": {
                "attempts": 5,
                "next_retry_at": None,
                "exhausted": True,
            },
        },
    }
    state_path = project / "extraction-state.yaml"
    state_path.write_text(yaml.safe_dump(state))
    setup = tmp_path / "llm-setup.json"
    setup.write_text(json.dumps({
        "memory_enabled": True,
        "memory_model": "deepseek/deepseek-v4.1-flash",
        "memory_provider": "openrouter",
    }))
    for name in SERVICE_ROUTE_ENV:
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("MAGNOLIA_PROJECT_DIR", "before-refusal")
    before = {
        name: os.environ.get(name)
        for name in SERVICE_ROUTE_ENV | {"MAGNOLIA_PROJECT_DIR"}
    }

    status = retry_cli.main([
        "closed.jsonl", "--project-dir", str(project), "--setup", str(setup),
    ])

    assert status == 2
    assert "configured memory LLM is unavailable" in capsys.readouterr().err
    assert yaml.safe_load(state_path.read_text()) == state
    assert not session.with_suffix(".distilled").exists()
    assert {
        name: os.environ.get(name)
        for name in SERVICE_ROUTE_ENV | {"MAGNOLIA_PROJECT_DIR"}
    } == before
