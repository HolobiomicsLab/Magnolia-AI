import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

from campaign_runner.executors import ExecContext, ExecutorRefused
from campaign_runner.launch import prepare_launch
from campaign_runner.routing import ResolvedRoute


FORBIDDEN = {
    "CLAUDE_CONFIG_DIR": "CLAUDE-SENTINEL",
    "CODEX_HOME": "CODEX-HOME-SENTINEL",
    "VIBE_HOME": "VIBE-HOME-SENTINEL",
    "CODEX_API_KEY": "CODEX-KEY-SENTINEL",
    "ANTHROPIC_API_KEY": "ANTHROPIC-KEY-SENTINEL",
}
ROOT = Path(__file__).parents[1]


def _context(tmp_path, *, client="claude-code", role="helper", invocation="one", **changes):
    project = tmp_path / "project"
    run = tmp_path / "run"
    project.mkdir(exist_ok=True)
    (run / "control").mkdir(parents=True, exist_ok=True)
    (run / "work").mkdir(parents=True, exist_ok=True)
    route = ResolvedRoute(client, "selected-model", "family", client, role, "test")
    capabilities = set(changes.pop("capabilities", ()))
    evidence = {
        "accepted": True,
        "trusted": True,
        "stale": False,
        "identity": f"{client}-fixture-v1",
        "discovery_dir": str(project.resolve()),
        "capabilities": sorted(capabilities),
    }
    values = {
        "route": route,
        "role": role,
        "installation_root": Path(__file__).parents[1],
        "workspace_root": tmp_path,
        "project_identity": "project-fixture",
        "project_dir": project,
        "run_dir": run,
        "control_dir": run / "control",
        "work_dir": run / "work",
        "python_executable": "/venv/bin/python",
        "tools_port": 8123,
        "setup": {"memory_enabled": True},
        "memory_service": {"transport": "http", "url": "http://127.0.0.1:8765/mcp"},
        "invocation_id": invocation,
        "launch_argv": (f"stub-{client}", "--headless"),
        "role_capabilities": {client: {role: evidence}},
        "environment": {"PATH": os.environ.get("PATH", ""), **FORBIDDEN},
    }
    values.update(changes)
    return ExecContext(**values)


def _artifact_text(launch):
    return "\n".join(path.read_text(encoding="utf-8") for path in launch.files)


def test_required_role_env_wins(tmp_path):
    context = _context(
        tmp_path,
        renderer_env={
            "MAGNOLIA_ROLE": "interactive",
            "MAGNOLIA_MODEL": "wrong-model",
            "MAGNOLIA_HOOK_POLICY": "relative.json",
            "MAGNOLIA_HOOK_POLICY_REQUIRED": "0",
            "MAGNOLIA_DOORBELL": "1",
        },
    )

    launch = prepare_launch(context)

    assert launch.env["MAGNOLIA_ROLE"] == "helper"
    assert launch.env["MAGNOLIA_MODEL"] == "selected-model"
    assert Path(launch.env["MAGNOLIA_HOOK_POLICY"]).is_absolute()
    assert launch.env["MAGNOLIA_HOOK_POLICY_REQUIRED"] == "1"
    assert launch.env["MAGNOLIA_DOORBELL"] == "0"


def test_parallel_roles_and_interactive_render_do_not_cross_contaminate(tmp_path):
    first = prepare_launch(_context(tmp_path, invocation="helper-1"))
    second = prepare_launch(
        _context(
            tmp_path,
            client="vibe",
            role="evaluator",
            invocation="evaluator-1",
        )
    )

    assert first.invocation_dir != second.invocation_dir
    assert first.policy_digest != second.policy_digest
    assert first.env["MAGNOLIA_MODEL"] == "selected-model"
    assert second.env["MAGNOLIA_ROLE"] == "evaluator"
    assert "helper" not in _artifact_text(second)
    assert not (tmp_path / "project" / ".vibe" / "config.toml").exists()


def test_resume_keeps_policy_identity(tmp_path):
    first = prepare_launch(_context(tmp_path))
    resumed = prepare_launch(_context(tmp_path, resume_from=first.invocation_dir))

    assert resumed.invocation_dir == first.invocation_dir
    assert resumed.policy_digest == first.policy_digest
    assert resumed.config_digest == first.config_digest
    assert resumed.required_capability_identity == first.required_capability_identity


@pytest.mark.parametrize(
    ("evidence_change", "message"),
    (({}, "missing"), ({"stale": True}, "stale"), ({"trusted": False}, "untrusted")),
)
def test_missing_stale_or_untrusted_launch_refused(tmp_path, evidence_change, message):
    context = _context(tmp_path)
    evidence = context.role_capabilities["claude-code"]["helper"]
    if evidence_change:
        evidence.update(evidence_change)
    else:
        context = _context(tmp_path, role_capabilities={})

    with pytest.raises(ExecutorRefused, match=message):
        prepare_launch(context)


