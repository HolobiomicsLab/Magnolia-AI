import copy
import hashlib
import json
import os
import shutil
import subprocess
import sys
import tomllib
from types import SimpleNamespace
from dataclasses import replace
from pathlib import Path

import pytest

from campaign_runner.cli import _memory_route
from campaign_runner.executors import ExecContext, ExecutorRefused, executor_for
from campaign_runner.executors.capabilities import load_role_capabilities
from campaign_runner.executors.codex import CodexExecutor
from campaign_runner.launch import (
    _hook_source_digest,
    configuration_identity,
    prepare_launch,
)
from campaign_runner.routing import ResolvedRoute
from compchem_memory.harness import RENDERERS
from compchem_memory.harness.neutral import NeutralConfig
from compchem_memory.harness.protocol import LaunchContext
from compchem_memory.hook.doorbell import process_prompt
from compchem_memory.hook.project import ProjectPaths


STRIPPED_CLIENT_ENV = {
    "CLAUDE_CONFIG_DIR": "CLAUDE-SENTINEL",
    "CODEX_HOME": "CODEX-HOME-SENTINEL",
    "VIBE_HOME": "VIBE-HOME-SENTINEL",
    "CODEX_API_KEY": "CODEX-KEY-SENTINEL",
    "ANTHROPIC_API_KEY": "ANTHROPIC-KEY-SENTINEL",
    "ANTHROPIC_AUTH_TOKEN": "ANTHROPIC-AUTH-SENTINEL",
    "OPENAI_API_KEY": "OPENAI-KEY-SENTINEL",
    "MISTRAL_API_KEY": "MISTRAL-KEY-SENTINEL",
}
PASSTHROUGH_CLIENT_ENV = {
    "MAGNOLIA_OPENAI_API_KEY": "MN-OAI-ALIAS-SENTINEL",
    "MAGNOLIA_MISTRAL_API_KEY": "MN-MIS-ALIAS-SENTINEL",
    "OPENROUTER_API_KEY": "OPENROUTER-KEY-SENTINEL",
    "DEEPSEEK_API_KEY": "DEEPSEEK-KEY-SENTINEL",
    "KIMI_PLAN_MAGNOLIA_API_KEY": "KIMI-PLAN-KEY-SENTINEL",
    "KIMI_API_KEY": "KIMI-KEY-SENTINEL",
    "ZHIPU_API_KEY": "ZHIPU-KEY-SENTINEL",
    "ZAI_API_KEY": "ZAI-KEY-SENTINEL",
    "MAGNOLIA_LLM_API_KEY": "MAGNOLIA-MEMORY-KEY-SENTINEL",
    "MAGNOLIA_CRITIC_API_KEY": "MAGNOLIA-CRITIC-KEY-SENTINEL",
}
PARENT_CLIENT_ENV = {**STRIPPED_CLIENT_ENV, **PASSTHROUGH_CLIENT_ENV}
ROOT = Path(__file__).parents[1]
ISOLATED_MEMORY_ARGS = [
    "-I",
    str(
        ROOT / "mcp-servers" / "compchem-memory" / "src"
        / "compchem_memory" / "hook" / "runner.py"
    ),
    "--memory-server",
]
WORKER_CAPABILITIES = (
    "native_read",
    "native_glob",
    "native_grep",
    "native_edit",
    "mcp:compchem-memory.memory_get_context",
    "mcp:compchem-memory.memory_search",
    "mcp:compchem-memory.memory_get_run_history",
)


def _fake_codex_client(tmp_path):
    binary = tmp_path / "codex"
    binary.write_text(
        "#!/bin/sh\n"
        "if [ \"$1\" = \"--version\" ]; then\n"
        "  printf '%s\\n' 'codex-cli 0.154.0'\n"
        "fi\n",
        encoding="utf-8",
    )
    binary.chmod(0o755)
    return binary


def _context(
    tmp_path,
    *,
    client="claude-code",
    role="helper",
    invocation="one",
    model="selected-model",
    **changes,
):
    project = tmp_path / "project"
    run = tmp_path / "run"
    project.mkdir(exist_ok=True)
    (run / "control").mkdir(parents=True, exist_ok=True)
    (run / "work").mkdir(parents=True, exist_ok=True)
    if client == "codex":
        config_dir = project / ".codex"
        config_dir.mkdir(exist_ok=True)
        (config_dir / "config.toml").write_text("model_provider = \"subscription\"\n")
        (config_dir / "hooks.json").write_text('{"hooks": {}}\n')
        codex_user_config = tmp_path / "codex-user.toml"
        codex_user_config.write_text("", encoding="utf-8")
    if client == "vibe":
        config_dir = project / ".vibe"
        config_dir.mkdir(exist_ok=True)
        (config_dir / "config.toml").write_text("disabled_tools = []\n")
        (config_dir / "hooks.toml").write_text("[[hooks]]\nname = \"fixture\"\n")
    route = ResolvedRoute(client, model, "family", client, role, "test")
    capabilities = set(changes.pop("capabilities", ()))
    evidence = {
        "accepted": True,
        "trusted": True,
        "stale": False,
        "identity": "pending",
        "discovery_dir": {
            "scope": "launch-project",
            "configuration": "run-owned settings",
        },
        "capabilities": sorted(capabilities),
        "model_isolation": {
            "accepted": True,
            "model": model,
            "identity": f"{client}-model-isolation-v1",
        },
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
        "setup": {
            "memory_enabled": True,
            **(
                {"codex_user_config_path": str(codex_user_config)}
                if client == "codex" else {}
            ),
        },
        "memory_service": {
            "transport": "stdio",
            "command": "/venv/bin/python",
            "args": ISOLATED_MEMORY_ARGS,
            "env_vars": ["MAGNOLIA_PROJECT_DIR"],
        },
        "invocation_id": invocation,
        "launch_argv": (
            (str(_fake_codex_client(tmp_path)),)
            if client == "codex" else (f"stub-{client}", "--headless")
        ),
        "role_capabilities": {client: {role: evidence}},
        "environment": (
            {
                "PATH": os.environ.get("PATH", ""),
                "CLAUDE_CODE_OAUTH_TOKEN": "member-subscription-token",
            }
            if client == "claude-code"
            else {"PATH": os.environ.get("PATH", ""), **PARENT_CLIENT_ENV}
        ),
    }
    values.update(changes)
    context = ExecContext(**values)
    evidence["identity"] = configuration_identity(context)
    return context


def _artifact_text(launch):
    return "\n".join(path.read_text(encoding="utf-8") for path in launch.files)


def _fake_claude_client(tmp_path):
    package = tmp_path / "npm-global" / "@anthropic-ai" / "claude-code"
    binary = package / "bin" / "claude.exe"
    binary.parent.mkdir(parents=True, exist_ok=True)
    binary.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
    binary.chmod(0o755)
    (package / "package.json").write_text(
        json.dumps({"name": "@anthropic-ai/claude-code", "version": "2.1.325"}),
        encoding="utf-8",
    )
    return binary


