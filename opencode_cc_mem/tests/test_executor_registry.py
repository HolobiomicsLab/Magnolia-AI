import pytest

from campaign_runner.executors import ExecContext, ExecutorRefused, executor_for
from campaign_runner.executors.capabilities import load_role_capabilities
from campaign_runner.routing import resolve_route
from campaign_runner.workers import OpenCodeExecutor


def test_registry_requires_role_capability(tmp_path):
    route = resolve_route("worker", {"harness": "codex", "models": {"codex": "codex/gpt-5"}})
    context = ExecContext(route=route, role="worker", role_capabilities={"codex": set()})
    with pytest.raises(ExecutorRefused, match="worker"):
        executor_for(route, "worker", context)


def test_registry_rejects_explicitly_unaccepted_evidence():
    route = resolve_route("worker", {"harness": "codex", "models": {"codex": "codex/gpt-5"}})
    context = ExecContext(
        route=route,
        role="worker",
        role_capabilities={"codex": {"worker": {"accepted": False}}},
    )

    with pytest.raises(ExecutorRefused, match="lacks accepted"):
        executor_for(route, "worker", context)


def test_opencode_executor_is_unchanged(tmp_path):
    route = resolve_route("worker", {"main_model": "opencode-go/deepseek-v4.1-flash"})
    context = ExecContext(route=route, role="worker", opencode_binary="opencode")
    executor = executor_for(route, "worker", context)
    assert isinstance(executor, OpenCodeExecutor)
    assert executor.model == "opencode-go/deepseek-v4.1-flash"


@pytest.mark.parametrize("role", ("worker", "reviewer", "helper", "evaluator"))
def test_default_claude_capability_projection_refuses_every_role(role):
    route = resolve_route(
        role,
        {
            "harness": "claude-code",
            "models": {"claude-code": "claude-code/haiku"},
            "routes": {role: "claude-code/haiku"},
            "model": "claude-code/haiku",
        },
    )
    context = ExecContext(
        route=route,
        role=role,
        role_capabilities=load_role_capabilities(),
    )

    with pytest.raises(ExecutorRefused, match="lacks accepted"):
        executor_for(route, role, context)


@pytest.mark.parametrize("client", ("codex",))
@pytest.mark.parametrize("role", ("worker", "reviewer", "helper", "evaluator"))
def test_default_codex_projection_refuses_every_role(client, role):
    route = resolve_route(
        role,
        {
            "harness": client,
            "models": {client: f"{client}/fixture-model"},
            "routes": {role: f"{client}/fixture-model"},
            "model": f"{client}/fixture-model",
        },
    )
    context = ExecContext(
        route=route,
        role=role,
        role_capabilities=load_role_capabilities(),
    )

    with pytest.raises(ExecutorRefused, match="lacks accepted"):
        executor_for(route, role, context)


def test_default_vibe_projection_passes_capability_admission(tmp_path):
    route = resolve_route(
        "worker",
        {
            "harness": "vibe",
            "models": {"vibe": "vibe/fixture-model"},
            "routes": {"worker": "vibe/fixture-model"},
            "model": "vibe/fixture-model",
        },
    )
    context = ExecContext(
        route=route,
        role="worker",
        project_dir=tmp_path,
        launch_argv=("vibe",),
        role_capabilities=load_role_capabilities(),
        setup={"vibe_trust_path": str(tmp_path / "trusted_folders.toml")},
    )

    with pytest.raises(ExecutorRefused, match="not persistently trusted"):
        executor_for(route, "worker", context)
