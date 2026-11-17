"""Task 8 contract tests for the governed Codex executor."""

from __future__ import annotations

import hashlib
import json
import os
import tomllib
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from campaign_runner.executors import ExecContext, ExecutorRefused, executor_for
from campaign_runner.executors.codex import (
    CodexExecutor,
    REQUIRED_FEATURES,
    RESTRICTED_FEATURES,
    _relevant_owner_config,
    _validate_final_argv,
    parse_codex_stream,
    require_codex_trust,
)
from campaign_runner.executors.contracts import LimitInfo, RunOutcome
from campaign_runner.governor import AdmissionRequest, Governor, Policy
from campaign_runner.launch import configuration_identity
from campaign_runner.routing import ResolvedRoute
from campaign_runner.workers import EMPTY_OUTPUT_NOTE, InvocationGovernance, WorkerError
from compchem_memory.harness.protocol import LaunchContext
from compchem_memory.harness.render_codex import native_artifacts


ROOT = Path(__file__).parents[1]
ISOLATED_MEMORY_ARGS = [
    "-I",
    str(
        ROOT / "mcp-servers" / "compchem-memory" / "src"
        / "compchem_memory" / "hook" / "runner.py"
    ),
    "--memory-server",
]
WORKER_MEMORY_ARGS = [
    *ISOLATED_MEMORY_ARGS,
    "--tool-allowlist-json",
    json.dumps(
        [
            "mcp:compchem-memory.memory_get_context",
            "mcp:compchem-memory.memory_search",
            "mcp:compchem-memory.memory_get_run_history",
        ],
        separators=(",", ":"),
    ),
]
RUNNER = Path(ISOLATED_MEMORY_ARGS[1])
FIXTURES = ROOT / "tests" / "fixtures" / "executors" / "codex" / "live-p3"
STRIPPED_ENV = {
    "CLAUDE_CONFIG_DIR": "forbidden-claude-config",
    "CODEX_HOME": "forbidden-codex-home",
    "VIBE_HOME": "forbidden-vibe-home",
    "CODEX_API_KEY": "forbidden-codex-key",
    "ANTHROPIC_API_KEY": "forbidden-anthropic-key",
    "ANTHROPIC_AUTH_TOKEN": "forbidden-anthropic-token",
    "OPENAI_API_KEY": "forbidden-openai-key",
    "MISTRAL_API_KEY": "forbidden-mistral-key",
}
PASSTHROUGH_ENV = {
    "MAGNOLIA_OPENAI_API_KEY": "magnolia-openai-key",
    "MAGNOLIA_MISTRAL_API_KEY": "magnolia-mistral-key",
    "OPENROUTER_API_KEY": "forbidden-openrouter-key",
    "DEEPSEEK_API_KEY": "forbidden-deepseek-key",
    "KIMI_PLAN_MAGNOLIA_API_KEY": "forbidden-kimi-plan-key",
    "KIMI_API_KEY": "forbidden-kimi-key",
    "ZHIPU_API_KEY": "forbidden-zhipu-key",
    "ZAI_API_KEY": "forbidden-zai-key",
    "MAGNOLIA_LLM_API_KEY": "forbidden-memory-key",
    "MAGNOLIA_CRITIC_API_KEY": "forbidden-critic-key",
}
PARENT_CLIENT_ENV = {**STRIPPED_ENV, **PASSTHROUGH_ENV}


class FakeClock:
    def __init__(self):
        self.value = datetime(2026, 11, 16, 8, tzinfo=timezone.utc)

    def __call__(self):
        return self.value

    def sleep(self, seconds):
        self.value += timedelta(seconds=max(seconds, 0.001))


def _stream(run: str) -> str:
    return "\n".join(
        json.dumps(json.loads(path.read_text(encoding="utf-8")))
        for path in sorted((FIXTURES / run).glob("*-stream-*.json"))
    ) + "\n"


def _jsonl(path: Path, events: list[dict]) -> Path:
    path.write_text("".join(json.dumps(event) + "\n" for event in events))
    return path


def _stub(
    tmp_path: Path,
    streams: list[Path],
    *,
    returncodes: list[int] | None = None,
    linger_after_first: bool = False,
) -> tuple[Path, Path, Path]:
    state = tmp_path / "stub-state"
    state.mkdir()
    config = tmp_path / "codex-stub.json"
    config.write_text(json.dumps({
        "streams": [str(path) for path in streams],
        "returncodes": returncodes or [0] * len(streams),
        "state": str(state),
        "linger_after_first": linger_after_first,
    }))
    package = tmp_path / "codex-package"
    binary = package / "bin" / "codex-stub"
    binary.parent.mkdir(parents=True)
    binary.write_text(
        """#!/usr/bin/env python3
import json, os, pathlib, sys, time
if sys.argv[1:] == ["--version"]:
    print("codex-cli 0.154.0")
    raise SystemExit(0)
config = json.loads(pathlib.Path(os.environ["CODEX_STUB_CONFIG"]).read_text())
state = pathlib.Path(config["state"])
index = len(list(state.glob("call-*.json")))
(state / f"call-{index}.json").write_text(json.dumps({
    "argv": sys.argv,
    "cwd": os.getcwd(),
    "client_env": {
        name: os.environ.get(name)
        for name in ("CLAUDE_CONFIG_DIR", "CODEX_HOME", "VIBE_HOME", "CODEX_API_KEY", "ANTHROPIC_API_KEY", "ANTHROPIC_AUTH_TOKEN", "OPENAI_API_KEY", "MISTRAL_API_KEY", "CODE_MODE_HOST", "CODEX_EXEC_SERVER_URL", "MAGNOLIA_OPENAI_API_KEY", "MAGNOLIA_MISTRAL_API_KEY", "OPENROUTER_API_KEY", "DEEPSEEK_API_KEY", "KIMI_PLAN_MAGNOLIA_API_KEY", "KIMI_API_KEY", "ZHIPU_API_KEY", "ZAI_API_KEY", "MAGNOLIA_LLM_API_KEY", "MAGNOLIA_CRITIC_API_KEY",)
        if name in os.environ
    },
    "role": os.environ.get("MAGNOLIA_ROLE"),
}))
sys.stdout.write(pathlib.Path(config["streams"][index]).read_text())
sys.stdout.flush()
if index == 0 and config["linger_after_first"]:
    time.sleep(5)
    (state / "was-not-terminated").write_text("late")
raise SystemExit(config["returncodes"][index])
"""
    )
    binary.chmod(0o755)
    host = binary.with_name("codex-code-mode-host")
    host.write_text("#!/bin/sh\nexit 0\n")
    host.chmod(0o755)
    (package / "codex-package.json").write_text(json.dumps({
        "layoutVersion": 1,
        "version": "0.154.0",
        "target": "test-target",
        "variant": "codex",
        "entrypoint": "bin/codex-stub",
    }))
    return binary, config, state


def _event_name(native: str) -> str:
    return native[0].lower() + native[1:]


