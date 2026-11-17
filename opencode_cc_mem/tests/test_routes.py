import importlib.util
import json
from importlib.machinery import SourceFileLoader
from pathlib import Path
from types import SimpleNamespace

import pytest

from campaign_runner.cli import resolve_campaign_routes
from campaign_runner.reviewers import ReviewError, choose_reviewer_model, model_family
from campaign_runner.routing import RouteError, main as routing_main, resolve_route
from router_runner import evaluator as evaluator_module


ROOT = Path(__file__).resolve().parents[1]
DAEMON = ROOT / "softwares" / "bin" / "magnolia-agents-daemon"
_DAEMON_SPEC = importlib.util.spec_from_file_location(
    "routes_agents_daemon",
    DAEMON,
    loader=SourceFileLoader("routes_agents_daemon", str(DAEMON)),
)
agents_daemon = importlib.util.module_from_spec(_DAEMON_SPEC)
_DAEMON_SPEC.loader.exec_module(agents_daemon)


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


def test_all_five_real_consumers_share_resolver(tmp_path, monkeypatch, capsys):
    monkeypatch.delenv("MAGNOLIA_AGENTS_MODEL", raising=False)
    monkeypatch.delenv("MAGNOLIA_EVAL_MODEL", raising=False)
    selected = "codex/gpt-5.6-sol"
    setup = {
        "routes": {
            "interactive": selected,
            "worker": selected,
            "reviewer": selected,
            "helper": selected,
            "evaluator": selected,
        },
    }
    install = tmp_path / "install" / "opencode_cc_mem"
    (install / ".magnolia").mkdir(parents=True)
    (install / ".magnolia" / "llm-setup.json").write_text(json.dumps(setup))
    lane = install / "projects" / "literature"
    lane.mkdir(parents=True)

    assert routing_main([
        "--root", str(install), "--role", "interactive",
    ]) == 0
    interactive = json.loads(capsys.readouterr().out)

    worker_stage = SimpleNamespace(
        id="worker", type="opencode", model=None, hard=False, review=False
    )
    worker_protocol = SimpleNamespace(
        worker=SimpleNamespace(model=None, hard_stage_model=None),
        reviewer=SimpleNamespace(enabled=False, model=None, alternate_model=None),
        stages=(worker_stage,),
    )
    reviewer_stage = SimpleNamespace(
        id="reviewer",
        type="opencode",
        model="claude-code/opus",
        hard=False,
        review=True,
    )
    reviewer_protocol = SimpleNamespace(
        worker=SimpleNamespace(model=None, hard_stage_model=None),
        reviewer=SimpleNamespace(enabled=True, model=None, alternate_model=None),
        stages=(reviewer_stage,),
    )
    campaign_worker = resolve_campaign_routes(
        worker_protocol, setup=setup, require_reviewers=True
    )
    campaign_reviewer = resolve_campaign_routes(
        reviewer_protocol, setup=setup, require_reviewers=True
    )
    helper = agents_daemon._resolve_helper_route(str(tmp_path / "install"), str(lane), None)
    evaluator = evaluator_module.resolve_evaluator_route(setup=setup)

    routes = (
        (interactive["client"], interactive["model"]),
        (
            campaign_worker.worker["worker"].client,
            campaign_worker.worker["worker"].model,
        ),
        (
            campaign_reviewer.reviewer["reviewer"].client,
            campaign_reviewer.reviewer["reviewer"].model,
        ),
        (helper.client, helper.model),
        (evaluator.client, evaluator.model),
    )
    assert routes == (("codex", "gpt-5.6-sol"),) * 5


def test_subscription_key_strings_are_exact():
    assert resolve_route("worker", {"worker": {"model": "claude-code/opus"}}).subscription_key == "claude-code"
    assert resolve_route("worker", {"worker": {"model": "codex/gpt-5.6-sol"}}).subscription_key == "codex"
    assert resolve_route("worker", {"worker": {"model": "vibe/zai-glm-5.2"}}).subscription_key == "vibe"
    assert resolve_route("worker", {"worker": {"model": "openrouter/deepseek-v4"}}).subscription_key == "opencode:openrouter"