def _production_claude_context(tmp_path, *, role="helper", project_name="project", **changes):
    project = tmp_path / project_name
    run = tmp_path / f"run-{project_name}-{role}"
    project.mkdir(exist_ok=True)
    (run / "control").mkdir(parents=True, exist_ok=True)
    (run / "work").mkdir(exist_ok=True)
    binary = _fake_claude_client(tmp_path)
    installation = tmp_path / "snapshot" / "opencode_cc_mem"
    source = installation / "mcp-servers" / "compchem-memory" / "src"
    source.mkdir(parents=True, exist_ok=True)
    package = source / "compchem_memory"
    if not package.exists():
        shutil.copytree(
            ROOT / "mcp-servers" / "compchem-memory" / "src" / "compchem_memory",
            package,
            ignore=shutil.ignore_patterns("__pycache__"),
        )
    repository_venv = installation.parent / ".venv"
    python = repository_venv / "bin" / "python3"
    python.parent.mkdir(parents=True, exist_ok=True)
    if not python.exists():
        python.symlink_to(sys.executable)
    (repository_venv / "pyvenv.cfg").write_text(
        f"home = {Path(sys.executable).resolve().parent}\n",
        encoding="utf-8",
    )
    capabilities = copy.deepcopy(load_role_capabilities())
    identity = capabilities["claude-code"][role]["launch_identity"]["components"]
    identity["client_sha256"] = hashlib.sha256(binary.read_bytes()).hexdigest()
    identity["client_version"] = "2.1.325"
    route = ResolvedRoute(
        "claude-code", "haiku", "claude-haiku", "claude-code", role, "test"
    )
    role_cap = {
        "worker": 25,
        "reviewer": 8,
        "helper": 25,
        "evaluator": 1,
    }[role]
    if role == "helper":
        (project / "docs").mkdir(exist_ok=True)
    values = {
        "route": route,
        "role": role,
        "installation_root": installation,
        "workspace_root": tmp_path,
        "project_identity": f"identity-{project_name}",
        "project_dir": project,
        "run_dir": run,
        "control_dir": run / "control",
        "work_dir": run / "work",
        "python_executable": str(python),
        "tools_port": 8123,
        "setup": {"memory_enabled": True},
        "memory_service": _memory_route(route, str(python), installation),
        "invocation_id": f"{role}-{project_name}",
        "launch_argv": (str(binary),),
        "role_capabilities": capabilities,
        "environment": {"PATH": os.environ.get("PATH", "")},
        "role_cap": 30 if role == "worker" else role_cap,
        "governor_cap": 60,
        "required_capabilities": ("native_read",) if role == "helper" else (),
        "capability_read_roots": (project / "docs",) if role == "helper" else (),
    }
    values.update(changes)
    return ExecContext(**values)


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
    assert launch.env["MAGNOLIA_HOOK_POLICY_SHA256"] == launch.policy_digest
    assert launch.env["MAGNOLIA_DOORBELL"] == "0"


def test_member_oauth_survives_but_client_api_key_is_filtered(tmp_path):
    context = _context(
        tmp_path,
        environment={
            "CLAUDE_CODE_OAUTH_TOKEN": "member-subscription-token",
            "OPENAI_API_KEY": "task-zero-verification-key",
        },
    )

    launch = prepare_launch(context)

    assert launch.env["CLAUDE_CODE_OAUTH_TOKEN"] == "member-subscription-token"
    assert "OPENAI_API_KEY" not in launch.env


def test_codex_rendered_config_may_name_passthrough_key(tmp_path):
    launch = prepare_launch(
        _context(
            tmp_path,
            client="codex",
            role="worker",
            capabilities=WORKER_CAPABILITIES,
            memory_service={
                "transport": "stdio",
                "command": "/venv/bin/python",
                "args": ISOLATED_MEMORY_ARGS,
                "env_vars": ["OPENROUTER_API_KEY"],
            },
        )
    )

    assert "OPENROUTER_API_KEY" in _artifact_text(launch)
    assert launch.env["OPENROUTER_API_KEY"] == "OPENROUTER-KEY-SENTINEL"


def test_codex_identity_uses_version_command_and_rechecks_binary(tmp_path):
    binary = _fake_codex_client(tmp_path)
    context = _context(
        tmp_path,
        client="codex",
        role="worker",
        capabilities=WORKER_CAPABILITIES,
        launch_argv=(str(binary),),
    )
    launch = prepare_launch(context)

    assert launch.argv == (str(binary.resolve()),)
    assert launch.client_sha256 == hashlib.sha256(binary.read_bytes()).hexdigest()

    for path, body in launch.files.items():
        if path.name in {"config.toml", "hooks.json"}:
            target = context.project_dir / path.relative_to(launch.invocation_dir)
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(body)
    executor = CodexExecutor(
        launch, "selected-model", "worker", 30,
        user_config_path=Path(context.setup["codex_user_config_path"]),
    )
    binary.write_text("#!/bin/sh\nexit 1\n", encoding="utf-8")
    with pytest.raises(ExecutorRefused, match="changed before spawn"):
        executor._verify_client_bytes()


def test_codex_version_probe_uses_run_owned_home_and_launch_path(tmp_path):
    record = tmp_path / "version-environment.txt"
    binary = tmp_path / "codex-version-probe"
    binary.write_text(
        "#!/bin/sh\n"
        f'printf \'%s\\n%s\\n\' "$HOME" "$PATH" > "{record}"\n'
        '/bin/mkdir -p "$HOME/.codex/tmp/arg0"\n'
        "printf '%s\\n' 'codex-cli 0.154.0'\n",
        encoding="utf-8",
    )
    binary.chmod(0o755)
    context = _context(
        tmp_path,
        client="codex",
        launch_argv=(str(binary),),
        environment={"PATH": "/run-owned/bin"},
    )

    launch = prepare_launch(context)

    home, observed_path = record.read_text().splitlines()
    assert Path(home).parent == launch.invocation_dir.parent
    assert not Path(home).exists()
    assert observed_path == "/run-owned/bin"


def test_codex_normalized_launch_identity_names_selected_model(tmp_path):
    from campaign_runner import launch as module

    context = _context(
        tmp_path,
        client="codex",
        model="gpt-bound",
        python_executable=sys.executable,
        environment={
            "PATH": os.environ.get("PATH", ""),
            "MAGNOLIA_PYTHON": sys.executable,
        },
    )
    launch = prepare_launch(context)
    native = {
        str(path.relative_to(launch.invocation_dir)): body
        for path, body in launch.files.items()
        if str(path.relative_to(launch.invocation_dir)).startswith(".codex/")
    }

    normalized = module._normalized_launch_identity(
        context,
        launch.invocation_dir,
        launch.env,
        launch.files[launch.invocation_dir / "settings.json"],
        launch.files[launch.invocation_dir / "mcp.json"],
        native,
        launch.files[launch.invocation_dir / "policy.json"],
    )

    assert normalized["components"]["model"] == "gpt-bound"
    changed = copy.deepcopy(normalized)
    changed["components"]["model"] = "gpt-other"
    with pytest.raises(ExecutorRefused, match="identity mismatch: model"):
        module._verify_bound_launch_identity(
            {"launch_identity": normalized}, changed
        )


def test_codex_configuration_identity_ignores_owner_config(tmp_path):
    binary = _fake_codex_client(tmp_path)
    owner = tmp_path / "owner.toml"
    owner.write_text('[mcp_servers.owner]\ncommand = "first"\n', encoding="utf-8")
    context = _context(
        tmp_path,
        client="codex",
        launch_argv=(str(binary),),
        setup={"codex_user_config_path": str(owner)},
    )
    before = configuration_identity(context)
    owner.write_text('[mcp_servers.owner]\ncommand = "second"\n', encoding="utf-8")

    assert configuration_identity(context) == before


def test_codex_identity_is_path_independent_across_checkouts(tmp_path):
    from campaign_runner import launch as module

    identities = []
    for name in ("checkout-a", "checkout-b"):
        root = tmp_path / name
        root.mkdir()
        installation = root / "opencode_cc_mem"
        shutil.copytree(
            ROOT,
            installation,
            ignore=shutil.ignore_patterns("__pycache__", ".pytest_cache"),
        )
        python = root / ".venv" / "bin" / "python3"
        python.parent.mkdir(parents=True)
        python.symlink_to(sys.executable)
        (python.parents[1] / "pyvenv.cfg").write_text(
            f"home = {Path(sys.executable).resolve().parent}\n",
            encoding="utf-8",
        )
        binary = _fake_codex_client(root)
        route = ResolvedRoute(
            "codex", "selected-model", "family", "codex", "worker", "test"
        )
        context = _context(
            root,
            client="codex",
            role="worker",
            capabilities=WORKER_CAPABILITIES,
            launch_argv=(str(binary),),
            installation_root=installation,
            python_executable=str(python),
            memory_service=_memory_route(route, str(python), installation),
        )
        launch = prepare_launch(context)
        native = {
            str(path.relative_to(launch.invocation_dir)): body
            for path, body in launch.files.items()
            if str(path.relative_to(launch.invocation_dir)).startswith(".codex/")
        }
        normalized = module._normalized_launch_identity(
            context,
            launch.invocation_dir,
            launch.env,
            launch.files[launch.invocation_dir / "settings.json"],
            launch.files[launch.invocation_dir / "mcp.json"],
            native,
            launch.files[launch.invocation_dir / "policy.json"],
        )
        identities.append((configuration_identity(context), normalized["components"]))

    assert identities[0] == identities[1]


def test_codex_read_server_identity_is_stable_across_invocations(tmp_path):
    context = _context(
        tmp_path,
        client="codex",
        role="worker",
        capabilities=WORKER_CAPABILITIES,
        invocation="inv-a",
    )
    first = configuration_identity(context)
    second = configuration_identity(replace(context, invocation_id="inv-b"))

    assert first == second