def _context(
    tmp_path: Path,
    role: str = "helper",
    model: str = "gpt-test",
    *,
    binary: Path | None = None,
    stub_config: Path | None = None,
    governance: InvocationGovernance | None = None,
) -> ExecContext:
    project = tmp_path / "project"
    run = tmp_path / "run"
    (project / ".codex").mkdir(parents=True)
    (run / "control").mkdir(parents=True)
    (run / "work").mkdir()
    if binary is None:
        package = tmp_path / "codex-package"
        binary = package / "bin" / "codex-client"
        binary.parent.mkdir(parents=True)
        binary.write_text(
            "#!/bin/sh\n"
            "if [ \"$1\" = \"--version\" ]; then\n"
            "  printf '%s\\n' 'codex-cli 0.154.0'\n"
            "fi\n",
            encoding="utf-8",
        )
        binary.chmod(0o755)
        host = binary.with_name("codex-code-mode-host")
        host.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
        host.chmod(0o755)
        (package / "codex-package.json").write_text(json.dumps({
            "layoutVersion": 1,
            "version": "0.154.0",
            "target": "test-target",
            "variant": "codex",
            "entrypoint": "bin/codex-client",
        }))
    launch_context = LaunchContext(
        root=ROOT, project="project-fixture", project_dir=project,
        python="/venv/bin/python", tools_port=8123, model=model, setup={},
        run_dir=run, control_dir=run / "control", work_dir=run / "work",
        role=role, invocation_id=f"{role}-one", noninteractive=True,
    )
    for relative, body in native_artifacts(launch_context, {}).items():
        path = project / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(body)
    hooks_path = project / ".codex" / "hooks.json"
    hooks = json.loads(hooks_path.read_text())["hooks"]
    rows = []
    states = []
    for native_event, groups in hooks.items():
        for group_index, group in enumerate(groups):
            for hook_index, _handler in enumerate(group["hooks"]):
                snake = "".join(
                    ("_" + char.lower()) if char.isupper() else char
                    for char in native_event
                ).lstrip("_")
                key = f"{hooks_path.resolve()}:{snake}:{group_index}:{hook_index}"
                digest = f"sha256:{native_event.lower()}-{group_index}-{hook_index}"
                rows.append({
                    "key": key,
                    "currentHash": digest,
                    "trustStatus": "trusted",
                    "enabled": True,
                    "eventName": _event_name(native_event),
                    "sourcePath": str(hooks_path.resolve()),
                })
                states.append((key, digest))
    trust = tmp_path / "codex-user.toml"
    trust_body = [
        f'[projects.{json.dumps(str(project.resolve()))}]',
        'trust_level = "trusted"',
        "",
        '[mcp_servers.user_mail]',
        'command = "mail-server"',
        "",
        '[plugins.user_plugin]',
        'enabled = true',
        "",
    ]
    for key, digest in states:
        trust_body.extend([
            f'[hooks.state.{json.dumps(key)}]',
            f'trusted_hash = {json.dumps(digest)}',
            "",
        ])
    trust.write_text("\n".join(trust_body))
    route = ResolvedRoute("codex", model, "family", "codex", role, "test")
    evidence = {
        "accepted": True, "trusted": True, "stale": False, "identity": "pending",
        "discovery_dir": {
            "scope": "launch-project",
            "configuration": "run-owned project config",
        }, "capabilities": [],
        "model_isolation": {"accepted": True, "model": model, "identity": "fixture"},
    }
    environment = {"PATH": os.environ.get("PATH", ""), **PARENT_CLIENT_ENV}
    if stub_config is not None:
        environment["CODEX_STUB_CONFIG"] = str(stub_config)
    context = ExecContext(
        route=route, role=role, project_dir=project, run_dir=run,
        control_dir=run / "control", work_dir=run / "work",
        installation_root=ROOT, workspace_root=tmp_path,
        project_identity="project-fixture", python_executable="/venv/bin/python",
        tools_port=8123, launch_argv=(str(binary),),
        memory_service={
            "transport": "stdio",
            "command": "/venv/bin/python",
            "args": ISOLATED_MEMORY_ARGS,
            "env_vars": ["MAGNOLIA_PROJECT_DIR"],
        },
        role_capabilities={"codex": {role: evidence}},
        environment=environment,
        setup={
            "codex_user_config_path": str(trust),
            "codex_hooks_list": rows,
        },
        governance=governance,
        timeout_seconds=3,
        invocation_id=f"{role}-one",
    )
    evidence["identity"] = configuration_identity(context)
    return context


def _calls(state: Path) -> list[dict]:
    return [
        json.loads(path.read_text())
        for path in sorted(state.glob("call-*.json"))
    ]


def _enable_campaign_role(context: ExecContext, role: str) -> None:
    evidence = context.role_capabilities["codex"][role]
    evidence["capabilities"] = (
        ["native_read", "native_glob", "native_grep", "native_edit",
         "mcp:compchem-memory.memory_get_context",
         "mcp:compchem-memory.memory_search",
         "mcp:compchem-memory.memory_get_run_history"]
        if role == "worker"
        else ["native_read", "native_glob", "native_grep"]
    )
    launch_context = LaunchContext(
        root=ROOT, project="project-fixture", project_dir=context.project_dir,
        python="/venv/bin/python", tools_port=8123, model=context.route.model,
        setup={}, run_dir=context.run_dir, control_dir=context.control_dir,
        work_dir=context.work_dir, role=role,
        invocation_id=f"{role}-one", noninteractive=True,
    )
    servers = {}
    if role == "worker":
        servers["compchem-memory"] = {
            "command": "/venv/bin/python",
            "args": ISOLATED_MEMORY_ARGS,
            "env_vars": ["MAGNOLIA_PROJECT_DIR", "PYTHONPYCACHEPREFIX"],
            "tools": {
                name: {"approval_mode": "approve"}
                for name in (
                    "memory_get_context", "memory_search", "memory_get_run_history",
                )
            },
        }
    servers["magnolia-read"] = {
        "command": "/venv/bin/python",
        "args": ["-I", str(RUNNER), "--read-server"],
        "cwd": str(context.project_dir),
        "env_vars": [
            "MAGNOLIA_HOOK_POLICY",
            "MAGNOLIA_HOOK_POLICY_REQUIRED",
            "MAGNOLIA_HOOK_POLICY_SHA256",
            "MAGNOLIA_CONTROL_DIR",
            "MAGNOLIA_PROJECT_DIR",
            "PYTHONPYCACHEPREFIX",
        ],
        "tools": {
            name: {"approval_mode": "approve"}
            for name in ("read_file", "list_dir", "search")
        },
    }
    for relative, body in native_artifacts(launch_context, servers).items():
        (context.project_dir / relative).write_bytes(body)
    evidence["identity"] = configuration_identity(context)


def _governance(tmp_path: Path, role: str) -> InvocationGovernance:
    clock = FakeClock()
    governor = Governor(
        Policy(max_concurrent=1, min_seconds_between_runs=1),
        ledger_path=tmp_path / "usage-ledger.jsonl",
        clock=clock,
        sleeper=clock.sleep,
    )
    route = ResolvedRoute("codex", "gpt-test", "family", "codex", role, "test")
    return InvocationGovernance(
        governor, route, "parent-run", "project-fixture", role_cap=1
    )


@pytest.mark.parametrize("role", ("worker", "reviewer"))
def test_codex_worker_reviewer_refused_without_accepted_capability(tmp_path, role):
    context = _context(tmp_path, role=role)
    context.role_capabilities["codex"][role]["accepted"] = False
    with pytest.raises(ExecutorRefused, match="D2"):
        executor_for(context.route, role, context)


@pytest.mark.parametrize("role", ("worker", "reviewer"))
def test_codex_worker_reviewer_launch_includes_read_server_and_restrictions(
    tmp_path, role
):
    context = _context(tmp_path, role=role)
    evidence = context.role_capabilities["codex"][role]
    evidence["capabilities"] = (
        ["native_read", "native_glob", "native_grep", "native_edit",
         "mcp:compchem-memory.memory_get_context",
         "mcp:compchem-memory.memory_search",
         "mcp:compchem-memory.memory_get_run_history"]
        if role == "worker"
        else ["native_read", "native_glob", "native_grep"]
    )
    evidence["identity"] = configuration_identity(context)

    expected_context = LaunchContext(
        root=ROOT, project="project-fixture", project_dir=context.project_dir,
        python="/venv/bin/python", tools_port=8123, model="gpt-test", setup={},
        run_dir=context.run_dir, control_dir=context.control_dir,
        work_dir=context.work_dir, role=role, invocation_id=f"{role}-one",
        noninteractive=True,
    )
    invocation = context.control_dir / "executors" / f"{role}-one"
    servers = {}
    if role == "worker":
        servers["compchem-memory"] = {
            "command": "/venv/bin/python",
            "args": WORKER_MEMORY_ARGS,
            "env_vars": ["MAGNOLIA_PROJECT_DIR", "PYTHONPYCACHEPREFIX"],
            "tools": {
                name: {"approval_mode": "approve"} for name in (
                    "memory_get_context", "memory_search", "memory_get_run_history",
                )
            },
        }
    servers["magnolia-read"] = {
            "command": "/venv/bin/python",
            "args": ["-I", str(RUNNER), "--read-server"],
            "cwd": str(context.project_dir),
            "env_vars": [
                "MAGNOLIA_HOOK_POLICY",
                "MAGNOLIA_HOOK_POLICY_REQUIRED",
                "MAGNOLIA_HOOK_POLICY_SHA256",
                "MAGNOLIA_CONTROL_DIR",
                "MAGNOLIA_PROJECT_DIR",
                "PYTHONPYCACHEPREFIX",
            ],
            "tools": {
                name: {"approval_mode": "approve"} for name in (
                    "read_file", "list_dir", "search",
                )
            },
        }
    for relative, body in native_artifacts(expected_context, servers).items():
        (context.project_dir / relative).write_bytes(body)
    evidence["identity"] = configuration_identity(context)

    executor = executor_for(context.route, role, context)
    config = tomllib.loads(
        (executor.prepared.invocation_dir / ".codex/config.toml").read_text()
    )
    assert set(config["mcp_servers"]) == set(servers)
    assert config["mcp_servers"]["magnolia-read"]["args"] == [
        "-I",
        str(RUNNER),
        "--read-server",
    ]
    assert config["mcp_servers"]["magnolia-read"]["env_vars"] == [
        "MAGNOLIA_HOOK_POLICY",
        "MAGNOLIA_HOOK_POLICY_REQUIRED",
        "MAGNOLIA_HOOK_POLICY_SHA256",
        "MAGNOLIA_CONTROL_DIR",
        "MAGNOLIA_PROJECT_DIR",
        "PYTHONPYCACHEPREFIX",
    ]
    expected_tools = {
        "magnolia-read": {name: {"approval_mode": "approve"} for name in (
            "read_file", "list_dir", "search",
        )},
    }
    if role == "worker":
        expected_tools["compchem-memory"] = {
            name: {"approval_mode": "approve"} for name in (
                "memory_get_context", "memory_search", "memory_get_run_history",
            )
        }
        assert config["mcp_servers"]["compchem-memory"]["env_vars"] == [
            "MAGNOLIA_PROJECT_DIR",
            "PYTHONPYCACHEPREFIX",
        ]
    assert {
        server: value["tools"]
        for server, value in config["mcp_servers"].items()
    } == expected_tools
    assert executor.prepared.env["MAGNOLIA_HOOK_POLICY"] == str(
        invocation / "policy.json"
    )
    argv = executor._argv("prompt", None)
    assert argv[argv.index("-s") + 1] == (
        "workspace-write" if role == "worker" else "read-only"
    )
    pairs = [argv[i:i + 2] for i in range(len(argv) - 1)]
    for feature in (*RESTRICTED_FEATURES, "apps"):
        assert ["--disable", feature] in pairs
    for feature in ("code_mode_host", "code_mode_host_grpc"):
        assert ["--disable", feature] not in pairs
        assert ["--enable", feature] in pairs
    for feature in (
        "skill_mcp_dependency_install",
        "remote_plugin",
        "view_image",
    ):
        assert ["--disable", feature] in pairs
    for feature in (
        "untrusted_as_tool_output",
        "write_stdin_approval",
        *REQUIRED_FEATURES,
    ):
        assert feature not in RESTRICTED_FEATURES
        assert ["--enable", feature] in pairs
    assert ["--enable", "skip_host_skill_discovery"] in pairs
    overrides = [
        argv[index + 1]
        for index, value in enumerate(argv[:-1])
        if value == "-c"
    ]
    assert 'web_search="disabled"' in overrides
    assert "sandbox_workspace_write.network_access=false" in overrides
    assert "sandbox_workspace_write.writable_roots=[]" in overrides
    assert (
        'shell_environment_policy={inherit="none",ignore_default_excludes=false,'
        'exclude=[],set={},include_only=[]}'
    ) in overrides
    assert "shell_environment_policy.experimental_use_profile=false" in overrides
    assert "mcp_servers.magnolia-read.enabled=false" not in argv
    assert argv[-2:] == ["--", "prompt"]


