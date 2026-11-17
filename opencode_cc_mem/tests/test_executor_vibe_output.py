"""Vibe 3.1.2 headless JSON output parsing tests."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from campaign_runner.executors.vibe import parse_vibe_export
from campaign_runner.reviewers import _extract_json, validate_review


FIXTURES = Path(__file__).parent / "fixtures" / "executors" / "vibe" / "output"


def export() -> str:
    return json.dumps({
        "schema_version": 1,
        "session_id": "session",
        "steps": 1,
        "outcome": "finished",
        "exit_code": 0,
        "config": {"active_model": "model"},
    })


def parse_stdout(name: str):
    return parse_vibe_export(
        export(), stdout_text=(FIXTURES / name).read_text(encoding="utf-8")
    )


def test_one_turn_evaluator_uses_assistant_text_parts():
    parsed = parse_stdout("evaluator.json")

    assert parsed.final_text == "VERDICT: true\nSCORE: 10"
    assert parsed.all_text == "VERDICT: true\nSCORE: 10"
    assert parsed.outcome.kind == "success"


def test_multi_turn_uses_last_assistant_message_with_text():
    parsed = parse_stdout("multi_turn.json")

    assert parsed.final_text == "Second response"
    assert parsed.all_text == "First response\nSecond response"


def test_string_content_is_supported():
    parsed = parse_stdout("string_content.json")

    assert parsed.final_text == "String response"
    assert parsed.all_text == "String response"


@pytest.mark.parametrize("stdout", ["", "not-json", "[]", "{}"])
def test_malformed_or_empty_stdout_preserves_empty_text_and_success_export(stdout):
    parsed = parse_vibe_export(export(), stdout_text=stdout)

    assert parsed.final_text == ""
    assert parsed.all_text == ""
    assert parsed.outcome.kind == "success"


def test_reviewer_final_text_passes_production_validator(tmp_path):
    evidence = tmp_path / "work" / "result.md"
    evidence.parent.mkdir()
    evidence.write_text("Observed result\n", encoding="utf-8")
    review = {
        "issues": [{
            "severity": "minor",
            "claim": "The result needs one clarification.",
            "evidence": ["work/result.md"],
            "suggested_action": "Clarify the result.",
        }],
        "followups": [],
    }
    stdout = json.dumps([{
        "type": "message",
        "role": "assistant",
        "content": [{"type": "text", "text": json.dumps(review)}],
    }])

    parsed = parse_vibe_export(export(), stdout_text=stdout)
    issues, followups = validate_review(_extract_json(parsed.final_text), tmp_path)

    assert issues == review["issues"]
    assert followups == []