def test_codex_read_server_receives_policy_digest_by_name(tmp_path):
    launch = prepare_launch(_context(
        tmp_path,
        client="codex",
        role="worker",
        capabilities=WORKER_CAPABILITIES,
    ))

    config = _artifact_text(launch)
    assert "MAGNOLIA_HOOK_POLICY_SHA256" in config
    assert launch.env["MAGNOLIA_HOOK_POLICY_SHA256"] == launch.policy_digest


def test_claude_vibe_opencode_launch_unchanged(tmp_path):
    for client in ("claude-code", "vibe"):
        client_root = tmp_path / client
        client_root.mkdir()
        launch = prepare_launch(
            _context(client_root, client=client, invocation=client)
        )
        assert "magnolia-read" not in _artifact_text(launch)

    launcher = (ROOT / "softwares" / "bin" / "magnolia").read_text(
        encoding="utf-8"
    )
    assert "magnolia-read" not in launcher


def test_parallel_roles_and_interactive_render_do_not_cross_contaminate(tmp_path):
    first = prepare_launch(
        _context(tmp_path, invocation="helper-1", model="helper-model")
    )
    second = prepare_launch(
        _context(
            tmp_path,
            client="vibe",
            role="evaluator",
            invocation="evaluator-1",
            model="evaluator-model",
        )
    )
    interactive_project = tmp_path / "interactive"
    interactive = RENDERERS["claude"](
        NeutralConfig([], frozenset({"bash"})),
        LaunchContext(
            root=ROOT,
            project="interactive",
            project_dir=interactive_project,
            python=sys.executable,
            tools_port=8123,
            model="interactive-model",
            setup={},
        ),
    )

    assert first.invocation_dir != second.invocation_dir
    assert first.policy_digest != second.policy_digest
    assert first.env["MAGNOLIA_MODEL"] == "helper-model"
    assert second.env["MAGNOLIA_MODEL"] == "evaluator-model"
    assert second.env["MAGNOLIA_ROLE"] == "evaluator"
    assert "helper" not in _artifact_text(second)
    assert interactive.argv[-2:] == ["--model", "interactive-model"]
    assert "interactive-model" not in _artifact_text(first)
    assert "interactive-model" not in _artifact_text(second)
    assert (tmp_path / "project" / ".vibe" / "config.toml").read_text() == (
        "disabled_tools = []\n"
    )


def test_resume_keeps_policy_identity(tmp_path):
    first = prepare_launch(_context(tmp_path))
    resumed = prepare_launch(_context(tmp_path, resume_from=first.invocation_dir))

    assert resumed.invocation_dir == first.invocation_dir
    assert resumed.policy_digest == first.policy_digest
    assert resumed.config_digest == first.config_digest
    assert resumed.required_capability_identity == first.required_capability_identity


def test_resume_refuses_tampered_policy_even_with_rewritten_manifest(tmp_path):
    first = prepare_launch(_context(tmp_path))
    policy_path = first.invocation_dir / "policy.json"
    manifest_path = first.invocation_dir / "launch.json"
    policy_path.chmod(0o644)
    policy = json.loads(policy_path.read_text(encoding="utf-8"))
    policy["deny_shell"] = False
    policy_path.write_text(json.dumps(policy, sort_keys=True) + "\n", encoding="utf-8")
    policy_path.chmod(0o444)
    manifest_path.chmod(0o644)
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    import hashlib

    manifest["artifacts"]["policy.json"] = hashlib.sha256(
        policy_path.read_bytes()
    ).hexdigest()
    manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
    manifest_path.chmod(0o444)

    with pytest.raises(ExecutorRefused, match="policy.json"):
        prepare_launch(_context(tmp_path, resume_from=first.invocation_dir))


def test_resume_refuses_copy_inside_work_tree(tmp_path):
    first = prepare_launch(_context(tmp_path))
    copied = tmp_path / "run" / "work" / "copied-invocation"
    shutil.copytree(first.invocation_dir, copied)

    with pytest.raises(ExecutorRefused, match="control.*executors"):
        prepare_launch(_context(tmp_path, resume_from=copied))


def test_resume_refuses_other_project_identity(tmp_path):
    first = prepare_launch(_context(tmp_path))

    with pytest.raises(ExecutorRefused, match="project_identity"):
        prepare_launch(
            _context(
                tmp_path,
                resume_from=first.invocation_dir,
                project_identity="other-project",
            )
        )


@pytest.mark.parametrize("replacement", ("symlink", "directory"))
def test_resume_refuses_non_regular_artifact(tmp_path, replacement):
    first = prepare_launch(_context(tmp_path))
    policy = first.invocation_dir / "policy.json"
    policy.chmod(0o644)
    body = policy.read_bytes()
    policy.unlink()
    if replacement == "symlink":
        target = tmp_path / "run" / "work" / "policy.json"
        target.write_bytes(body)
        target.chmod(0o444)
        policy.symlink_to(target)
    else:
        policy.mkdir(mode=0o444)

    with pytest.raises(ExecutorRefused, match="regular file"):
        prepare_launch(_context(tmp_path, resume_from=first.invocation_dir))


@pytest.mark.parametrize(
    ("evidence_change", "message"),
    (
        ({}, "missing"),
        ({"stale": True}, "stale"),
        ({"stale": None}, "stale"),
        ({"trusted": False}, "untrusted"),
    ),
)
def test_missing_stale_or_untrusted_launch_refused(tmp_path, evidence_change, message):
    context = _context(tmp_path)
    evidence = context.role_capabilities["claude-code"]["helper"]
    if evidence_change == {"stale": None}:
        evidence.pop("stale")
    elif evidence_change:
        evidence.update(evidence_change)
    else:
        context = _context(tmp_path, role_capabilities={})

    with pytest.raises(ExecutorRefused, match=message):
        prepare_launch(context)


@pytest.mark.parametrize("client", ("claude-code", "codex", "vibe"))
def test_effective_environment_strips_only_client_credentials(tmp_path, client):
    environment = {"PATH": os.environ.get("PATH", ""), **PARENT_CLIENT_ENV}
    if client == "claude-code":
        environment = {
            "PATH": os.environ.get("PATH", ""),
            "CLAUDE_CODE_OAUTH_TOKEN": "member-subscription-token",
            **PASSTHROUGH_CLIENT_ENV,
        }
    launch = prepare_launch(
        _context(tmp_path, client=client, invocation=client, environment=environment)
    )
    resumed = prepare_launch(
        _context(
            tmp_path,
            client=client,
            invocation=client,
            resume_from=launch.invocation_dir,
            environment=environment,
        )
    )
    combined = json.dumps(dict(launch.env), sort_keys=True) + _artifact_text(launch)

    for name, sentinel in STRIPPED_CLIENT_ENV.items():
        assert name not in launch.env
        assert name not in resumed.env
        assert sentinel not in combined
    for name, sentinel in PASSTHROUGH_CLIENT_ENV.items():
        if client == "claude-code":
            assert name not in launch.env
            assert name not in resumed.env
        else:
            assert launch.env[name] == sentinel
            assert resumed.env[name] == sentinel
    assert all(path.stat().st_mode & 0o222 == 0 for path in launch.files)


@pytest.mark.parametrize("name", tuple(STRIPPED_CLIENT_ENV))
def test_inherited_forbidden_value_cannot_survive_under_other_name(
    tmp_path, monkeypatch, name
):
    sentinel = STRIPPED_CLIENT_ENV[name]
    monkeypatch.setenv(name, sentinel)
    context = _context(
        tmp_path,
        environment={"PATH": os.environ.get("PATH", ""), name: sentinel},
        renderer_env={"COPIED_VALUE": sentinel},
    )

    if name.startswith("ANTHROPIC_"):
        with pytest.raises(ExecutorRefused, match=name):
            prepare_launch(context)
    else:
        launch = prepare_launch(context)
        assert name not in launch.env
        assert sentinel not in json.dumps(dict(launch.env)) + _artifact_text(launch)


@pytest.mark.parametrize("name", tuple(STRIPPED_CLIENT_ENV))
def test_forbidden_value_in_argv_is_refused(tmp_path, name):
    message = name if name.startswith("ANTHROPIC_") else "forbidden client environment"
    with pytest.raises(ExecutorRefused, match=message):
        prepare_launch(
            _context(
                tmp_path,
                launch_argv=("stub", "--credential", STRIPPED_CLIENT_ENV[name]),
                environment={name: STRIPPED_CLIENT_ENV[name]},
            )
        )