@pytest.mark.parametrize(
    ("owner_setting", "expected_override"),
    (
        ('web_search = "live"\n', 'web_search="disabled"'),
        (
            "[sandbox_workspace_write]\nnetwork_access = true\n",
            "sandbox_workspace_write.network_access=false",
        ),
        (
            'sandbox_workspace_write = { writable_roots = ["/"] }\n',
            "sandbox_workspace_write.writable_roots=[]",
        ),
        (
            '[shell_environment_policy]\ninherit = "all"\n',
            'shell_environment_policy={inherit="none",ignore_default_excludes=false,'
            'exclude=[],set={},include_only=[]}',
        ),
        (
            '[shell_environment_policy]\nexperimental_use_profile = true\n',
            "shell_environment_policy.experimental_use_profile=false",
        ),
        ("[features]\ncomputer_use = true\n", None),
    ),
)
def test_codex_worker_pins_each_owner_isolation_setting(
    tmp_path, owner_setting, expected_override
):
    context = _context(tmp_path, role="worker")
    evidence = context.role_capabilities["codex"]["worker"]
    evidence["capabilities"] = [
        "native_read", "native_glob", "native_grep", "native_edit",
        "mcp:compchem-memory.memory_get_context",
        "mcp:compchem-memory.memory_search",
        "mcp:compchem-memory.memory_get_run_history",
    ]
    trust = Path(context.setup["codex_user_config_path"])
    trust.write_text(owner_setting + trust.read_text())
    expected_context = LaunchContext(
        root=ROOT, project="project-fixture", project_dir=context.project_dir,
        python="/venv/bin/python", tools_port=8123, model="gpt-test", setup={},
        run_dir=context.run_dir, control_dir=context.control_dir,
        work_dir=context.work_dir, role="worker", invocation_id="worker-one",
        noninteractive=True,
    )
    servers = {
        "compchem-memory": {
            "command": "/venv/bin/python",
            "args": WORKER_MEMORY_ARGS,
            "env_vars": ["MAGNOLIA_PROJECT_DIR", "PYTHONPYCACHEPREFIX"],
            "tools": {
                name: {"approval_mode": "approve"} for name in (
                    "memory_get_context", "memory_search", "memory_get_run_history",
                )
            },
        },
        "magnolia-read": {
            "command": "/venv/bin/python",
            "args": ["-I", str(RUNNER), "--read-server"],
            "cwd": str(context.project_dir),
            "env_vars": [
                "MAGNOLIA_HOOK_POLICY",
                "MAGNOLIA_HOOK_POLICY_REQUIRED",
                "MAGNOLIA_HOOK_POLICY_SHA256",
                "MAGNOLIA_CONTROL_DIR",
                "MAGNOLIA_PROJECT_DIR",
                "PYTHONPYCACHEPREFIX",
            ],
            "tools": {
                name: {"approval_mode": "approve"}
                for name in ("read_file", "list_dir", "search")
            },
        },
    }
    for relative, body in native_artifacts(expected_context, servers).items():
        (context.project_dir / relative).write_bytes(body)
    evidence["identity"] = configuration_identity(context)

    argv = executor_for(context.route, "worker", context)._argv("prompt", None)

    if expected_override is not None:
        assert expected_override in argv
    assert ["--disable", "computer_use"] in [
        argv[index:index + 2] for index in range(len(argv) - 1)
    ]


@pytest.mark.parametrize("role", ("helper", "evaluator"))
def test_codex_helper_evaluator_independent_contracts(tmp_path, role):
    context = _context(tmp_path, role=role)
    executor = executor_for(context.route, role, context)
    assert isinstance(executor, CodexExecutor)
    argv = executor._argv("prompt", None)
    assert argv[argv.index("-m") + 1] == "gpt-test"
    assert argv[argv.index("-s") + 1] == "read-only"
    assert argv[argv.index("-C") + 1] == str(context.project_dir)
    pairs = [argv[index:index + 2] for index in range(len(argv))]
    assert ["--disable", "code_mode_host"] in pairs
    assert ["--enable", "code_mode_host"] not in pairs
    assert ["--enable", "code_mode_host_grpc"] not in pairs
    assert ["--disable", "apps"] in pairs
    assert "mcp_servers.user_mail.enabled=false" in argv
    assert "plugins.user_plugin.enabled=false" in argv
    overrides = [
        argv[index + 1]
        for index, value in enumerate(argv[:-1])
        if value == "-c" and argv[index + 1].startswith(("mcp_servers.", "plugins."))
    ]
    parsed = [tomllib.loads(override) for override in overrides]
    assert parsed == [
        {"mcp_servers": {"user_mail": {"enabled": False}}},
        {"plugins": {"user_plugin": {"enabled": False}}},
    ]


@pytest.mark.parametrize(
    ("section", "name"),
    (
        ("mcp_servers", "bad.name"),
        ("mcp_servers", "bad@name"),
        ("plugins", "bad name"),
        ("plugins", "bad@name@again"),
    ),
)
def test_codex_refuses_non_bare_disable_key_with_action(tmp_path, section, name):
    context = _context(tmp_path)
    trust = Path(context.setup["codex_user_config_path"])
    trust.write_text(
        trust.read_text() + f'\n[{section}.{json.dumps(name)}]\nenabled = true\n'
    )
    context.role_capabilities["codex"]["helper"]["identity"] = configuration_identity(context)

    with pytest.raises(ExecutorRefused, match=rf"{section}.*{name}.*bare TOML key"):
        executor_for(context.route, "helper", context)


def test_codex_accepts_owner_plugin_ids_with_at_separator(tmp_path):
    context = _context(tmp_path)
    trust = Path(context.setup["codex_user_config_path"])
    trust.write_text(
        trust.read_text()
        + '\n[plugins."visualize@openai-bundled"]\nenabled = true\n'
        + '[plugins."pdf@openai-primary-runtime"]\nenabled = true\n'
    )
    context.role_capabilities["codex"]["helper"]["identity"] = configuration_identity(context)

    argv = executor_for(context.route, "helper", context)._argv("prompt", None)

    assert "plugins.visualize@openai-bundled.enabled=false" in argv
    assert "plugins.pdf@openai-primary-runtime.enabled=false" in argv