@pytest.mark.parametrize("client", ("claude-code", "codex", "vibe"))
def test_effective_environment_and_artifacts_reject_sentinels(tmp_path, client):
    launch = prepare_launch(_context(tmp_path, client=client, invocation=client))
    resumed = prepare_launch(
        _context(tmp_path, client=client, invocation=client, resume_from=launch.invocation_dir)
    )
    combined = json.dumps(dict(launch.env), sort_keys=True) + _artifact_text(launch)

    for name, sentinel in FORBIDDEN.items():
        assert name not in launch.env
        assert name not in resumed.env
        assert sentinel not in combined
    assert all(path.stat().st_mode & 0o222 == 0 for path in launch.files)


def test_noninteractive_mention_writes_no_letter(tmp_path):
    context = _context(tmp_path, prompt="Please ask @literature for a source")

    prepare_launch(context)

    assert not list(tmp_path.rglob("*.letter.md"))
    assert not list(tmp_path.rglob("agent-letters"))


def test_api_memory_route_survives_client_env_filter(tmp_path):
    context = _context(
        tmp_path,
        role="worker",
        capabilities=(
            "native_read",
            "native_glob",
            "native_grep",
            "native_edit",
            "mcp:compchem-memory.memory_get_context",
            "mcp:compchem-memory.memory_search",
            "mcp:compchem-memory.memory_get_run_history",
        ),
        memory_service={
            "transport": "http",
            "url": "http://127.0.0.1:9876/mcp",
            "authorization_env": "ANTHROPIC_API_KEY",
        },
    )

    launch = prepare_launch(context)
    mcp = json.loads((launch.invocation_dir / "mcp.json").read_text(encoding="utf-8"))

    assert mcp["memory_service"]["url"] == "http://127.0.0.1:9876/mcp"
    assert mcp["memory_service"]["authorization_env"] == "ANTHROPIC_API_KEY"
    assert "ANTHROPIC_API_KEY" not in launch.env
    assert "ANTHROPIC-KEY-SENTINEL" not in _artifact_text(launch)


def test_memory_config_refuses_embedded_client_secret(tmp_path):
    with pytest.raises(ExecutorRefused, match="forbidden client environment value"):
        prepare_launch(
            _context(
                tmp_path,
                role="worker",
                capabilities=(
                    "native_read",
                    "native_glob",
                    "native_grep",
                    "native_edit",
                    "mcp:compchem-memory.memory_get_context",
                    "mcp:compchem-memory.memory_search",
                    "mcp:compchem-memory.memory_get_run_history",
                ),
                memory_service={
                    "transport": "stdio",
                    "command": "/venv/bin/python",
                    "args": ["--api-key", "ANTHROPIC-KEY-SENTINEL"],
                },
            )
        )


def test_no_tools_helper_and_evaluator_contract(tmp_path):
    helper = prepare_launch(_context(tmp_path, role="helper", invocation="helper"))
    evaluator = prepare_launch(
        _context(tmp_path, client="codex", role="evaluator", invocation="evaluator")
    )

    for launch in (helper, evaluator):
        policy = json.loads((launch.invocation_dir / "policy.json").read_text(encoding="utf-8"))
        assert policy["effective_capabilities"] == []
        assert policy["mcp_tool_allowlist"] == []
        assert policy["edit_roots"] == []
        assert policy["delegation_policy"] == "deny"


def test_helper_native_read_requires_proved_lane_root(tmp_path):
    with pytest.raises(ExecutorRefused, match="lane read root"):
        prepare_launch(
            _context(
                tmp_path,
                required_capabilities=("native_read",),
                capabilities=("native_read",),
            )
        )

    inbox = tmp_path / "lane" / "inbox"
    inbox.mkdir(parents=True)
    launch = prepare_launch(
        _context(
            tmp_path,
            invocation="native-read",
            required_capabilities=("native_read",),
            capabilities=("native_read",),
            capability_read_roots=(inbox,),
        )
    )
    policy = json.loads((launch.invocation_dir / "policy.json").read_text(encoding="utf-8"))
    assert policy["effective_capabilities"] == ["native_read"]
    assert policy["canonical_read_roots"][-1] == str(inbox.resolve())


@pytest.mark.parametrize("entrypoint", ("magnolia-campaign", "magnolia-agents-daemon"))
def test_entrypoints_start_from_unrelated_cwd_without_pythonpath(tmp_path, entrypoint):
    env = {
        "HOME": os.environ["HOME"],
        "PATH": os.environ.get("PATH", ""),
        "MAGNOLIA_CAMPAIGN_NO_REEXEC": "1",
    }

    result = subprocess.run(
        [sys.executable, str(ROOT / "softwares" / "bin" / entrypoint), "--help"],
        cwd=tmp_path,
        env=env,
        capture_output=True,
        text=True,
        timeout=10,
        check=False,
    )

    assert result.returncode == 0, result.stderr
    assert "usage:" in result.stdout.lower()