@pytest.mark.parametrize("name", tuple(STRIPPED_CLIENT_ENV))
def test_forbidden_value_in_resumed_artifact_is_refused(tmp_path, name):
    first = prepare_launch(_context(tmp_path))
    settings = first.invocation_dir / "settings.json"
    settings.chmod(0o644)
    settings.write_text(
        json.dumps({"copied_value": STRIPPED_CLIENT_ENV[name]}) + "\n",
        encoding="utf-8",
    )
    settings.chmod(0o444)

    with pytest.raises(ExecutorRefused):
        prepare_launch(_context(tmp_path, resume_from=first.invocation_dir))


def test_forbidden_path_value_is_not_matched_as_unrelated_path_prefix(tmp_path):
    config_root = tmp_path / "project" / "config-root"
    shared = config_root / "shared-skill.md"
    shared.parent.mkdir(parents=True)
    shared.write_text("skill\n", encoding="utf-8")
    context = _context(
        tmp_path,
        role="worker",
        capabilities=WORKER_CAPABILITIES,
        environment={
            "PATH": os.environ.get("PATH", ""),
            "CLAUDE_CONFIG_DIR": str(config_root),
        },
        shared_skill_files=(shared,),
    )

    launch = prepare_launch(context)

    assert str(shared.resolve()) in _artifact_text(launch)


def test_forbidden_path_value_is_not_matched_as_sibling_prefix(tmp_path):
    config_root = tmp_path / "project" / "cfg"
    config_root.mkdir(parents=True)
    shared = tmp_path / "project" / "cfg-shared" / "skill.md"
    shared.parent.mkdir(parents=True)
    shared.write_text("skill\n", encoding="utf-8")
    context = _context(
        tmp_path,
        role="worker",
        capabilities=WORKER_CAPABILITIES,
        environment={"CLAUDE_CONFIG_DIR": str(config_root)},
        shared_skill_files=(shared,),
    )

    launch = prepare_launch(context)

    assert str(shared.resolve()) in _artifact_text(launch)


@pytest.mark.parametrize("delimiter", ("/", "?", "&", "=", ":", "#", "\"", " "))
def test_forbidden_secret_value_uses_key_alphabet_delimiters(tmp_path, delimiter):
    secret = "sk-ant-SECRET_123"
    context = _context(
        tmp_path,
        role="worker",
        capabilities=WORKER_CAPABILITIES,
        environment={"ANTHROPIC_API_KEY": secret},
        memory_service={
            "transport": "http",
            "url": f"http://127.0.0.1:8765/mcp{delimiter}{secret}",
            "authorization_env": "MAGNOLIA_MEMORY_SERVICE_TOKEN",
        },
    )

    with pytest.raises(ExecutorRefused, match="ANTHROPIC_API_KEY"):
        prepare_launch(context)


def test_redirecting_secret_name_is_refused_even_inside_longer_value(tmp_path):
    secret = "sk-ant-SECRET_123"
    context = _context(
        tmp_path,
        role="worker",
        capabilities=WORKER_CAPABILITIES,
        environment={"ANTHROPIC_API_KEY": secret},
        memory_service={
            "transport": "http",
            "url": f"http://127.0.0.1:8765/mcp/{secret}X",
            "authorization_env": "MAGNOLIA_MEMORY_SERVICE_TOKEN",
        },
    )

    with pytest.raises(ExecutorRefused, match="ANTHROPIC_API_KEY"):
        prepare_launch(context)


def test_noninteractive_mention_writes_no_letter(tmp_path):
    context = _context(tmp_path, prompt="Please ask @literature for a source")

    launch = prepare_launch(context)
    project = tmp_path / "project"
    result = process_prompt(
        context.prompt,
        ProjectPaths(tmp_path, project, project / ".magnolia", "project", False),
        launch.env,
    )

    assert result.text == context.prompt
    assert result.letters == []
    assert not list(tmp_path.rglob("*.letter.md"))
    assert not list(tmp_path.rglob("*.task.md"))


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
            "authorization_env": "MAGNOLIA_MEMORY_SERVICE_TOKEN",
        },
        environment={"MAGNOLIA_MEMORY_SERVICE_TOKEN": "service-token"},
    )

    launch = prepare_launch(context)
    mcp = json.loads((launch.invocation_dir / "mcp.json").read_text(encoding="utf-8"))

    assert mcp["memory_service"] == {
        "transport": "http",
        "url": "http://127.0.0.1:9876/mcp",
        "authorization_env": "MAGNOLIA_MEMORY_SERVICE_TOKEN",
    }
    assert mcp["memory_service"]["url"].startswith("http://127.0.0.1:")
    assert "ANTHROPIC_API_KEY" not in launch.env
    assert "ANTHROPIC-KEY-SENTINEL" not in _artifact_text(launch)


def test_vibe_memory_url_and_bearer_are_run_owned(tmp_path):
    token_file = tmp_path / "memory-token"
    token_file.write_text("service-bearer-sentinel\n", encoding="utf-8")
    token_file.chmod(0o600)
    route = {
        "transport": "http",
        "url": "http://127.0.0.1:9876/mcp",
        "authorization_env": "MAGNOLIA_MEMORY_SERVICE_TOKEN",
        "token_file": str(token_file),
    }
    context = _context(
        tmp_path,
        client="vibe",
        role="worker",
        capabilities=WORKER_CAPABILITIES,
        memory_service=route,
    )

    launch = prepare_launch(context)
    config = tomllib.loads(
        (launch.invocation_dir / ".vibe/config.toml").read_text(encoding="utf-8")
    )
    rendered = config["mcp_servers"][0]
    mcp = json.loads((launch.invocation_dir / "mcp.json").read_text())

    assert rendered["transport"] == "http"
    assert rendered["url"] == route["url"]
    assert rendered["auth"] == {
        "type": "static",
        "api_key_env": "MAGNOLIA_MEMORY_SERVICE_TOKEN",
    }
    assert "headers" not in rendered
    assert "${" not in repr(rendered)
    assert launch.env["MAGNOLIA_MEMORY_SERVICE_TOKEN"] == "service-bearer-sentinel"
    memory_keys = set(PASSTHROUGH_CLIENT_ENV) - {"MAGNOLIA_CRITIC_API_KEY"}
    assert not (memory_keys & set(launch.env))
    assert "service-bearer-sentinel" not in _artifact_text(launch)
    assert "token_file" not in mcp["memory_service"]
    assert configuration_identity(context) != configuration_identity(
        replace(
            context,
            memory_service={**route, "url": "http://127.0.0.1:9877/mcp"},
        )
    )


def test_campaign_vibe_managed_route_acquires_project_service(
    tmp_path, monkeypatch
):
    token_file = tmp_path / "managed-token"
    token_file.write_text("managed-service-token\n", encoding="utf-8")
    token_file.chmod(0o600)
    handle = SimpleNamespace(
        route=lambda: {
            "transport": "http",
            "url": "http://127.0.0.1:43101/mcp",
            "authorization_env": "MAGNOLIA_MEMORY_SERVICE_TOKEN",
            "token_file": str(token_file),
        }
    )
    acquired = []
    monkeypatch.setattr(
        "compchem_memory.harness.memory_service.ensure_memory_service",
        lambda context: acquired.append(context) or handle,
    )
    context = _context(
        tmp_path,
        client="vibe",
        role="worker",
        capabilities=WORKER_CAPABILITIES,
        setup={"memory_provider": "openai", "memory_model": "test"},
        memory_service={"transport": "managed"},
        environment={"PATH": os.environ.get("PATH", ""), "OPENAI_API_KEY": "provider"},
    )

    launch = prepare_launch(context)

    assert len(acquired) == 1
    assert acquired[0].project_dir == context.project_dir
    assert launch.memory_service_handle is handle
    rendered = tomllib.loads(
        (launch.invocation_dir / ".vibe/config.toml").read_text(encoding="utf-8")
    )["mcp_servers"][0]
    assert rendered["url"] == "http://127.0.0.1:43101/mcp"
    assert rendered["auth"]["api_key_env"] == "MAGNOLIA_MEMORY_SERVICE_TOKEN"