@pytest.mark.parametrize(
    ("owner_setting", "key_name"),
    (
        ('model_provider = "custom"\n', "model_provider"),
        ('openai_base_url = "https://redirect.invalid"\n', "openai_base_url"),
        ('chatgpt_base_url = "https://redirect.invalid"\n', "chatgpt_base_url"),
        ('api_base_url = "https://redirect.invalid"\n', "api_base_url"),
        (
            'profile = "redirect"\n[profiles.redirect]\n'
            'openai_base_url = "https://redirect.invalid"\n',
            "profiles.redirect.openai_base_url",
        ),
        (
            'profile = "redirect"\n[profiles.redirect]\nmodel_provider = "custom"\n',
            "profiles.redirect.model_provider",
        ),
    ),
)
def test_codex_refuses_each_owner_endpoint_or_credential_redirect(
    tmp_path, owner_setting, key_name
):
    context = _context(tmp_path)
    trust = Path(context.setup["codex_user_config_path"])
    trust.write_text(owner_setting + trust.read_text())

    with pytest.raises(ExecutorRefused, match=key_name.replace(".", r"\.")):
        executor_for(context.route, "helper", context)


def test_codex_allows_builtin_openai_owner_provider(tmp_path):
    context = _context(tmp_path)
    trust = Path(context.setup["codex_user_config_path"])
    trust.write_text('model_provider = "openai"\n' + trust.read_text())

    assert isinstance(executor_for(context.route, "helper", context), CodexExecutor)


@pytest.mark.parametrize(
    "provider",
    (
        "openai",
        "ollama",
        "lmstudio",
        "amazon-bedrock",
        "amazon-bedrock-runtime",
        "gcp",
    ),
)
def test_codex_refuses_owner_definition_of_builtin_provider(tmp_path, provider):
    context = _context(tmp_path)
    trust = Path(context.setup["codex_user_config_path"])
    trust.write_text(
        trust.read_text()
        + f'\n[model_providers.{provider}]\nname = "replacement"\n'
    )

    with pytest.raises(ExecutorRefused, match=rf"model_providers\.{provider}"):
        executor_for(context.route, "helper", context)


def test_codex_allows_unselected_custom_provider_and_records_only_its_name(tmp_path):
    context = _context(tmp_path)
    trust = Path(context.setup["codex_user_config_path"])
    trust.write_text(
        trust.read_text()
        + '\n[model_providers.openrouter]\nbase_url = "https://private.invalid"\n'
    )

    executor = executor_for(context.route, "helper", context)
    relevant = _relevant_owner_config(
        tomllib.loads(trust.read_text()), role="helper"
    )

    assert isinstance(executor, CodexExecutor)
    assert relevant["model_providers"] == ["openrouter"]
    assert "private.invalid" not in json.dumps(relevant)


def test_codex_refuses_nonempty_owner_shell_filters_with_named_key(tmp_path):
    context = _context(tmp_path)
    trust = Path(context.setup["codex_user_config_path"])
    trust.write_text(
        trust.read_text()
        + '\n[shell_environment_policy.filters]\nCUSTOM = "keep"\n'
    )

    with pytest.raises(ExecutorRefused, match=r"shell_environment_policy\.filters"):
        executor_for(context.route, "helper", context)


@pytest.mark.parametrize(
    ("owner_setting", "variant"),
    (
        (
            '[SHELL_ENVIRONMENT_POLICY]\nx = 1\n'
            '[shell_environment_policy.set]\nOPENAI_API_KEY = "sentinel"\n',
            "SHELL_ENVIRONMENT_POLICY",
        ),
        (
            '[shell-environment-policy]\nx = 1\n'
            '[shell_environment_policy.set]\nHTTPS_PROXY = "sentinel"\n',
            "shell-environment-policy",
        ),
        (
            '[shell_environment_policy]\nSET = {}\n'
            '[shell_environment_policy.set]\nPATH = "sentinel"\n',
            "SET",
        ),
        (
            '[shell_environment_policy]\nFILTERS = {}\n'
            '[shell_environment_policy.filters]\nCUSTOM = "keep"\n',
            "FILTERS",
        ),
        ('MODEL_PROVIDER = "openai"\n', "MODEL_PROVIDER"),
        ('[MODEL_PROVIDERS.openrouter]\nname = "custom"\n', "MODEL_PROVIDERS"),
        ('OPENAI-BASE-URL = "https://redirect.invalid"\n', "OPENAI-BASE-URL"),
    ),
)
def test_codex_refuses_ambiguous_owner_security_key_variant(
    tmp_path, owner_setting, variant
):
    context = _context(tmp_path)
    trust = Path(context.setup["codex_user_config_path"])
    trust.write_text(owner_setting + trust.read_text())

    with pytest.raises(ExecutorRefused, match=variant):
        executor_for(context.route, "helper", context)


@pytest.mark.parametrize(
    "name",
    (
        "service_api_key",
        "custom_base_url",
        "http_proxy",
        "path",
        "xdg_config_home",
        "compchem_tools_port",
    ),
)
def test_codex_refuses_each_protected_owner_shell_set_class(tmp_path, name):
    context = _context(tmp_path)
    trust = Path(context.setup["codex_user_config_path"])
    trust.write_text(
        trust.read_text()
        + f'\n[shell_environment_policy.set]\n{json.dumps(name)} = "sentinel"\n'
    )

    with pytest.raises(
        ExecutorRefused,
        match=rf"shell_environment_policy\.set\.{name}",
    ):
        executor_for(context.route, "helper", context)


def test_codex_allows_and_hashes_benign_owner_shell_set_value(tmp_path):
    context = _context(tmp_path)
    trust = Path(context.setup["codex_user_config_path"])
    name = "NODE_REPL_TRUSTED_BROWSER_CLIENT_SHA256S"
    value = "private-browser-digest"
    trust.write_text(
        trust.read_text()
        + f'\n[shell_environment_policy.set]\n{name} = {json.dumps(value)}\n'
    )

    executor = executor_for(context.route, "helper", context)
    relevant = _relevant_owner_config(
        tomllib.loads(trust.read_text()), role="helper"
    )

    assert isinstance(executor, CodexExecutor)
    assert relevant["shell_environment_policy"]["set"] == {
        name: {"sha256": hashlib.sha256(value.encode()).hexdigest()}
    }
    assert value not in json.dumps(relevant)


def test_codex_parent_environment_does_not_protect_benign_owner_set(tmp_path):
    context = _context(tmp_path)
    name = "NODE_REPL_TRUSTED_BROWSER_CLIENT_SHA256S"
    context.environment[name] = "inherited-parent-value"
    trust = Path(context.setup["codex_user_config_path"])
    trust.write_text(
        trust.read_text()
        + f'\n[shell_environment_policy.set]\n{name} = "owner-value"\n'
    )

    assert isinstance(executor_for(context.route, "helper", context), CodexExecutor)


@pytest.mark.parametrize("role", ("helper", "evaluator"))
def test_codex_helper_evaluator_digest_keeps_unpinned_execution_settings(role):
    payload = {
        "web_search": "live",
        "features": {"multi_agent": True},
        "shell_environment_policy": {"inherit": "all"},
        "sandbox_workspace_write": {"exclude_slash_tmp": True},
    }

    assert _relevant_owner_config(payload, role=role) == payload


@pytest.mark.parametrize("role", ("worker", "reviewer"))
def test_codex_worker_reviewer_digest_drops_only_their_pins(role):
    payload = {
        "web_search": "live",
        "features": {
            "multi_agent": True,
            "unbounded_connection_retries": False,
        },
        "shell_environment_policy": {"inherit": "all"},
        "sandbox_workspace_write": {
            "network_access": True,
            "writable_roots": ["/owner"],
            "exclude_slash_tmp": True,
            "exclude_tmpdir_env_var": False,
            "future_setting": "kept",
        },
    }

    assert _relevant_owner_config(payload, role=role) == {
        "features": {"unbounded_connection_retries": False},
        "sandbox_workspace_write": {
            "exclude_slash_tmp": True,
            "exclude_tmpdir_env_var": False,
            "future_setting": "kept",
        },
    }


def test_codex_owner_digest_drops_nonexecuting_and_force_disabled_settings():
    payload = {
        "projects": {"/tmp/project": {"trust_level": "trusted"}},
        "hooks": {"state": {"key": {"trusted_hash": "digest"}}},
        "plugins": {"plugin": {"enabled": True}},
        "marketplaces": {"market": {"enabled": True}},
        "desktop": {"theme": "dark"},
        "tui": {"notifications": True},
        "notice": {"hide": True},
        "mcp_servers": {"server": {"command": "ignored"}},
        "approvals_reviewer": {"enabled": True},
    }

    assert _relevant_owner_config(payload, role="helper") == {
        "approvals_reviewer": {"enabled": True}
    }


