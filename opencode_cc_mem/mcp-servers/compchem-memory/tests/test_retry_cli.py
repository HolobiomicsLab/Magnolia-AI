"""The documented manual retry command clears one exhausted session."""

import json

import yaml

from compchem_memory import retry_cli


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

    status = retry_cli.main([
        "closed.jsonl", "--project-dir", str(project),
    ])

    assert status == 0
    assert len(calls) == 1
    assert "session_retries" not in yaml.safe_load(
        (project / "extraction-state.yaml").read_text()
    )
    assert '"status": "committed"' in capsys.readouterr().out