def test_vibe_reviewer_without_memory_tools_does_not_acquire_managed_service(
    tmp_path, monkeypatch
):
    acquired = []
    monkeypatch.setattr(
        "compchem_memory.harness.memory_service.ensure_memory_service",
        lambda context: acquired.append(context),
    )
    context = _context(
        tmp_path,
        client="vibe",
        role="reviewer",
        capabilities=("native_read", "native_glob", "native_grep"),
        setup={},
        memory_service={"transport": "managed"},
        environment={"PATH": os.environ.get("PATH", "")},
    )

    launch = prepare_launch(context)

    assert acquired == []
    mcp = json.loads((launch.invocation_dir / "mcp.json").read_text())
    assert mcp["memory_service"] is None
    assert "MAGNOLIA_MEMORY_SERVICE_TOKEN" not in launch.env


def test_memory_role_without_route_is_refused(tmp_path):
    with pytest.raises(ExecutorRefused, match="memory.*route"):
        prepare_launch(
            _context(
                tmp_path,
                role="worker",
                capabilities=WORKER_CAPABILITIES,
                memory_service={},
            )
        )


def test_memory_stdio_route_refuses_nonisolated_module_launch(tmp_path):
    with pytest.raises(ExecutorRefused, match="isolated repository runner"):
        prepare_launch(
            _context(
                tmp_path,
                role="worker",
                capabilities=WORKER_CAPABILITIES,
                memory_service={
                    "transport": "stdio",
                    "command": "/venv/bin/python",
                    "args": ["-m", "compchem_memory.server"],
                    "env_vars": ["MAGNOLIA_PROJECT_DIR"],
                },
            )
        )


def test_memory_stdio_route_refuses_project_copy_of_runner(tmp_path):
    context = _context(
        tmp_path,
        role="worker",
        capabilities=WORKER_CAPABILITIES,
    )
    project_runner = (
        context.project_dir
        / "mcp-servers/compchem-memory/src/compchem_memory/hook/runner.py"
    )
    context.memory_service["args"] = ["-I", str(project_runner), "--memory-server"]

    with pytest.raises(ExecutorRefused, match="isolated repository runner"):
        prepare_launch(context)


@pytest.mark.parametrize(
    "api_key_name",
    ("ANTHROPIC_API_KEY", "OPENAI_API_KEY", "MISTRAL_API_KEY"),
)
def test_memory_route_depending_on_filtered_variable_is_refused(
    tmp_path, api_key_name
):
    with pytest.raises(ExecutorRefused, match=rf"filtered.*{api_key_name}"):
        prepare_launch(
            _context(
                tmp_path,
                role="worker",
                capabilities=WORKER_CAPABILITIES,
                memory_service={
                    "transport": "stdio",
                    "command": "/venv/bin/python",
                    "env_vars": [api_key_name],
                },
            )
        )