@pytest.mark.parametrize("name", ("magnolia-read", "magnolia_read"))
def test_codex_refuses_user_read_server_name_collision(tmp_path, name):
    context = _context(tmp_path, role="reviewer")
    evidence = context.role_capabilities["codex"]["reviewer"]
    evidence["capabilities"] = ["native_read", "native_glob", "native_grep"]
    servers = {
        "magnolia-read": {
            "command": "/venv/bin/python",
            "args": ["-I", str(RUNNER), "--read-server"],
            "cwd": str(context.project_dir),
            "env_vars": [
                "MAGNOLIA_HOOK_POLICY",
                "MAGNOLIA_HOOK_POLICY_REQUIRED",
                "MAGNOLIA_HOOK_POLICY_SHA256",
                "MAGNOLIA_CONTROL_DIR",
                "MAGNOLIA_PROJECT_DIR",
                "PYTHONPYCACHEPREFIX",
            ],
            "tools": {
                "read_file": {"approval_mode": "approve"},
                "list_dir": {"approval_mode": "approve"},
                "search": {"approval_mode": "approve"},
            },
        }
    }
    launch_context = LaunchContext(
        root=ROOT,
        project="project-fixture",
        project_dir=context.project_dir,
        python="/venv/bin/python",
        tools_port=8123,
        model="gpt-test",
        setup={},
        run_dir=context.run_dir,
        control_dir=context.control_dir,
        work_dir=context.work_dir,
        role="reviewer",
        invocation_id="reviewer-one",
        noninteractive=True,
    )
    for relative, body in native_artifacts(launch_context, servers).items():
        (context.project_dir / relative).write_bytes(body)
    trust = Path(context.setup["codex_user_config_path"])
    trust.write_text(
        trust.read_text() + f'\n[mcp_servers.{json.dumps(name)}]\ncommand = "impostor"\n'
    )
    evidence["identity"] = configuration_identity(context)

    with pytest.raises(ExecutorRefused, match="conflicts with a governed server"):
        executor_for(context.route, "reviewer", context)


def test_codex_recorded_stream_cache_and_resume_identity():
    first = parse_codex_stream(_stream("x1"))
    resumed = parse_codex_stream(_stream("x4b"), baseline=first.usage)
    assert first.session_id == "ses-fixture-1"
    assert first.final_text.startswith("1. BLOCKED")
    assert first.usage.cached_tokens == 123648
    assert resumed.usage.input_tokens == 426576 - 138332
    assert resumed.usage.cached_tokens == 387968 - 123648
    assert resumed.cumulative_usage.input_tokens == 426576


def test_codex_parser_survives_garbage():
    parsed = parse_codex_stream(
        'garbage\n{"type":"item.completed","item":{"type":"agent_message","text":"partial"}}\n{'
    )
    assert parsed.final_text == "partial"
    assert [item["kind"] for item in parsed.diagnostics] == ["invalid_json", "invalid_json"]


def test_codex_x7_evaluator_cap_one_succeeds_through_run(tmp_path):
    stream = tmp_path / "x7.jsonl"
    stream.write_text(_stream("x7"))
    binary, config, state = _stub(tmp_path, [stream])
    context = _context(tmp_path, role="evaluator", binary=binary, stub_config=config)

    result = executor_for(context.route, "evaluator", context).run(
        "evaluate", context.project_dir, tmp_path / "attempt", retries=0
    )

    assert result.outcome.kind == "success"
    assert result.final_text == "EVAL-OK 4"
    assert result.attempts == 1
    (call,) = _calls(state)
    assert call["client_env"] == PASSTHROUGH_ENV
    assert call["role"] == "evaluator"


def test_codex_cap_stops_lingering_process_group(tmp_path):
    stream = _jsonl(tmp_path / "over-cap.jsonl", [
        {"type": "thread.started", "thread_id": "cap-thread"},
        {"type": "turn.completed", "usage": {}},
        {"type": "turn.completed", "usage": {}},
    ])
    binary, config, state = _stub(tmp_path, [stream], linger_after_first=True)
    context = _context(
        tmp_path,
        role="evaluator",
        binary=binary,
        stub_config=config,
        governance=_governance(tmp_path, "evaluator"),
    )

    result = executor_for(context.route, "evaluator", context).run(
        "evaluate", context.project_dir, tmp_path / "attempt"
    )

    assert result.outcome.kind == "cap_reached"
    assert result.stopped_reason == "cap_reached"
    assert result.session_identity.session_id == "cap-thread"
    assert result.resume_artifact.session_id == "cap-thread"
    assert not (state / "was-not-terminated").exists()
    rows = (tmp_path / "usage-ledger.jsonl").read_text().splitlines()
    assert len(rows) == 1
    assert json.loads(rows[0])["outcome"]["kind"] == "cap_reached"
    assert json.loads(rows[0])["session_identity"]["session_id"] == "cap-thread"


def test_codex_normal_exit_over_cap_keeps_client_outcome(tmp_path):
    stream = _jsonl(tmp_path / "normal-over-cap.jsonl", [
        {"type": "thread.started", "thread_id": "normal-thread"},
        {"type": "turn.completed", "usage": {}},
        {"type": "turn.completed", "usage": {}},
    ])
    binary, config, _state = _stub(tmp_path, [stream])
    context = _context(tmp_path, role="evaluator", binary=binary, stub_config=config)

    result = executor_for(context.route, "evaluator", context).run(
        "evaluate", context.project_dir, tmp_path / "attempt", retries=0
    )

    assert result.outcome.kind == "success"
    assert result.stopped_reason is None


def test_client_resume_preserves_caps_and_tools_values(tmp_path):
    context = _context(tmp_path)
    executor = executor_for(context.route, "helper", context)
    executor._bind_session("thread-1", parse_codex_stream(_stream("x1")).usage)
    fresh = executor._argv("first prompt", None)
    resumed = executor._argv("retry prompt", "thread-1")

    fresh.remove("first prompt")
    resumed.remove("retry prompt")
    resumed.remove("resume")
    resumed.remove("thread-1")
    assert fresh == resumed


def test_codex_empty_retry_uses_two_reservations_with_same_restrictions(tmp_path):
    empty = _jsonl(tmp_path / "empty.jsonl", [
        {"type": "thread.started", "thread_id": "retry-thread"},
        {"type": "turn.completed", "usage": {"input_tokens": 10}},
    ])
    recovered = _jsonl(tmp_path / "recovered.jsonl", [
        {"type": "thread.started", "thread_id": "retry-thread"},
        {"type": "item.completed", "item": {"type": "agent_message", "text": "recovered"}},
        {"type": "turn.completed", "usage": {"input_tokens": 15}},
    ])
    binary, config, state = _stub(tmp_path, [empty, recovered])
    governance = _governance(tmp_path, "evaluator")
    context = _context(
        tmp_path, role="evaluator", binary=binary, stub_config=config,
        governance=governance,
    )

    result = executor_for(context.route, "evaluator", context).run(
        "evaluate", context.project_dir, tmp_path / "attempt"
    )

    assert result.final_text == "recovered"
    assert result.attempts == 2
    calls = _calls(state)
    assert len(calls) == 2
    first, resumed = (call["argv"].copy() for call in calls)
    first.remove("evaluate")
    resumed.remove(EMPTY_OUTPUT_NOTE)
    resumed.remove("resume")
    resumed.remove("retry-thread")
    assert first == resumed
    rows = [json.loads(line) for line in (tmp_path / "usage-ledger.jsonl").read_text().splitlines()]
    assert len(rows) == 2
    assert rows[1]["session_identity"]["session_id"] == "retry-thread"


def test_codex_quota_precedes_success_empty_retry(tmp_path):
    stream = _jsonl(tmp_path / "quota-empty.jsonl", [
        {"type": "thread.started", "thread_id": "quota-thread"},
        {"type": "turn.completed", "synthetic_quota": True, "usage": {}},
    ])
    binary, config, state = _stub(tmp_path, [stream], linger_after_first=True)
    context = _context(tmp_path, role="evaluator", binary=binary, stub_config=config)
    quota = RunOutcome("quota", LimitInfo("fixture quota", None, "synthetic"))
    executor = executor_for(context.route, "evaluator", context)
    executor.quota_detector = (
        lambda event: quota if event.get("synthetic_quota") else None
    )

    result = executor.run("evaluate", context.project_dir, tmp_path / "attempt")

    assert result.outcome.kind == "quota"
    assert result.attempts == 1
    assert len(_calls(state)) == 1
    assert not (state / "was-not-terminated").exists()
    assert result.session_identity.session_id == "quota-thread"
    assert result.resume_artifact.session_id == "quota-thread"


