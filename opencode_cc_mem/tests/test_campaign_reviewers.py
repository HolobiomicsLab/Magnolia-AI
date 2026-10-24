import json
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from campaign_runner.reviewers import (  # noqa: E402
    ReviewError,
    Reviewer,
    _extract_json,
    choose_reviewer_model,
    model_family,
    validate_review,
)


FAKE = r'''#!/usr/bin/env python3
import json
import os
import sys

mode = os.environ.get("FAKE_REVIEW_MODE", "valid")
session = "ses_review"
if mode == "retry" and "--session" not in sys.argv[1:]:
    text = "not json"
elif mode == "invalid":
    text = '{"issues":[{"severity":"catastrophic","claim":"x","evidence":["work/out.md"],"suggested_action":"y"}]}'
else:
    text = os.environ.get("FAKE_REVIEW_TEXT", '{"issues":[],"followups":[]}')
print(json.dumps({"type": "step_start", "sessionID": session, "part": {"type": "step-start"}}))
print(json.dumps({"type": "text", "sessionID": session, "part": {"type": "text", "messageID": "m1", "text": text}}))
'''


def fake_binary(tmp_path):
    path = tmp_path / "fake-opencode"
    path.write_text(FAKE, encoding="utf-8")
    path.chmod(0o755)
    return path


def test_model_family_and_independence():
    assert model_family("kimi-code-plan-cn/k3.1-pro#high") == "kimi"
    assert model_family("opencode-go/deepseek-v4.1-flash") == "deepseek"
    assert model_family("zai-coding-plan/glm-5.3") == "glm"
    assert (
        choose_reviewer_model(
            "kimi-code-plan-cn/k3.1-pro#high",
            "opencode-go/deepseek-v4.1-flash",
            "zai-coding-plan/glm-5.3",
        )
        == "opencode-go/deepseek-v4.1-flash"
    )
    assert (
        choose_reviewer_model(
            "opencode-go/deepseek-v4.1-flash",
            "opencode-go/deepseek-v4.1-flash",
            "zai-coding-plan/glm-5.3",
        )
        == "zai-coding-plan/glm-5.3"
    )
    with pytest.raises(ReviewError, match="no independent"):
        choose_reviewer_model(
            "opencode-go/deepseek-v4.1-flash",
            "opencode-go/deepseek-v4.1-flash",
            None,
        )


def test_extract_json_accepts_fence_and_prose():
    assert _extract_json('```json\n{"issues":[]}\n```') == {"issues": []}
    assert _extract_json('Here is the review:\n{"issues":[]}\nDone.') == {"issues": []}
    with pytest.raises(ReviewError):
        _extract_json("no json here")


def test_validate_review_requires_evidence_paths(tmp_path):
    (tmp_path / "work").mkdir()
    (tmp_path / "work" / "out.md").write_text("evidence")
    issues, followups = validate_review(
        {
            "issues": [
                {
                    "severity": "major",
                    "claim": "claim",
                    "evidence": ["work/out.md"],
                    "suggested_action": "act",
                }
            ],
            "followups": [
                {
                    "observation": "obs",
                    "proposed_test": "test",
                    "estimated_core_hours": 1,
                    "reason": "why",
                }
            ],
        },
        tmp_path,
    )
    assert issues[0]["severity"] == "major"
    assert followups[0]["estimated_core_hours"] == 1
    with pytest.raises(ReviewError, match="does not exist"):
        validate_review(
            {
                "issues": [
                    {
                        "severity": "major",
                        "claim": "claim",
                        "evidence": ["work/missing.md"],
                        "suggested_action": "act",
                    }
                ],
                "followups": [],
            },
            tmp_path,
        )
    with pytest.raises(ReviewError, match="severity"):
        validate_review(
            {
                "issues": [
                    {
                        "severity": "catastrophic",
                        "claim": "claim",
                        "evidence": ["work/out.md"],
                        "suggested_action": "act",
                    }
                ],
                "followups": [],
            },
            tmp_path,
        )
    with pytest.raises(ReviewError, match="unknown fields"):
        validate_review(
            {"issues": [], "followups": [], "approved": True},
            tmp_path,
        )


def test_reviewer_returns_valid_result(tmp_path):
    binary = fake_binary(tmp_path)
    (tmp_path / "work").mkdir()
    (tmp_path / "work" / "out.md").write_text("evidence")
    (tmp_path / "report.md").write_text("report")
    reviewer = Reviewer(
        binary=str(binary),
        environment={
            "FAKE_REVIEW_TEXT": json.dumps(
                {
                    "issues": [
                        {
                            "severity": "minor",
                            "claim": "claim",
                            "evidence": ["work/out.md"],
                            "suggested_action": "act",
                        }
                    ],
                    "followups": [],
                }
            )
        },
    )
    result = reviewer.run(
        "opencode-go/deepseek-v4.1-flash",
        "review",
        tmp_path,
        tmp_path / "attempt",
        tmp_path,
    )
    assert result.issues[0]["claim"] == "claim"
    assert result.model == "opencode-go/deepseek-v4.1-flash"


def test_reviewer_retries_invalid_json(tmp_path):
    binary = fake_binary(tmp_path)
    (tmp_path / "work").mkdir()
    (tmp_path / "work" / "out.md").write_text("evidence")
    (tmp_path / "report.md").write_text("report")
    reviewer = Reviewer(
        binary=str(binary),
        environment={"FAKE_REVIEW_MODE": "retry"},
    )
    result = reviewer.run(
        "opencode-go/deepseek-v4.1-flash",
        "review",
        tmp_path,
        tmp_path / "attempt",
        tmp_path,
    )
    assert result.attempts == 2
    assert result.issues == []


def test_reviewer_invalid_after_retry_raises(tmp_path):
    binary = fake_binary(tmp_path)
    reviewer = Reviewer(
        binary=str(binary),
        environment={"FAKE_REVIEW_MODE": "invalid"},
    )
    with pytest.raises(ReviewError, match="severity"):
        reviewer.run(
            "opencode-go/deepseek-v4.1-flash",
            "review",
            tmp_path,
            tmp_path / "attempt",
            tmp_path,
        )
