"""Managed memory-service leases follow executor lifetimes."""

from types import SimpleNamespace

import pytest

from campaign_runner.executors.claude_code import ClaudeCodeExecutor
from campaign_runner.executors.codex import CodexExecutor


@pytest.mark.parametrize("executor_type", (ClaudeCodeExecutor, CodexExecutor))
def test_native_executor_close_releases_memory_service_once(
    tmp_path, monkeypatch, executor_type,
):
    released = []
    handle = object()
    prepared = SimpleNamespace(
        argv=("/bin/true",),
        env={},
        client_sha256="client-digest",
        memory_service_handle=handle,
        code_mode_host_path=None,
        code_mode_host_sha256=None,
        codex_package_identity=None,
    )
    monkeypatch.setattr(
        "compchem_memory.harness.memory_service.release_memory_service",
        lambda item: released.append(item),
    )
    if executor_type is CodexExecutor:
        monkeypatch.setattr(CodexExecutor, "_native_artifact", lambda *args: None)
        monkeypatch.setattr(CodexExecutor, "_verify_project_artifacts", lambda *args: None)
        monkeypatch.setattr(CodexExecutor, "_refresh_owner_config", lambda *args: None)
        executor = executor_type(
            prepared, "gpt-test", "helper", 1,
            user_config_path=tmp_path / "config.toml",
        )
    else:
        executor = executor_type(prepared, "claude-test", "helper", 1)

    executor.close()
    executor.close()

    assert released == [handle]
    assert prepared.memory_service_handle is None