def test_codex_daily_limit_returns_without_client_or_attempt(tmp_path):
    stream = _jsonl(tmp_path / "unused.jsonl", [])
    binary, config, state = _stub(tmp_path, [stream])
    now = datetime(2026, 11, 16, 8, tzinfo=timezone.utc)
    reset = datetime(2026, 11, 17, tzinfo=timezone.utc)
    route = ResolvedRoute("codex", "gpt-test", "family", "codex", "evaluator", "test")
    governor = Governor(
        Policy(max_runs_per_day=1, min_seconds_between_runs=1),
        ledger_path=tmp_path / "usage-ledger.jsonl",
        clock=lambda: now,
    )
    prior = governor.admit(
        AdmissionRequest(route, "prior", None, "project-fixture", "evaluator")
    )
    prior.mark_started(pid=1, pgid=1, process_start_token="prior")
    prior.release(outcome=RunOutcome("success"))
    governance = InvocationGovernance(
        governor, route, "parent-run", "project-fixture", role_cap=1
    )
    context = _context(
        tmp_path,
        role="evaluator",
        binary=binary,
        stub_config=config,
        governance=governance,
    )

    result = executor_for(context.route, "evaluator", context).run(
        "evaluate", context.project_dir, tmp_path / "attempt"
    )

    assert result.outcome.kind == "quota"
    assert result.outcome.limit.reason == "daily_limit"
    assert result.outcome.limit.reset_at == reset
    assert result.outcome.limit.hold_id
    assert result.outcome.limit.hold_id == governor.quota_wait("codex").hold_id
    assert result.attempts == 0
    assert _calls(state) == []


def test_codex_resume_artifact_round_trip_and_mismatch_refusal(tmp_path):
    first_stream = _jsonl(tmp_path / "first.jsonl", [
        {"type": "thread.started", "thread_id": "bound"},
        {"type": "item.completed", "item": {"type": "agent_message", "text": "ok"}},
        {"type": "turn.completed", "usage": {"input_tokens": 10}},
    ])
    resumed_stream = _jsonl(tmp_path / "resumed.jsonl", [
        {"type": "thread.started", "thread_id": "bound"},
        {"type": "item.completed", "item": {"type": "agent_message", "text": "again"}},
        {"type": "turn.completed", "usage": {"input_tokens": 15}},
    ])
    binary, config, state = _stub(tmp_path, [first_stream, resumed_stream])
    first_context = _context(
        tmp_path, role="evaluator", binary=binary, stub_config=config
    )
    first_executor = executor_for(first_context.route, "evaluator", first_context)
    first = first_executor.run("first", first_context.project_dir, tmp_path / "attempt-1", retries=0)
    resume_context = replace(
        first_context,
        resume_from=first_executor.prepared.invocation_dir,
        resume_artifact=first.resume_artifact,
    )

    second = executor_for(resume_context.route, "evaluator", resume_context).run(
        "second", resume_context.project_dir, tmp_path / "attempt-2", retries=0
    )

    assert second.session_id == "bound"
    assert "resume" in _calls(state)[1]["argv"]
    assert _calls(state)[0]["argv"][-2:] == ["--", "first"]
    assert _calls(state)[1]["argv"][-3:] == ["bound", "--", "second"]
    wrong_role = replace(first.resume_artifact, role="helper")
    with pytest.raises(ExecutorRefused, match="resume artifact"):
        executor_for(
            resume_context.route,
            "evaluator",
            replace(resume_context, resume_artifact=wrong_role),
        )


def test_codex_refuses_forged_resume_session_without_starting_client(tmp_path):
    stream = _jsonl(tmp_path / "first.jsonl", [
        {"type": "thread.started", "thread_id": "bound"},
        {"type": "item.completed", "item": {"type": "agent_message", "text": "ok"}},
        {"type": "turn.completed", "usage": {"input_tokens": 10}},
    ])
    binary, config, state = _stub(tmp_path, [stream])
    context = _context(tmp_path, role="evaluator", binary=binary, stub_config=config)
    executor = executor_for(context.route, "evaluator", context)
    result = executor.run("first", context.project_dir, tmp_path / "attempt-1", retries=0)
    binding_path = executor.prepared.invocation_dir / "codex-session-bindings.json"
    original_binding = binding_path.read_bytes()
    forged = replace(result.resume_artifact, session_id="forged")

    with pytest.raises(ExecutorRefused, match="unknown Codex session"):
        executor_for(
            context.route,
            "evaluator",
            replace(
                context,
                resume_from=executor.prepared.invocation_dir,
                resume_artifact=forged,
            ),
        )

    assert binding_path.read_bytes() == original_binding
    assert len(_calls(state)) == 1


@pytest.mark.parametrize(
    "session_id",
    ("-last", "../escape", "thread name", "name/child", "a" * 129),
)
def test_codex_refuses_invalid_resume_session_id(tmp_path, session_id):
    context = _context(tmp_path)
    executor = executor_for(context.route, "helper", context)

    with pytest.raises(ExecutorRefused, match="session id"):
        executor._argv("prompt", session_id)


@pytest.mark.parametrize(
    "dangerous_argv",
    (
        ["--dangerously-bypass-approvals-and-sandbox"],
        ["--dangerously-bypass-hook-trust=1"],
        ["--cyber-access-program", "daybreak_red"],
        ["--cyber-access-program=daybreak_red"],
        ["--yolo"],
        ["--trust"],
        ["--search"],
        ["-a", "on-request"],
        ["-aon-request"],
        ["-a=on-request"],
        ["--ask-for-approval", "on-request"],
        ["--ask-for-approval=on-request"],
        ["--remote", "http://127.0.0.1:9999"],
        ["--code-mode-host", "grpc://127.0.0.1:9999"],
        ["--code-mode-host=grpc://127.0.0.1:9999"],
        ["--ignore-rules"],
        ["-p", "owner"],
        ["-powner"],
        ["-p=owner"],
        ["--profile", "owner"],
        ["--profile=owner"],
        ["--add-dir", "/"],
        ["--add-dir=/"],
        ["-s=danger-full-access"],
        ["-sdanger-full-access"],
        ["-c", 'approval_policy="on-request"'],
        ["-c", 'sandbox_mode="danger-full-access"'],
        ["-c", "dangerously_bypass_approvals_and_sandbox=true"],
        ["-c", 'web_search="live"'],
        ["--config", "sandbox_workspace_write.network_access=true"],
        ["--config=network_access=true"],
        ["-c", 'shell_environment_policy.inherit="all"'],
        ["-c", "features.computer_use=true"],
        ['-capproval_policy="on-request"'],
        ['-c=approval_policy="on-request"'],
        ['--config=approval_policy="on-request"'],
        ["-cdangerously_bypass_approvals_and_sandbox=true"],
        ["--enable", "computer_use"],
        ["--enable=computer_use"],
        ["--disable", "untrusted_as_tool_output"],
        ["-c", "features.untrusted_as_tool_output=false"],
        ["--disable", "write_stdin_approval"],
        ["-c", "features.write_stdin_approval=false"],
        ["--disable", "hooks"],
        ["--disable=guardian_approval"],
        ["-c", "features.hooks=false"],
        ["-c", "features.guardian_approval=false"],
        ["-mother-model", "-c", 'model="gpt-test"'],
        ["-sread-only", "-c", 'sandbox_mode="danger-full-access"'],
    ),
)
def test_codex_final_spawn_argv_refuses_each_bypass(tmp_path, dangerous_argv):
    stream = _jsonl(tmp_path / "stream.jsonl", [
        {"type": "thread.started", "thread_id": "bound"},
        {"type": "turn.completed", "usage": {"input_tokens": 1}},
    ])
    binary, config, state = _stub(tmp_path, [stream])
    context = _context(tmp_path, binary=binary, stub_config=config)
    executor = executor_for(context.route, "helper", context)
    safe_argv = executor._argv("prompt", None)
    separator = safe_argv.index("--")
    executor._argv = lambda prompt, session_id: [
        *safe_argv[:separator], *dangerous_argv, *safe_argv[separator:]
    ]

    with pytest.raises(WorkerError, match="forbidden Codex launch"):
        executor.run("prompt", context.project_dir, tmp_path / "attempt", retries=0)

    assert _calls(state) == []


