import gc
import json
import sqlite3
import sys
import weakref
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from campaign_runner import workers as workers_module  # noqa: E402
from campaign_runner.process import ProcessOutcome  # noqa: E402
from campaign_runner.reviewers import (  # noqa: E402
    REVIEW_FORMAT_NOTE,
    ReviewError,
    Reviewer,
    _extract_json,
    build_review_prompt,
    choose_reviewer_model,
    model_family,
    validate_review,
)
from campaign_runner.workers import empty_usage  # noqa: E402
from campaign_runner.workers import WorkerResult  # noqa: E402


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


def test_review_prompt_states_exact_followups_shape(tmp_path):
    prompt = build_review_prompt(
        [tmp_path / "work" / "out.md"], tmp_path / "report.md", "analyze"
    )

    exact_shape = (
        '"followups":[{"observation":"...","proposed_test":"...",'
        '"estimated_core_hours":0,"reason":"..."}]'
    )
    assert exact_shape in prompt
    assert "Every followups item must be an object with exactly" in prompt
    assert "Use an empty followups array when there are no follow-ups" in prompt
    assert exact_shape in REVIEW_FORMAT_NOTE


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


def test_reviewer_uses_database_usage(tmp_path):
    binary = fake_binary(tmp_path)
    database = tmp_path / "review.db"
    connection = sqlite3.connect(database)
    connection.execute(
        "CREATE TABLE session_message (session_id TEXT, type TEXT, data TEXT)"
    )
    connection.execute(
        "INSERT INTO session_message VALUES (?, ?, ?)",
        (
            "ses_review",
            "assistant",
            json.dumps(
                {
                    "cost": 0.2,
                    "tokens": {
                        "input": 10,
                        "output": 2,
                        "reasoning": 1,
                        "cache": {"read": 0, "write": 0},
                    },
                }
            ),
        ),
    )
    connection.commit()
    connection.close()
    reviewer = Reviewer(binary=str(binary), db_path=str(database))
    result = reviewer.run(
        "opencode-go/deepseek-v4.1-flash",
        "review",
        tmp_path,
        tmp_path / "attempt",
        tmp_path,
    )
    assert result.usage["input_tokens"] == 10
    assert result.usage["cost_usd"] == 0.2


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


def test_mistral_models_share_one_family():
    from campaign_runner.reviewers import ReviewError, choose_reviewer_model, model_family

    assert model_family("mistral/mistral-large-latest") == "mistral"
    assert model_family("mistral/devstral-latest") == "mistral"
    assert model_family("mistral/deepseek-v4-1-flash-1017") == "deepseek"
    assert model_family("zai-coding-plan/glm-5.3") == "glm"
    with pytest.raises(ReviewError):
        choose_reviewer_model("mistral/mistral-large-latest", "mistral/devstral-latest", None)
    assert choose_reviewer_model(
        "mistral/mistral-large-latest", "mistral/devstral-latest", "zai-coding-plan/glm-5.3"
    ) == "zai-coding-plan/glm-5.3"


def _review_process(kwargs, text, usage=None):
    events = [
        {
            "sessionID": "ses_review",
            "part": {"type": "text", "messageID": "m1", "text": text},
        }
    ]
    if usage is not None:
        events.append(
            {
                "sessionID": "ses_review",
                "part": {
                    "type": "step-finish",
                    "cost": usage["cost_usd"],
                    "tokens": {
                        "input": usage["input_tokens"],
                        "output": usage["output_tokens"],
                    },
                },
            }
        )
    kwargs["stdout_path"].write_text(
        "\n".join(json.dumps(event) for event in events) + "\n"
    )
    kwargs["stderr_path"].write_text("")
    return ProcessOutcome(
        0, False, 0.1, kwargs["stdout_path"], kwargs["stderr_path"]
    )


def test_reviewer_configured_repairs_no_executor_empty_retry(tmp_path, monkeypatch):
    calls = []

    def fake_run_process(**kwargs):
        calls.append(kwargs)
        text = "" if len(calls) < 3 else '{"issues":[],"followups":[]}'
        return _review_process(kwargs, text)

    monkeypatch.setattr(workers_module, "run_process", fake_run_process)
    result = Reviewer(binary="opencode", max_retries=2, max_steps=8).run(
        "provider/model", "REVIEW", tmp_path, tmp_path / "attempt", tmp_path
    )

    assert result.attempts == 3
    assert [call["argv"][-1] for call in calls] == [
        "REVIEW",
        REVIEW_FORMAT_NOTE,
        REVIEW_FORMAT_NOTE,
    ]
    assert "--session" not in calls[0]["argv"]
    assert all(
        call["argv"][call["argv"].index("--session") + 1] == "ses_review"
        for call in calls[1:]
    )
    assert all("campaign-reviewer" in call["argv"] for call in calls)
    assert all(call["timeout_seconds"] == 600 for call in calls)
    assert all(
        json.loads(call["env"]["OPENCODE_CONFIG_CONTENT"])["agents"]
        ["campaign-reviewer"]["steps"] == 8
        for call in calls
    )


