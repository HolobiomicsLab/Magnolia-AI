import pytest

from campaign_runner.reviewers import ReviewError, choose_reviewer_model, model_family
from campaign_runner.routing import RouteError, resolve_route


def test_route_precedence_per_role():
    inputs = {
        "main_model": "opencode-go/glm-5.3",
        "routes": {"worker": "vibe/glm-5.3", "reviewer": "codex/gpt-5"},
        "worker": {"model": "claude-code/kimi-k3", "hard_stage_model": "claude-code/gpt-5"},
        "reviewer": {"model": "claude-code/glm-5.3", "alternate_model": "codex/gpt-5"},
    }
    assert resolve_route("worker", inputs).model == "kimi-k3"
    assert resolve_route("worker", {**inputs, "stage": {"hard": True}}).model == "gpt-5"
    assert resolve_route("worker", {**inputs, "stage": {"model": "vibe/deepseek-v4"}}).client == "vibe"
    reviewer = resolve_route("reviewer", inputs)
    assert reviewer.client == "claude-code"
    assert reviewer.hook_harness == "claude"
    assert reviewer.model == "glm-5.3"


def test_missing_default_refused():
    with pytest.raises(RouteError, match="worker"):
        resolve_route("worker", {"harness": "claude-code"})


def test_prefix_family_not_client_name():
    assert model_family("claude-code/gpt-5") == "gpt"
    route = resolve_route("worker", {"main_model": "claude-code/gpt-5"})
    assert (route.client, route.family, route.model) == ("claude-code", "gpt", "gpt-5")
    assert resolve_route("worker", {"main_model": "opencode-go/deepseek-v4"}).model == "opencode-go/deepseek-v4"


def test_same_family_cross_client_rejected():
    with pytest.raises(ReviewError, match="independent"):
        choose_reviewer_model("claude-code/gpt-5", "codex/gpt-5", None)


def test_different_family_and_alternate_selected():
    assert choose_reviewer_model("claude-code/gpt-5", "codex/glm-5.3", None) == "codex/glm-5.3"
    assert choose_reviewer_model("claude-code/gpt-5", "codex/gpt-5", "vibe/glm-5.3") == "vibe/glm-5.3"


def test_helper_evaluator_and_interactive_precedence(monkeypatch):
    monkeypatch.setenv("MAGNOLIA_AGENTS_MODEL", "vibe/glm-5.3")
    monkeypatch.setenv("MAGNOLIA_EVAL_MODEL", "codex/gpt-5")
    inputs = {
        "harness": "claude-code",
        "main_model": "opencode-go/deepseek-v4",
        "models": {"claude-code": "sonnet"},
        "routes": {"helper": "claude-code/kimi-k3", "evaluator": "claude-code/kimi-k3"},
        "agent": {"json": {"model": "claude-code/gpt-5"}},
        "api_fallback_model": "opencode-go/deepseek-v4",
    }
    assert resolve_route("helper", inputs).model == "gpt-5"
    assert resolve_route("helper", {**inputs, "agent": {}}).model == "glm-5.3"
    monkeypatch.delenv("MAGNOLIA_AGENTS_MODEL")
    assert resolve_route("helper", {"harness": "claude-code", "models": {"claude-code": "sonnet"}}).model == "sonnet"
    assert resolve_route("helper", {}).model == "deepseek/deepseek-flash"
    assert resolve_route("evaluator", inputs).model == "gpt-5"
    assert resolve_route("interactive", inputs).model is None
    explicit = resolve_route("interactive", {**inputs, "harness_flag": "claude-code"})
    assert (explicit.client, explicit.model, explicit.source) == ("claude-code", "sonnet", "models.claude-code")
    unprefixed = resolve_route("worker", {"harness": "codex", "models": {"codex": "gpt-5"}, "worker": {"model": "opencode-go/glm-5.3"}})
    assert (unprefixed.client, unprefixed.model) == ("opencode", "opencode-go/glm-5.3")