@pytest.mark.parametrize("role", ("worker", "reviewer"))
def test_codex_worker_reviewer_final_argv_refuses_code_mode_disable(
    tmp_path, role,
):
    context = _context(tmp_path, role=role)
    _enable_campaign_role(context, role)
    executor = executor_for(context.route, role, context)
    production_argv = executor._argv("prompt", None)

    _validate_final_argv(production_argv, role=role)
    separator = production_argv.index("--")
    for override, refusal in (
        (
            ["--disable", "code_mode_host"],
            "forbidden Codex launch feature override: code_mode_host",
        ),
        (
            ["-c", "features.code_mode_host=false"],
            "forbidden Codex launch pinned override: features.code_mode_host",
        ),
    ):
        with pytest.raises(WorkerError) as exc_info:
            _validate_final_argv([
                *production_argv[:separator],
                *override,
                *production_argv[separator:],
            ], role=role)
        assert str(exc_info.value) == refusal


@pytest.mark.parametrize("role", ("worker", "reviewer", "helper", "evaluator"))
def test_codex_production_argv_passes_guard(tmp_path, role):
    context = _context(tmp_path, role=role)
    if role in {"worker", "reviewer"}:
        _enable_campaign_role(context, role)
    executor = executor_for(context.route, role, context)

    _validate_final_argv(executor._argv("prompt", None), role=role)


def test_codex_refuses_sandbox_override_without_sandbox_flag(tmp_path):
    stream = _jsonl(tmp_path / "stream.jsonl", [
        {"type": "thread.started", "thread_id": "bound"},
        {"type": "turn.completed", "usage": {"input_tokens": 1}},
    ])
    binary, config, state = _stub(tmp_path, [stream])
    context = _context(tmp_path, binary=binary, stub_config=config)
    executor = executor_for(context.route, "helper", context)
    safe_argv = executor._argv("prompt", None)
    separator = safe_argv.index("--")
    options = safe_argv[:separator]
    sandbox_index = options.index("-s")
    without_sandbox = options[:sandbox_index] + options[sandbox_index + 2:]
    executor._argv = lambda prompt, session_id: [
        *without_sandbox,
        "-c",
        'sandbox_mode="danger-full-access"',
        *safe_argv[separator:],
    ]

    with pytest.raises(WorkerError, match="sandbox_mode"):
        executor.run("prompt", context.project_dir, tmp_path / "attempt", retries=0)

    assert _calls(state) == []


@pytest.mark.parametrize(
    "overrides",
    (
        [
            '-cmodel_reasoning_effort="low"',
            '-cmodel_reasoning_effort="xhigh"',
        ],
        [
            '-c=model_reasoning_effort="low"',
            '-c=model_reasoning_effort="xhigh"',
        ],
        [
            '--config=model_reasoning_effort="low"',
            '--config=model_reasoning_effort="xhigh"',
        ],
    ),
)
def test_codex_glued_reasoning_effort_pairs_cannot_conflict(
    tmp_path, overrides
):
    stream = _jsonl(tmp_path / "stream.jsonl", [
        {"type": "thread.started", "thread_id": "bound"},
        {"type": "turn.completed", "usage": {"input_tokens": 1}},
    ])
    binary, config, state = _stub(tmp_path, [stream])
    context = _context(tmp_path, binary=binary, stub_config=config)
    executor = executor_for(context.route, "helper", context)
    safe_argv = executor._argv("prompt", None)
    separator = safe_argv.index("--")
    without_effort = []
    index = 0
    while index < separator:
        if (
            safe_argv[index] == "-c"
            and index + 1 < separator
            and safe_argv[index + 1].startswith("model_reasoning_effort=")
        ):
            index += 2
            continue
        without_effort.append(safe_argv[index])
        index += 1
    executor._argv = lambda prompt, session_id: [
        *without_effort, *overrides, *safe_argv[separator:]
    ]

    with pytest.raises(WorkerError, match="model_reasoning_effort"):
        executor.run("prompt", context.project_dir, tmp_path / "attempt", retries=0)

    assert _calls(state) == []


def test_codex_resumes_legacy_binding_without_client_key(tmp_path):
    """Bindings written before 3250939 retain their same-client resume path."""
    first_stream = _jsonl(tmp_path / "first.jsonl", [
        {"type": "thread.started", "thread_id": "bound"},
        {"type": "item.completed", "item": {"type": "agent_message", "text": "ok"}},
        {"type": "turn.completed", "usage": {"input_tokens": 10}},
    ])
    resumed_stream = _jsonl(tmp_path / "resumed.jsonl", [
        {"type": "thread.started", "thread_id": "bound"},
        {"type": "item.completed", "item": {"type": "agent_message", "text": "again"}},
        {"type": "turn.completed", "usage": {"input_tokens": 15}},
    ])
    binary, config, state = _stub(tmp_path, [first_stream, resumed_stream])
    context = _context(tmp_path, role="evaluator", binary=binary, stub_config=config)
    executor = executor_for(context.route, "evaluator", context)
    first = executor.run("first", context.project_dir, tmp_path / "attempt-1", retries=0)
    binding_path = executor.prepared.invocation_dir / "codex-session-bindings.json"
    payload = json.loads(binding_path.read_text())
    payload["sessions"]["bound"]["identity"].pop("client")
    binding_path.write_text(json.dumps(payload))
    resume_context = replace(
        context,
        resume_from=executor.prepared.invocation_dir,
        resume_artifact=first.resume_artifact,
    )

    second = executor_for(resume_context.route, "evaluator", resume_context).run(
        "second", resume_context.project_dir, tmp_path / "attempt-2", retries=0
    )

    assert second.session_id == "bound"
    assert "resume" in _calls(state)[1]["argv"]


def test_codex_refuses_binding_without_recorded_owner_digest(tmp_path):
    stream = _jsonl(tmp_path / "first.jsonl", [
        {"type": "thread.started", "thread_id": "bound"},
        {"type": "item.completed", "item": {"type": "agent_message", "text": "ok"}},
        {"type": "turn.completed", "usage": {"input_tokens": 10}},
    ])
    binary, config, _state = _stub(tmp_path, [stream])
    context = _context(tmp_path, role="evaluator", binary=binary, stub_config=config)
    executor = executor_for(context.route, "evaluator", context)
    result = executor.run("first", context.project_dir, tmp_path / "attempt-1", retries=0)
    binding_path = executor.prepared.invocation_dir / "codex-session-bindings.json"
    payload = json.loads(binding_path.read_text())
    payload["sessions"]["bound"]["identity"].pop("owner_config_digest")
    binding_path.write_text(json.dumps(payload))

    with pytest.raises(ExecutorRefused, match="different bound identity"):
        executor_for(
            context.route,
            "evaluator",
            replace(
                context,
                resume_from=executor.prepared.invocation_dir,
                resume_artifact=result.resume_artifact,
            ),
        )


def test_codex_resume_ignores_unrelated_owner_tui_edit(tmp_path):
    first_stream = _jsonl(tmp_path / "first.jsonl", [
        {"type": "thread.started", "thread_id": "bound"},
        {"type": "item.completed", "item": {"type": "agent_message", "text": "ok"}},
        {"type": "turn.completed", "usage": {"input_tokens": 10}},
    ])
    resumed_stream = _jsonl(tmp_path / "resumed.jsonl", [
        {"type": "thread.started", "thread_id": "bound"},
        {"type": "item.completed", "item": {"type": "agent_message", "text": "again"}},
        {"type": "turn.completed", "usage": {"input_tokens": 15}},
    ])
    binary, config, state = _stub(tmp_path, [first_stream, resumed_stream])
    context = _context(tmp_path, role="evaluator", binary=binary, stub_config=config)
    executor = executor_for(context.route, "evaluator", context)
    first = executor.run("first", context.project_dir, tmp_path / "attempt-1", retries=0)
    trust = Path(context.setup["codex_user_config_path"])
    trust.write_text(trust.read_text() + "\n[tui]\nnotifications = true\n")
    resume_context = replace(
        context,
        resume_from=executor.prepared.invocation_dir,
        resume_artifact=first.resume_artifact,
    )

    second = executor_for(resume_context.route, "evaluator", resume_context).run(
        "second", resume_context.project_dir, tmp_path / "attempt-2", retries=0
    )

    assert second.session_id == "bound"
    assert "resume" in _calls(state)[1]["argv"]


