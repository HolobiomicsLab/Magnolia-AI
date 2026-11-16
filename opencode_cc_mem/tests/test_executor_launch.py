import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

from campaign_runner.executors import ExecContext, ExecutorRefused
from campaign_runner.launch import configuration_identity, prepare_launch
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
WORKER_CAPABILITIES = (
    "native_read",
    "native_glob",
    "native_grep",
    "native_edit",
    "mcp:compchem-memory.memory_get_context",
    "mcp:compchem-memory.memory_search",
    "mcp:compchem-memory.memory_get_run_history",
)


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
        "discovery_dir": str(project.resolve()),
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
        "setup": {"memory_enabled": True},
        "memory_service": {"transport": "http", "url": "http://127.0.0.1:8765/mcp"},
        "invocation_id": invocation,
        "launch_argv": (f"stub-{client}", "--headless"),
        "role_capabilities": {client: {role: evidence}},
        "environment": {"PATH": os.environ.get("PATH", ""), **PARENT_CLIENT_ENV},
    }
    values.update(changes)
    context = ExecContext(**values)
    evidence["identity"] = configuration_identity(context)
    return context


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
                "env_vars": ["OPENROUTER_API_KEY"],
            },
        )
    )

    assert "OPENROUTER_API_KEY" in _artifact_text(launch)
    assert launch.env["OPENROUTER_API_KEY"] == "OPENROUTER-KEY-SENTINEL"


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
    launch = prepare_launch(_context(tmp_path, client=client, invocation=client))
    resumed = prepare_launch(
        _context(tmp_path, client=client, invocation=client, resume_from=launch.invocation_dir)
    )
    combined = json.dumps(dict(launch.env), sort_keys=True) + _artifact_text(launch)

    for name, sentinel in STRIPPED_CLIENT_ENV.items():
        assert name not in launch.env
        assert name not in resumed.env
        assert sentinel not in combined
    for name, sentinel in PASSTHROUGH_CLIENT_ENV.items():
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
        environment=None,
        renderer_env={"COPIED_VALUE": sentinel},
    )

    with pytest.raises(ExecutorRefused, match="forbidden client environment"):
        prepare_launch(context)


@pytest.mark.parametrize("name", tuple(STRIPPED_CLIENT_ENV))
def test_forbidden_value_in_argv_is_refused(tmp_path, name):
    with pytest.raises(ExecutorRefused, match="forbidden client environment"):
        prepare_launch(
            _context(
                tmp_path,
                launch_argv=("stub", "--credential", STRIPPED_CLIENT_ENV[name]),
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
        },
    )

    with pytest.raises(ExecutorRefused, match="forbidden client environment value"):
        prepare_launch(context)


def test_forbidden_secret_value_is_not_matched_inside_longer_key_token(tmp_path):
    secret = "sk-ant-SECRET_123"
    context = _context(
        tmp_path,
        role="worker",
        capabilities=WORKER_CAPABILITIES,
        environment={"ANTHROPIC_API_KEY": secret},
        memory_service={
            "transport": "http",
            "url": f"http://127.0.0.1:8765/mcp/{secret}X",
        },
    )

    launch = prepare_launch(context)

    assert f"{secret}X" in _artifact_text(launch)


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
        },
    )

    launch = prepare_launch(context)
    mcp = json.loads((launch.invocation_dir / "mcp.json").read_text(encoding="utf-8"))

    assert mcp["memory_service"] == {
        "transport": "http",
        "url": "http://127.0.0.1:9876/mcp",
    }
    assert mcp["memory_service"]["url"].startswith("http://127.0.0.1:")
    assert "ANTHROPIC_API_KEY" not in launch.env
    assert "ANTHROPIC-KEY-SENTINEL" not in _artifact_text(launch)


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
                    "command": "/venv/bin/python",
                    "args": ["--api-key", "OPENAI-KEY-SENTINEL"],
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


def test_project_configuration_change_invalidates_evidence(tmp_path):
    context = _context(tmp_path, client="codex")
    hooks = tmp_path / "project" / ".codex" / "hooks.json"
    hooks.write_text('{"hooks": {"changed": true}}\n', encoding="utf-8")

    with pytest.raises(ExecutorRefused, match="configuration identity"):
        prepare_launch(context)


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