def test_codex_rendered_config_refuses_stripped_key_value(tmp_path):
    with pytest.raises(ExecutorRefused, match="forbidden client environment value"):
        prepare_launch(
            _context(
                tmp_path,
                client="codex",
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
                    "command": "/venv/bin/OPENAI-KEY-SENTINEL",
                    "args": ISOLATED_MEMORY_ARGS,
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


@pytest.mark.parametrize(
    ("path_changes", "message"),
    (
        ({"work_dir": "run"}, "canonical run/work"),
        ({"control_dir": "work-control"}, "canonical run/control"),
    ),
)
def test_run_control_and_work_paths_are_canonical(
    tmp_path, path_changes, message
):
    run = tmp_path / "run"
    changes = {
        key: run if value == "run" else run / "work" / "control"
        for key, value in path_changes.items()
    }

    with pytest.raises(ExecutorRefused, match=message):
        prepare_launch(
            _context(
                tmp_path,
                role="worker",
                capabilities=WORKER_CAPABILITIES,
                **changes,
            )
        )


@pytest.mark.parametrize("kind", ("directory", "home", "config-root", "outside"))
def test_shared_rule_and_skill_entries_are_bounded_files(tmp_path, kind):
    project = tmp_path / "project"
    if kind == "directory":
        shared = project
    elif kind == "home":
        shared = Path.home()
    elif kind == "config-root":
        shared = Path.home() / ".config"
    else:
        shared = tmp_path / "outside.md"
        shared.write_text("outside\n", encoding="utf-8")

    with pytest.raises(ExecutorRefused, match="shared.*file"):
        prepare_launch(
            _context(
                tmp_path,
                role="worker",
                capabilities=WORKER_CAPABILITIES,
                shared_skill_files=(shared,),
            )
        )


def test_shared_project_and_installation_files_are_allowed(tmp_path):
    project_file = tmp_path / "project" / "rule.md"
    project_file.parent.mkdir()
    project_file.write_text("project rule\n", encoding="utf-8")
    install_rule = ROOT / "rules" / "memory-setup.md"
    install_skill = ROOT / ".opencode" / "skills" / "campaign-protocol" / "SKILL.md"

    launch = prepare_launch(
        _context(
            tmp_path,
            role="worker",
            capabilities=WORKER_CAPABILITIES,
            shared_rule_files=(project_file, install_rule),
            shared_skill_files=(install_skill,),
        )
    )
    policy = json.loads((launch.invocation_dir / "policy.json").read_text())

    assert str(project_file.resolve()) in policy["canonical_read_roots"]
    assert str(install_rule.resolve()) in policy["canonical_read_roots"]
    assert str(install_skill.resolve()) in policy["canonical_read_roots"]


def test_configured_cap_can_exceed_default_but_governor_clamps(tmp_path):
    launch = prepare_launch(_context(tmp_path, role_cap=50, governor_cap=60))
    clamped = prepare_launch(
        _context(
            tmp_path,
            invocation="clamped",
            role_cap=75,
            governor_cap=60,
        )
    )

    assert json.loads((launch.invocation_dir / "settings.json").read_text())[
        "max_turns"
    ] == 50
    assert json.loads((clamped.invocation_dir / "settings.json").read_text())[
        "max_turns"
    ] == 60


@pytest.mark.parametrize("client", ("claude-code", "codex", "vibe"))
def test_discovered_configuration_must_match_evidence_identity(tmp_path, client):
    context = _context(tmp_path, client=client)
    evidence = context.role_capabilities[client]["helper"]
    evidence["identity"] = "wrong-configuration-digest"

    with pytest.raises(ExecutorRefused, match="configuration identity"):
        prepare_launch(context)


@pytest.mark.parametrize("role", ("worker", "reviewer", "helper", "evaluator"))
def test_launch_accepts_claude_rows_in_any_project(tmp_path, role):
    launches = [
        prepare_launch(
            _production_claude_context(tmp_path, role=role, project_name=name)
        )
        for name in ("alpha", "beta")
    ]

    assert all(launch.discovery_dir == launch.discovery_dir.resolve() for launch in launches)
    assert launches[0].discovery_dir != launches[1].discovery_dir


@pytest.mark.parametrize("role", ("worker", "reviewer", "helper", "evaluator"))
def test_production_memory_route_is_accepted_for_every_claude_role(tmp_path, role):
    context = _production_claude_context(tmp_path, role=role)
    route = _memory_route(
        context.route, context.python_executable, context.installation_root
    )

    prepare_launch(replace(context, memory_service=route))


@pytest.mark.parametrize(
    ("role", "tag", "tools"),
    (
        ("worker", "W1", ("Read", "Glob", "Grep", "Edit", "Write")),
        ("reviewer", "R1", ("Read", "Glob", "Grep")),
        ("helper", "H1", ("Read",)),
        ("evaluator", "E1", ()),
    ),
)
def test_claude_bound_role_matches_live_tool_shape(tmp_path, role, tag, tools):
    context = _production_claude_context(tmp_path, role=role)
    executor = executor_for(context.route, role, context)
    argv = executor._argv("offline shape check", None, executor.max_turns)
    policy = json.loads((executor.prepared.invocation_dir / "policy.json").read_text())
    expected = context.role_capabilities["claude-code"][role]["capabilities"]
    assert policy["effective_capabilities"] == expected
    if role in {"helper", "evaluator"}:
        assert argv[argv.index("--tools") + 1] == ",".join(tools)
        observed = json.loads(
            (ROOT / "tests/fixtures/executors/claude/live-gate" / tag / "04-stream-init.json").read_text()
        )
        assert observed["tools"] == list(tools)
        assert observed["mcp_servers"] == []
    else:
        assert "--tools" not in argv
    if tools:
        mcp = [name.replace("mcp:", "mcp__").replace(".", "__") for name in expected if name.startswith("mcp:")]
        assert argv[argv.index("--allowedTools") + 1:] == [*tools, *mcp]
    else:
        assert "--allowedTools" not in argv
        assert executor.max_turns == 1
    read_roots = [] if role == "evaluator" else [str(context.project_dir)]
    if role == "helper":
        read_roots = [str(context.project_dir / "docs")]
    assert policy["canonical_read_roots"] == read_roots
    mcp_config = json.loads((executor.prepared.invocation_dir / "claude-mcp.json").read_text())
    assert list(mcp_config["mcpServers"]) == (["compchem-memory"] if role == "worker" else [])


def test_claude_no_tools_helper_refuses_observed_read_binding(tmp_path):
    context = _production_claude_context(
        tmp_path, required_capabilities=(), capability_read_roots=()
    )

    with pytest.raises(ExecutorRefused, match="identity mismatch: settings"):
        prepare_launch(context)


@pytest.mark.parametrize("role", ("worker", "reviewer", "helper", "evaluator"))
def test_bound_identity_matches_two_independent_installations(tmp_path, monkeypatch, role):
    from campaign_runner import launch as module

    actual = module._normalized_launch_identity
    identities = []

    def capture(context, invocation, env, settings, mcp, native, policy):
        replacements = module._identity_replacements(context, invocation)
        bodies = (settings, mcp, policy, *native.values())
        for body in bodies:
            normalized = module._normalize_identity_body(body, replacements).decode()
            assert str(context.installation_root) not in normalized
            assert str(ROOT) not in normalized
            assert str(tmp_path) not in normalized
        identity = actual(context, invocation, env, settings, mcp, native, policy)
        identities.append(identity["components"])
        return identity

    monkeypatch.setattr(module, "_normalized_launch_identity", capture)
    launches = []
    for name in ("first", "second installation"):
        location = tmp_path / name
        location.mkdir()
        context = _production_claude_context(location, role=role)
        assert not (context.installation_root / "mcp-servers/compchem-memory/src/compchem_memory").is_symlink()
        launches.append(prepare_launch(context))
        assert identities[-1] == context.role_capabilities["claude-code"][role]["launch_identity"]["components"]
    assert identities[0] == identities[1]
    assert launches[0].required_capability_identity != launches[1].required_capability_identity


@pytest.mark.parametrize("selection", ("unset", "environment", "setup"))
def test_worker_memory_environment_references_are_defined(tmp_path, selection):
    environment = {"PATH": os.environ.get("PATH", "")}
    setup = {"memory_enabled": True}
    expected = {"MAGNOLIA_MEMORY_MODEL": "", "MAGNOLIA_MEMORY_PROVIDER": ""}
    if selection != "unset":
        expected = {"MAGNOLIA_MEMORY_MODEL": "memory-model", "MAGNOLIA_MEMORY_PROVIDER": "provider"}
    if selection == "environment":
        environment.update(expected)
    elif selection == "setup":
        setup.update(memory_model="memory-model", memory_provider="provider")
    context = _production_claude_context(
        tmp_path, role="worker", environment=environment, setup=setup
    )
    launch = prepare_launch(context)
    mcp = json.loads((launch.invocation_dir / "claude-mcp.json").read_text())
    references = mcp["mcpServers"]["compchem-memory"]["env"]
    assert set(references) == set(context.memory_service["env_vars"])
    assert set(references) <= launch.env.keys()
    expanded = {name: launch.env.get(name, value) for name, value in references.items()}
    assert not any("${" in value for value in expanded.values())
    assert {name: expanded[name] for name in expected} == expected


@pytest.mark.parametrize("change", ("missing", "extra"))
def test_claude_refuses_memory_route_with_one_environment_name_changed(
    tmp_path, change
):
    context = _production_claude_context(tmp_path, role="worker")
    route = _memory_route(
        context.route, context.python_executable, context.installation_root
    )
    if change == "missing":
        route["env_vars"] = route["env_vars"][:-1]
    else:
        route["env_vars"] = [*route["env_vars"], "MAGNOLIA_EXTRA"]

    with pytest.raises(ExecutorRefused, match="mcp"):
        prepare_launch(replace(context, memory_service=route))


def test_launch_refuses_on_binary_drift(tmp_path):
    context = _production_claude_context(tmp_path)
    capabilities = copy.deepcopy(context.role_capabilities)
    capabilities["claude-code"]["helper"]["launch_identity"]["components"][
        "client_sha256"
    ] = "0" * 64
    context = replace(context, role_capabilities=capabilities)

    with pytest.raises(ExecutorRefused, match="client_sha256"):
        prepare_launch(context)


def test_launch_refuses_on_rendered_shape_drift(tmp_path):
    context = _production_claude_context(tmp_path)
    capabilities = copy.deepcopy(context.role_capabilities)
    capabilities["claude-code"]["helper"]["launch_identity"]["components"][
        "settings"
    ] = "0" * 64
    context = replace(context, role_capabilities=capabilities)

    with pytest.raises(ExecutorRefused, match="settings"):
        prepare_launch(context)


def test_claude_worker_accepts_observed_stdio_and_refuses_static_http(tmp_path):
    prepare_launch(_production_claude_context(tmp_path, role="worker"))
    context = _production_claude_context(
        tmp_path,
        role="worker",
        project_name="http",
        memory_service={"transport": "http", "url": "http://127.0.0.1:8765/mcp"},
    )

    with pytest.raises(ExecutorRefused, match="authenticated"):
        prepare_launch(context)


def test_launch_binds_policy_env_names_from_effective_environment(tmp_path, monkeypatch):
    context = _production_claude_context(tmp_path)
    from campaign_runner import launch as launch_module

    original = launch_module._effective_environment

    def without_policy_digest(*args, **kwargs):
        env = original(*args, **kwargs)
        env.pop("MAGNOLIA_HOOK_POLICY_SHA256")
        return env

    monkeypatch.setattr(launch_module, "_effective_environment", without_policy_digest)
    with pytest.raises(ExecutorRefused, match="required_env"):
        prepare_launch(context)


def test_explicit_child_environment_keys_are_protected_from_their_source(
    tmp_path, monkeypatch
):
    context = _context(tmp_path, client="codex")
    from campaign_runner import launch as launch_module

    original = launch_module._base_explicit_environment

    def with_future_name(*args, **kwargs):
        explicit = original(*args, **kwargs)
        explicit["COMPCHEM_FUTURE_CONTRACT"] = "bound"
        return explicit

    monkeypatch.setattr(
        launch_module, "_base_explicit_environment", with_future_name
    )
    launch = prepare_launch(context)

    assert launch.env["COMPCHEM_FUTURE_CONTRACT"] == "bound"
    assert "COMPCHEM_FUTURE_CONTRACT" in launch.protected_env_names


def test_launch_refuses_renamed_policy_environment_name(tmp_path, monkeypatch):
    context = _production_claude_context(tmp_path)
    from campaign_runner import launch as launch_module

    original = launch_module._effective_environment

    def renamed_policy_digest(*args, **kwargs):
        env = original(*args, **kwargs)
        env["MAGNOLIA_HOOK_POLICY_DIGEST"] = env.pop(
            "MAGNOLIA_HOOK_POLICY_SHA256"
        )
        return env

    monkeypatch.setattr(launch_module, "_effective_environment", renamed_policy_digest)
    with pytest.raises(ExecutorRefused, match="required_env"):
        prepare_launch(context)


def test_launch_refuses_unselected_hook_interpreter(tmp_path):
    context = _production_claude_context(
        tmp_path,
        python_executable="/usr/bin/python3",
        environment={"PATH": os.environ.get("PATH", ""), "MAGNOLIA_PYTHON": "/selected/bin/python3"},
    )

    with pytest.raises(ExecutorRefused, match="hook interpreter"):
        prepare_launch(context)


@pytest.mark.parametrize("selection", (None, "same-target-different-path"))
@pytest.mark.parametrize("kind", ("base", "other-venv", "outside-symlink"))
def test_hook_interpreter_refuses_unselected_paths(tmp_path, kind, selection):
    if kind == "base":
        interpreter = Path(sys.executable).resolve()
    elif kind == "other-venv":
        interpreter = tmp_path / "venvX" / "bin" / "python3"
        interpreter.parent.mkdir(parents=True)
        interpreter.symlink_to(Path(sys.executable).resolve())
        (interpreter.parent.parent / "pyvenv.cfg").write_text("home = /base\n")
    else:
        interpreter = tmp_path / "outside-python"
        interpreter.symlink_to(sys.executable)
    context = _production_claude_context(
        tmp_path,
        python_executable=str(interpreter),
        environment={
            "PATH": os.environ.get("PATH", ""),
            **({"MAGNOLIA_PYTHON": sys.executable} if selection is not None else {}),
        },
    )

    with pytest.raises(ExecutorRefused, match="hook interpreter"):
        prepare_launch(context)


@pytest.mark.parametrize("selection", (None, "/selected/bin/python3"))
def test_hook_interpreter_accepts_repository_venv_with_any_override(tmp_path, selection):
    environment = {"MAGNOLIA_PYTHON": selection} if selection is not None else {}
    context = _production_claude_context(tmp_path, role="worker", environment=environment)

    launch = prepare_launch(context)

    assert launch.env["MAGNOLIA_PYTHON"] == context.python_executable


@pytest.mark.parametrize("kind", ("venv", "base", "symlink"))
def test_hook_interpreter_accepts_exact_explicit_override(tmp_path, kind):
    interpreter = Path(sys.executable)
    if kind == "base":
        interpreter = interpreter.resolve()
    elif kind == "symlink":
        interpreter = tmp_path / "selected python"
        interpreter.symlink_to(sys.executable)
    context = _production_claude_context(
        tmp_path, role="worker", python_executable=str(interpreter),
        environment={"MAGNOLIA_PYTHON": str(interpreter)},
    )
    context = replace(
        context,
        memory_service=_memory_route(
            context.route, str(interpreter), context.installation_root
        ),
    )

    launch = prepare_launch(context)

    assert launch.env["MAGNOLIA_PYTHON"] == str(interpreter)
    settings = json.loads((launch.invocation_dir / "claude-settings.json").read_text())
    assert str(interpreter) in settings["hooks"]["SessionStart"][0]["hooks"][0]["command"]


@pytest.mark.parametrize("kind", ("relative", "missing", "not-executable", "directory"))
def test_hook_interpreter_refuses_invalid_override(tmp_path, kind):
    interpreter = tmp_path / "selected-python"
    if kind == "relative":
        interpreter = Path("relative/bin/python3")
    elif kind == "not-executable":
        interpreter.write_text("#!/bin/sh\nexit 0\n")
        interpreter.chmod(0o644)
    elif kind == "directory":
        interpreter.mkdir()
    context = _production_claude_context(
        tmp_path, python_executable=str(interpreter),
        environment={"MAGNOLIA_PYTHON": str(interpreter)},
    )

    with pytest.raises(ExecutorRefused, match="hook interpreter"):
        prepare_launch(context)


def test_hook_interpreter_repository_venv_requires_config(tmp_path):
    context = _production_claude_context(tmp_path)
    (context.installation_root.parent / ".venv/pyvenv.cfg").unlink()

    with pytest.raises(ExecutorRefused, match="hook interpreter.*pyvenv.cfg"):
        prepare_launch(context)


def test_launch_refuses_changed_hook_source(tmp_path, monkeypatch):
    context = _production_claude_context(tmp_path)
    monkeypatch.setattr(
        "campaign_runner.launch._hook_source_digest", lambda _context: "0" * 64
    )

    with pytest.raises(ExecutorRefused, match="hook_source"):
        prepare_launch(context)


def test_hook_source_digest_covers_non_python_files_but_ignores_pycache(tmp_path):
    installation = tmp_path / "repo" / "opencode_cc_mem"
    source = (
        installation
        / "mcp-servers"
        / "compchem-memory"
        / "src"
        / "compchem_memory"
    )
    repository_source = (
        ROOT / "mcp-servers" / "compchem-memory" / "src" / "compchem_memory"
    )
    shutil.copytree(repository_source / "hook", source / "hook")
    (source / "harness").mkdir()
    shutil.copy2(
        repository_source / "harness" / "render_common.py",
        source / "harness" / "render_common.py",
    )
    context = _production_claude_context(tmp_path)
    context = replace(context, installation_root=installation)
    baseline = _hook_source_digest(context)
    pycache = source / "hook" / "__pycache__"
    pycache.mkdir(exist_ok=True)
    (pycache / "policy.cpython-312.pyc").write_bytes(b"forged")

    assert _hook_source_digest(context) == baseline
    (source / "hook" / "policy.cpython-312-darwin.so").write_bytes(b"extension")
    assert _hook_source_digest(context) != baseline


def test_claude_hook_is_isolated_to_repository_source_and_private_pycache(tmp_path):
    context = _production_claude_context(tmp_path)
    launch = prepare_launch(context)
    settings = json.loads((launch.invocation_dir / "claude-settings.json").read_text())
    command = settings["hooks"]["PreToolUse"][0]["hooks"][0]["command"]
    runner = (
        context.installation_root / "mcp-servers" / "compchem-memory" / "src"
        / "compchem_memory" / "hook" / "runner.py"
    )

    assert " -I " in command
    assert str(runner.resolve()) in command
    assert "PYTHONPYCACHEPREFIX" in launch.env
    assert Path(launch.env["PYTHONPYCACHEPREFIX"]).parent == launch.invocation_dir
    assert not Path(launch.env["PYTHONPYCACHEPREFIX"]).exists()


def test_launch_refuses_symlinked_hook_runner(tmp_path):
    context = _production_claude_context(tmp_path)
    runner = (
        context.installation_root / "mcp-servers" / "compchem-memory" / "src"
        / "compchem_memory" / "hook" / "runner.py"
    )
    target = tmp_path / "runner-target.py"
    target.write_bytes(runner.read_bytes())
    runner.unlink()
    runner.symlink_to(target)

    with pytest.raises(ExecutorRefused, match="hook_runner"):
        prepare_launch(context)


@pytest.mark.parametrize(
    ("role", "role_cap"),
    (("worker", 30), ("worker", 20), ("reviewer", 12)),
)
def test_launch_binds_cap_rule_not_concrete_value(tmp_path, role, role_cap):
    launch = prepare_launch(
        _production_claude_context(tmp_path, role=role, role_cap=role_cap)
    )
    settings = json.loads((launch.invocation_dir / "settings.json").read_text())
    assert settings["max_turns"] == min(role_cap, 60)


def test_claude_rehashes_absolute_client_immediately_before_spawn(tmp_path):
    context = _production_claude_context(tmp_path)
    executor = executor_for(context.route, "helper", context)
    executable = Path(executor.prepared.argv[0])
    assert executable.is_absolute()
    executable.write_text("#!/bin/sh\nexit 7\n", encoding="utf-8")
    executable.chmod(0o755)

    with pytest.raises(ExecutorRefused, match="changed before spawn"):
        executor.run("test", context.project_dir, tmp_path / "attempt", retries=0)


def test_claude_environment_is_allowlisted_and_redirects_are_named(tmp_path):
    allowed = {
        "HOME": str(tmp_path),
        "PATH": os.environ.get("PATH", ""),
        "CLAUDE_CODE_OAUTH_TOKEN": "subscription",
        "HTTPS_PROXY": "http://proxy.example",
        "SSL_CERT_FILE": "/certs/ca.pem",
        "XDG_STATE_HOME": str(tmp_path / "state"),
    }
    dropped = {
        "IGNORED": "x",
        **{f"XDG_{name}_HOME": str(tmp_path / name.lower()) for name in ("CONFIG", "CACHE", "DATA")},
    }
    launch = prepare_launch(
        _production_claude_context(tmp_path, environment={**allowed, **dropped})
    )
    assert all(launch.env[name] == value for name, value in allowed.items())
    assert not set(dropped) & set(launch.env)


    for name in ("ANTHROPIC_BASE_URL", "CLAUDE_CODE_USE_BEDROCK", "NODE_OPTIONS"):
        context = _production_claude_context(
            tmp_path,
            project_name=name.lower(),
            environment={**allowed, name: "redirect"},
        )
        with pytest.raises(ExecutorRefused, match=name):
            prepare_launch(context)


@pytest.mark.parametrize("source", ("environment", "renderer_env"))
def test_claude_xdg_state_home_reaches_hook(tmp_path, source):
    home = tmp_path / "home"
    home.mkdir()
    state = tmp_path / "xdg-state"
    environment = {"HOME": str(home), "PATH": os.environ.get("PATH", "")}
    renderer_env = {}
    (environment if source == "environment" else renderer_env)["XDG_STATE_HOME"] = str(state)
    context = _context(
        tmp_path, python_executable=sys.executable,
        environment=environment, renderer_env=renderer_env,
    )
    launch = prepare_launch(context)
    settings = json.loads((launch.invocation_dir / "claude-settings.json").read_text())
    command = settings["hooks"]["SessionStart"][0]["hooks"][0]["command"]
    result = subprocess.run(
        ["/bin/sh", "-c", command],
        input=json.dumps({"session_id": "xdg-check", "cwd": str(context.project_dir), "source": "startup"}),
        text=True, capture_output=True, cwd=context.project_dir, env=dict(launch.env), timeout=30,
    )

    assert result.returncode == 0, result.stderr
    binding = state / "magnolia/bindings/claude-xdg-check.json"
    assert binding.is_file(), (result.stdout, result.stderr)
    assert not (home / ".local/state/magnolia/bindings").exists()


@pytest.mark.parametrize("name", ("settings.json", "settings.local.json"))
def test_claude_refuses_project_settings_loaded_by_setting_sources(tmp_path, name):
    context = _production_claude_context(tmp_path)
    settings = context.run_dir / ".claude" / name
    settings.parent.mkdir()
    settings.write_text("{}\n", encoding="utf-8")

    with pytest.raises(ExecutorRefused, match=name):
        prepare_launch(context)


def test_client_version_reads_only_own_package_json(tmp_path):
    context = _production_claude_context(tmp_path, project_name="wrong-package")
    binary = Path(context.launch_argv[0])
    (tmp_path / "package.json").write_text(
        json.dumps({"name": "other", "version": "99.0.0"}), encoding="utf-8"
    )
    (binary.parent.parent / "package.json").write_text(
        json.dumps({"name": "other", "version": "2.1.325"}), encoding="utf-8"
    )
    with pytest.raises(ExecutorRefused, match="version metadata"):
        prepare_launch(context)


def test_real_claude_2_1_325_identity_when_installed(tmp_path):
    binary_name = shutil.which("claude")
    if binary_name is None:
        pytest.skip("Claude Code 2.1.325 is absent: executable not found")
    binary = Path(binary_name).resolve()
    package = binary.parent.parent / "package.json"
    try:
        metadata = json.loads(package.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        pytest.skip("Claude Code 2.1.325 is absent: package metadata unavailable")
    if metadata.get("name") != "@anthropic-ai/claude-code" or metadata.get("version") != "2.1.325":
        pytest.skip("Claude Code 2.1.325 is absent: installed version differs")
    expected = load_role_capabilities()["claude-code"]["helper"]["launch_identity"]
    assert hashlib.sha256(binary.read_bytes()).hexdigest() == expected["components"][
        "client_sha256"
    ]


def test_launch_refuses_non_launch_project_scope(tmp_path):
    context = _production_claude_context(tmp_path)
    capabilities = copy.deepcopy(context.role_capabilities)
    capabilities["claude-code"]["helper"]["discovery_dir"] = {
        "scope": "probe-project"
    }
    context = replace(context, role_capabilities=capabilities)

    with pytest.raises(ExecutorRefused, match="launch-project"):
        prepare_launch(context)


def test_resume_rechecks_identity(tmp_path):
    context = _production_claude_context(tmp_path)
    first = prepare_launch(context)
    capabilities = copy.deepcopy(context.role_capabilities)
    capabilities["claude-code"]["helper"]["launch_identity"]["components"][
        "client_version"
    ] = "different-version"

    with pytest.raises(ExecutorRefused, match="client_version"):
        prepare_launch(
            replace(
                context,
                role_capabilities=capabilities,
                resume_from=first.invocation_dir,
            )
        )


def test_codex_configuration_identity_uses_rendered_run_files_not_project_copy(tmp_path):
    context = _context(tmp_path, client="codex")
    identity = configuration_identity(context)
    hooks = tmp_path / "project" / ".codex" / "hooks.json"
    hooks.write_text('{"hooks": {"changed": true}}\n', encoding="utf-8")

    assert configuration_identity(context) == identity


def test_selected_model_requires_isolation_evidence(tmp_path):
    context = _context(tmp_path)
    evidence = context.role_capabilities["claude-code"]["helper"]
    evidence.pop("model_isolation")

    with pytest.raises(ExecutorRefused, match="model isolation"):
        prepare_launch(context)


def test_native_default_drops_inherited_model_binding(tmp_path):
    launch = prepare_launch(
        _context(
            tmp_path,
            model=None,
            environment={"PATH": "/bin", "MAGNOLIA_MODEL": "interactive-model"},
            renderer_env={"MAGNOLIA_MODEL": "renderer-model"},
        )
    )

    assert "MAGNOLIA_MODEL" not in launch.env


def test_explicit_empty_environment_remains_empty(tmp_path):
    launch = prepare_launch(_context(tmp_path, model=None, environment={}))

    assert "HOME" not in launch.env
    assert "PATH" not in launch.env


def test_invocation_mkdir_race_is_executor_refusal(tmp_path, monkeypatch):
    original_mkdir = Path.mkdir

    def racing_mkdir(path, *args, **kwargs):
        if path.name == "race":
            raise FileExistsError(path)
        return original_mkdir(path, *args, **kwargs)

    monkeypatch.setattr(Path, "mkdir", racing_mkdir)

    with pytest.raises(ExecutorRefused, match="already exists"):
        prepare_launch(_context(tmp_path, invocation="race"))


@pytest.mark.parametrize("entrypoint", ("magnolia-campaign", "magnolia-agents-daemon"))
def test_entrypoints_start_from_unrelated_cwd_without_pythonpath(tmp_path, entrypoint):
    env = {
        "HOME": os.environ["HOME"],
        "PATH": os.environ.get("PATH", ""),
        "MAGNOLIA_CAMPAIGN_NO_REEXEC": "1",
    }

    script = ROOT / "softwares" / "bin" / entrypoint
    probe = (
        "import runpy,sys; "
        f"sys.argv=[{str(script)!r},'--help']; "
        "\ntry: runpy.run_path(sys.argv[0], run_name='__main__')"
        "\nexcept SystemExit as exc:\n assert exc.code in (0, None)"
        "\nfrom campaign_runner import launch as module"
        "\nprint('LAUNCH_MODULE=' + module.__file__)"
    )

    result = subprocess.run(
        [sys.executable, "-c", probe],
        cwd=tmp_path,
        env=env,
        capture_output=True,
        text=True,
        timeout=10,
        check=False,
    )

    assert result.returncode == 0, result.stderr
    assert "usage:" in result.stdout.lower()
    module_line = next(
        line for line in result.stdout.splitlines() if line.startswith("LAUNCH_MODULE=")
    )
    assert Path(module_line.split("=", 1)[1]).is_relative_to(ROOT)


def test_daemon_help_does_not_require_pyyaml(tmp_path):
    script = ROOT / "softwares" / "bin" / "magnolia-agents-daemon"
    probe = (
        "import builtins,runpy,sys; "
        "original_import=builtins.__import__; "
        "\ndef import_without_yaml(name,*args,**kwargs):"
        "\n if name == 'yaml' or name.startswith('yaml.'):"
        "\n  raise ModuleNotFoundError(\"No module named 'yaml'\")"
        "\n return original_import(name,*args,**kwargs)"
        "\nbuiltins.__import__=import_without_yaml"
        f"\nsys.argv=[{str(script)!r},'--help']"
        "\nrunpy.run_path(sys.argv[0], run_name='__main__')"
    )

    result = subprocess.run(
        [sys.executable, "-c", probe],
        cwd=tmp_path,
        env={"HOME": os.environ["HOME"], "PATH": os.environ.get("PATH", "")},
        capture_output=True,
        text=True,
        timeout=10,
        check=False,
    )

    assert result.returncode == 0, result.stderr
    assert "usage:" in result.stdout.lower()


def test_claude_relative_xdg_state_home_is_refused(tmp_path):
    context = _production_claude_context(
        tmp_path,
        environment={"HOME": str(tmp_path), "PATH": os.environ.get("PATH", ""), "XDG_STATE_HOME": "work"},
    )
    with pytest.raises(ExecutorRefused, match="relative XDG_STATE_HOME"):
        prepare_launch(context)