def test_codex_resume_records_but_does_not_enforce_owner_digest(tmp_path):
    first_stream = _jsonl(tmp_path / "first.jsonl", [
        {"type": "thread.started", "thread_id": "bound"},
        {"type": "item.completed", "item": {"type": "agent_message", "text": "ok"}},
        {"type": "turn.completed", "usage": {"input_tokens": 10}},
    ])
    resumed_stream = _jsonl(tmp_path / "resumed.jsonl", [
        {"type": "thread.started", "thread_id": "bound"},
        {"type": "item.completed", "item": {"type": "agent_message", "text": "again"}},
        {"type": "turn.completed", "usage": {"input_tokens": 15}},
    ])
    binary, config, _state = _stub(tmp_path, [first_stream, resumed_stream])
    context = _context(tmp_path, role="evaluator", binary=binary, stub_config=config)
    trust = Path(context.setup["codex_user_config_path"])
    trust.write_text(
        trust.read_text()
        + "\n[shell_environment_policy.set]\n"
        + 'NODE_REPL_TRUSTED_BROWSER_CLIENT_SHA256S = "first"\n'
    )
    executor = executor_for(context.route, "evaluator", context)
    first = executor.run("first", context.project_dir, tmp_path / "attempt-1", retries=0)
    binding_path = executor.prepared.invocation_dir / "codex-session-bindings.json"
    old_digest = json.loads(binding_path.read_text())["sessions"]["bound"][
        "identity"
    ]["owner_config_digest"]
    trust.write_text(trust.read_text().replace('= "first"', '= "second"'))
    resume_context = replace(
        context,
        resume_from=executor.prepared.invocation_dir,
        resume_artifact=first.resume_artifact,
    )

    executor_for(resume_context.route, "evaluator", resume_context).run(
        "second", resume_context.project_dir, tmp_path / "attempt-2", retries=0
    )

    new_digest = json.loads(binding_path.read_text())["sessions"]["bound"][
        "identity"
    ]["owner_config_digest"]
    assert new_digest != old_digest


def test_codex_bound_session_still_refuses_model_change(tmp_path):
    stream = _jsonl(tmp_path / "first.jsonl", [
        {"type": "thread.started", "thread_id": "bound"},
        {"type": "item.completed", "item": {"type": "agent_message", "text": "ok"}},
        {"type": "turn.completed", "usage": {"input_tokens": 10}},
    ])
    binary, config, _state = _stub(tmp_path, [stream])
    context = _context(tmp_path, role="evaluator", binary=binary, stub_config=config)
    executor = executor_for(context.route, "evaluator", context)
    executor.run("first", context.project_dir, tmp_path / "attempt-1", retries=0)
    executor.model = "different-model"

    with pytest.raises(ExecutorRefused, match="different bound identity"):
        executor._require_bound_session("bound")


@pytest.mark.parametrize(
    ("field", "value"),
    (
        ("configuration_identity_digest", "different-digest"),
        ("role", "helper"),
        ("project_identity", "different-project"),
        ("client", "claude-code"),
    ),
)
def test_codex_refuses_resume_session_rebound_in_store(tmp_path, field, value):
    stream = _jsonl(tmp_path / "first.jsonl", [
        {"type": "thread.started", "thread_id": "bound"},
        {"type": "item.completed", "item": {"type": "agent_message", "text": "ok"}},
        {"type": "turn.completed", "usage": {"input_tokens": 10}},
    ])
    binary, config, state = _stub(tmp_path, [stream])
    context = _context(tmp_path, role="evaluator", binary=binary, stub_config=config)
    executor = executor_for(context.route, "evaluator", context)
    result = executor.run("first", context.project_dir, tmp_path / "attempt-1", retries=0)
    binding_path = executor.prepared.invocation_dir / "codex-session-bindings.json"
    payload = json.loads(binding_path.read_text())
    payload["sessions"]["bound"]["identity"][field] = value
    binding_path.write_text(json.dumps(payload))

    with pytest.raises(ExecutorRefused, match="different bound identity"):
        executor_for(
            context.route,
            "evaluator",
            replace(
                context,
                resume_from=executor.prepared.invocation_dir,
                resume_artifact=result.resume_artifact,
            ),
        )

    assert len(_calls(state)) == 1


@pytest.mark.parametrize("damage", ("hook", "mcp", "model"))
def test_codex_refuses_each_project_native_drift(tmp_path, damage):
    context = _context(tmp_path)
    if damage == "hook":
        path = context.project_dir / ".codex" / "hooks.json"
        payload = json.loads(path.read_text())
        payload["hooks"]["PreToolUse"][0]["hooks"][0]["command"] = "drifted"
        path.write_text(json.dumps(payload) + "\n")
    else:
        path = context.project_dir / ".codex" / "config.toml"
        suffix = (
            '\n[mcp_servers.drift]\ncommand = "drift"\n'
            if damage == "mcp"
            else '\nmodel = "drifted-model"\n'
        )
        path.write_text(path.read_text() + suffix)
    context.role_capabilities["codex"]["helper"]["identity"] = configuration_identity(context)

    with pytest.raises(ExecutorRefused, match="project.*Codex|Codex.*project"):
        executor_for(context.route, "helper", context)


@pytest.mark.parametrize("damage", ("missing_row", "missing_hash"))
def test_codex_required_hook_trust_fails_closed(tmp_path, damage):
    context = _context(tmp_path)
    rows = context.setup["codex_hooks_list"]
    pre_tool = next(row for row in rows if row["eventName"] == "preToolUse")
    if damage == "missing_row":
        rows.remove(pre_tool)
    else:
        trust = Path(context.setup["codex_user_config_path"])
        body = trust.read_text()
        marker = f'[hooks.state.{json.dumps(pre_tool["key"])}]\n'
        start = body.index(marker)
        value_start = body.index("trusted_hash", start)
        value_end = body.index("\n", value_start)
        trust.write_text(body[:value_start] + body[value_end + 1:])

    with pytest.raises(ExecutorRefused, match="/hooks|trust"):
        executor_for(context.route, "helper", context)


def test_codex_required_hook_trust_matches_each_handler_key(tmp_path):
    context = _context(tmp_path)
    rows = context.setup["codex_hooks_list"]
    pre_tool = next(row for row in rows if row["eventName"] == "preToolUse")
    hooks_path = context.project_dir / ".codex" / "hooks.json"
    hooks = json.loads(hooks_path.read_text())
    handlers = hooks["hooks"]["PreToolUse"][0]["hooks"]
    handlers.append(dict(handlers[0]))
    hooks_path.write_text(json.dumps(hooks) + "\n")
    duplicate = dict(pre_tool)
    rows.append(duplicate)

    with pytest.raises(ExecutorRefused, match="key|hooks"):
        require_codex_trust(context.project_dir, context.setup)


def test_codex_user_config_change_is_disabled_without_changing_run_identity(tmp_path):
    context = _context(tmp_path)
    trust = Path(context.setup["codex_user_config_path"])
    identity = configuration_identity(context)
    trust.write_text(
        trust.read_text() + '\n[mcp_servers.late_server]\ncommand = "late"\n'
    )

    executor = executor_for(context.route, "helper", context)

    assert configuration_identity(context) == identity
    assert "mcp_servers.late_server.enabled=false" in executor._argv("prompt", None)


def test_codex_rechecks_owner_redirect_immediately_before_spawn(tmp_path):
    stream = _jsonl(tmp_path / "stream.jsonl", [
        {"type": "thread.started", "thread_id": "bound"},
        {"type": "turn.completed", "usage": {"input_tokens": 1}},
    ])
    binary, config, state = _stub(tmp_path, [stream])
    context = _context(tmp_path, binary=binary, stub_config=config)
    executor = executor_for(context.route, "helper", context)
    trust = Path(context.setup["codex_user_config_path"])
    trust.write_text('openai_base_url = "https://redirect.invalid"\n' + trust.read_text())

    with pytest.raises(ExecutorRefused, match="openai_base_url"):
        executor.run("prompt", context.project_dir, tmp_path / "attempt", retries=0)

    assert _calls(state) == []


def test_codex_session_binding_records_relevant_owner_config_digest(tmp_path):
    stream = _jsonl(tmp_path / "stream.jsonl", [
        {"type": "thread.started", "thread_id": "bound"},
        {"type": "item.completed", "item": {"type": "agent_message", "text": "ok"}},
        {"type": "turn.completed", "usage": {"input_tokens": 1}},
    ])
    binary, config, _state = _stub(tmp_path, [stream])
    context = _context(tmp_path, binary=binary, stub_config=config)
    executor = executor_for(context.route, "helper", context)

    executor.run("prompt", context.project_dir, tmp_path / "attempt", retries=0)

    payload = json.loads(
        (executor.prepared.invocation_dir / "codex-session-bindings.json").read_text()
    )
    digest = payload["sessions"]["bound"]["identity"]["owner_config_digest"]
    assert len(digest) == 64
    assert all(character in "0123456789abcdef" for character in digest)


def test_client_nonquota_errors_stay_failures():
    parsed = parse_codex_stream('{"type":"error","message":"ordinary"}')
    assert parsed.outcome is None
    assert parsed.result_seen is False
    assert parsed.diagnostics[0]["kind"] == "client_error"