def test_vibe_reviewer_keeps_memory_lease_across_format_retries(
    tmp_path, monkeypatch, request
):
    from types import SimpleNamespace

    from campaign_runner.executors.vibe import VibeExecutor

    handle = object()
    prepared = SimpleNamespace(memory_service_handle=handle)
    released = []
    executor = object.__new__(VibeExecutor)
    executor.prepared = prepared
    executor.role = "reviewer"
    executor._memory_service_finalizer = weakref.finalize(
        executor,
        VibeExecutor._release_prepared_memory_service,
        prepared,
    )
    request.addfinalizer(executor._memory_service_finalizer.detach)
    monkeypatch.setattr(
        "compchem_memory.harness.memory_service.release_memory_service",
        lambda value: released.append(value) or True,
    )
    monkeypatch.setattr(VibeExecutor, "_run", lambda self, *args, **kwargs: object())

    executor.run("review", tmp_path, tmp_path, stem="reviewer")
    executor.run("repair", tmp_path, tmp_path, stem="reviewer_retry_1")

    assert released == []
    executor.close()
    executor.close()
    assert released == [handle]
    del executor
    gc.collect()
    assert released == [handle]
    assert prepared.memory_service_handle is None


@pytest.mark.parametrize("ending", ["success", "invalid", "exception", "cancelled"])
def test_vibe_reviewer_explicitly_releases_lease_at_review_end(tmp_path, monkeypatch, request, ending):
    from types import SimpleNamespace

    from campaign_runner.executors.contracts import RunOutcome
    from campaign_runner.executors.vibe import VibeExecutor

    handle, released, calls = object(), [], []
    executor = object.__new__(VibeExecutor)
    executor.prepared = SimpleNamespace(memory_service_handle=handle)
    executor.role = "reviewer"
    executor._memory_service_finalizer = weakref.finalize(
        executor, VibeExecutor._release_prepared_memory_service, executor.prepared,
    )
    request.addfinalizer(executor._memory_service_finalizer.detach)
    monkeypatch.setattr(
        "compchem_memory.harness.memory_service.release_memory_service",
        lambda value: released.append(value) or True,
    )
    monkeypatch.setattr("campaign_runner.reviewers.executor_for", lambda *args: executor)

    def run(self, *args, **kwargs):
        assert released == []
        calls.append(kwargs["stem"])
        if ending == "exception":
            raise RuntimeError("client failed")
        text = '{"issues":[],"followups":[]}' if len(calls) == 2 else "invalid"
        if ending == "invalid":
            text = "invalid"
        return WorkerResult(
            0, False, 0.1, "same", text, "", 0, empty_usage(), 1, [], [],
            outcome=RunOutcome("cancelled") if ending == "cancelled" else None,
        )

    monkeypatch.setattr(VibeExecutor, "_run", run)
    reviewer = Reviewer(binary="unused")
    if ending in {"exception", "invalid"}:
        with pytest.raises((RuntimeError, ReviewError)) as captured:
            reviewer.run("vibe/test", "review", tmp_path, tmp_path, tmp_path)
        assert captured.value.__traceback__ is not None
    else:
        reviewer.run("vibe/test", "review", tmp_path, tmp_path, tmp_path)
    assert released == [handle]
    assert executor.prepared.memory_service_handle is None
    if ending == "success":
        assert calls == ["reviewer", "reviewer_retry_1"]


def test_reviewer_db_usage_fallback(tmp_path, monkeypatch):
    fallback = empty_usage()
    fallback.update(input_tokens=11.0, output_tokens=3.0, cost_usd=0.25)

    calls = []

    def fake_run_process(**kwargs):
        calls.append(kwargs)
        return _review_process(
            kwargs, '{"issues":[],"followups":[]}', usage=fallback
        )

    monkeypatch.setattr(workers_module, "run_process", fake_run_process)
    monkeypatch.setattr(
        "campaign_runner.reviewers.read_session_usage", lambda *args: empty_usage()
    )
    result = Reviewer(binary="opencode", db_path=str(tmp_path / "empty.db")).run(
        "provider/model", "REVIEW", tmp_path, tmp_path / "attempt", tmp_path
    )
    assert result.usage == fallback
    assert len(calls) == 1
    assert calls[0]["argv"][3:7] == [
        "provider/model", "--agent", "campaign-reviewer", "--format"
    ]


def test_worker_and_reviewer_db_fallback_only_for_opencode(tmp_path, monkeypatch):
    class AlternateExecutor:
        def run(self, *args, **kwargs):
            return WorkerResult(
                0, False, 0.1, "same", '{"issues":[],"followups":[]}', "", 0,
                {**empty_usage(), "input_tokens": 7.0}, 1, [], [],
            )

    monkeypatch.setattr(
        "campaign_runner.reviewers.executor_for", lambda route, role, context: AlternateExecutor()
    )
    monkeypatch.setattr(
        "campaign_runner.reviewers.read_session_usage",
        lambda *args: (_ for _ in ()).throw(AssertionError("OpenCode DB queried")),
    )
    result = Reviewer(binary="unused", db_path="unused").run(
        "claude-code/sonnet", "review", tmp_path, tmp_path / "attempt", tmp_path
    )
    assert result.usage["input_tokens"] == 7.0


@pytest.mark.parametrize("body_error", [ReviewError("invalid review"), KeyboardInterrupt(), None])
def test_reviewer_close_failure_preserves_body_exception(tmp_path, monkeypatch, caplog, body_error):
    closed = []
    close_error = OSError("lease close failed")

    class Executor:
        def run(self, *args, **kwargs):
            if body_error is not None:
                raise body_error
            return WorkerResult(
                0, False, 0.1, "same", '{"issues":[],"followups":[]}', "", 0,
                empty_usage(), 1, [], [],
            )

        def close(self):
            closed.append(True)
            raise close_error

    monkeypatch.setattr("campaign_runner.reviewers.executor_for", lambda *args: Executor())
    expected = body_error if body_error is not None else close_error
    with pytest.raises(type(expected)) as caught:
        Reviewer(binary="unused").run("vibe/test", "review", tmp_path, tmp_path, tmp_path)

    assert caught.value is expected
    assert closed == [True]
    if body_error is not None:
        assert "lease close failed" in caplog.text
