import pytest

from campaign_runner.executors import ExecContext, ExecutorRefused, executor_for
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
