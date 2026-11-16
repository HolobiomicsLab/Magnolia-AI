import json
import tomllib

from compchem_memory.harness import RENDERERS
from compchem_memory.harness.neutral import McpServer, NeutralConfig
from compchem_memory.harness.ownership import is_magnolia_hook_command
from compchem_memory.harness.render_codex import _servers, render


def _dir(context):
    return context.project_dir / ".codex"


def _policy(context):
    return context.project_dir / ".magnolia" / "harness" / "codex" / "policy.json"


def test_codex_renderer_is_registered_with_its_binary():
    assert RENDERERS["codex"] is render
    assert render.binary == "codex"


def test_codex_argv(make_render):
    neutral, context = make_render(model="gpt-x")
    assert render(neutral, context).argv == [
        "codex", "-C", str(context.project_dir), "-s", "workspace-write", "-m", "gpt-x",
    ]
    neutral, context = make_render()
    assert "-m" not in render(neutral, context).argv


def test_codex_policy_blocks_shell_rendered(make_render):
    neutral, context = make_render()
    launch = render(neutral, context)
    hooks = json.loads(launch.files[_dir(context) / "hooks.json"])["hooks"]

    assert json.loads(launch.files[_policy(context)]) == {"deny_tools": ["bash"]}
    assert launch.env == {"MAGNOLIA_HOOK_POLICY": str(_policy(context))}
    (hook,) = hooks["PreToolUse"][0]["hooks"]
    assert hook["type"] == "command"
    assert is_magnolia_hook_command(hook["command"])
    assert hook["command"].endswith(" codex pre-tool")
    assert hook["timeout"] == 30
    assert set(hooks) == {
        "SessionStart", "UserPromptSubmit", "PreToolUse", "PostToolUse", "Stop",
    }
    assert hooks["PostToolUse"][0]["hooks"][0]["timeout"] == 10
    assert "async" not in json.dumps(hooks)


def test_codex_config_has_no_bypass_and_no_codex_home(make_render):
    neutral, context = make_render(model="m")
    launch = render(neutral, context)
    text = " ".join(launch.argv) + " ".join(launch.env) + b" ".join(
        launch.files.values()
    ).decode()
    for forbidden in (
        "--dangerously", "--auto-approve", "--trust", "CODEX_HOME", "CLAUDE_CONFIG_DIR",
        "VIBE_HOME", "bypass",
    ):
        assert forbidden not in text
    assert launch.links == {
        context.project_dir / ".agents" / "skills" / "alpha":
            context.root / ".opencode" / "skills" / "alpha",
    }


def test_codex_config_toml_servers(make_render):
    neutral, context = make_render()
    config = tomllib.loads(
        render(neutral, context).files[_dir(context) / "config.toml"].decode()
    )["mcp_servers"]

    assert config["compchem-tools"] == {
        "url": "http://127.0.0.1:8123/mcp", "tool_timeout_sec": 660,
    }
    memory = config["compchem-memory"]
    assert memory["command"] == "/venv/bin/python"
    assert memory["args"] == ["-m", "compchem_memory.server"]
    assert memory["tool_timeout_sec"] == 660
    assert memory["env"]["MAGNOLIA_MEMORY_MODEL"] == "mem-model"
    assert "OPENAI_API_KEY" in memory["env_vars"]
    assert "MAGNOLIA_CRITIC_API_KEY" in memory["env_vars"]
    assert "MAGNOLIA_OPENROUTER_PROVIDER" in memory["env_vars"]
    assert "MAGNOLIA_MEMORY_PROVIDER" in memory["env_vars"]
    assert "MAGNOLIA_LLM_PROVIDER" in memory["env_vars"]
    assert "MAGNOLIA_LLM_MODEL" in memory["env_vars"]
    assert "ANTHROPIC_BASE_URL" in memory["env_vars"]
    assert "OPENROUTER_BASE_URL" in memory["env_vars"]

    neutral, context = make_render(setup={})
    config = tomllib.loads(
        render(neutral, context).files[_dir(context) / "config.toml"].decode()
    )["mcp_servers"]
    assert "compchem-memory" not in config


def test_codex_remote_server_never_gets_stdio_env_fields():
    neutral = NeutralConfig(servers=[McpServer(
        name="compchem-memory", kind="remote", command=None,
        url="https://memory.example/mcp", env={"SHOULD_NOT_RENDER": "value"},
        timeout_ms=1000, enabled=True,
    )], deny_tools=frozenset())

    server = _servers(neutral)["compchem-memory"]

    assert server == {
        "url": "https://memory.example/mcp",
        "tool_timeout_sec": 1,
    }


def test_codex_config_never_renders_forwarded_secret_values(make_render, monkeypatch):
    monkeypatch.setenv("OPENAI_API_KEY", "sentinel-secret-value")
    neutral, context = make_render()
    launch = render(neutral, context)

    assert all(b"sentinel-secret-value" not in content for content in launch.files.values())


def test_codex_rules_are_developer_instructions_and_member_value_wins(make_render):
    neutral, context = make_render()
    path = _dir(context) / "config.toml"
    config = tomllib.loads(render(neutral, context).files[path].decode())
    assert config["developer_instructions"].startswith("<!-- magnolia-rules -->")
    assert "rule a" in config["developer_instructions"]
    assert "AGENTS BODY" not in config["developer_instructions"]

    path.parent.mkdir(parents=True)
    path.write_text('developer_instructions = "member instructions"\n', encoding="utf-8")
    launch = render(neutral, context)
    assert tomllib.loads(launch.files[path].decode())["developer_instructions"] == "member instructions"
    assert any("developer_instructions" in conflict for conflict in launch.conflicts)


def test_codex_developer_rules_use_codex_tool_names(make_render):
    neutral, context = make_render()
    (context.root / "rules" / "a.md").write_text(
        "Use compchem-tools_run_shell and compchem-memory_memory_search.\n\n"
        "## Tool names in other clients\nDrop this.\n\n## Keep\nKept.\n",
        encoding="utf-8",
    )

    config = tomllib.loads(render(neutral, context).files[_dir(context) / "config.toml"].decode())
    rules = config["developer_instructions"]

    assert "mcp__compchem_tools__run_shell" in rules
    assert "mcp__compchem_memory__memory_search" in rules
    assert "Tool names in other clients" not in rules


def test_codex_preserves_member_entries_and_is_idempotent(make_render):
    neutral, context = make_render()
    folder = _dir(context)
    folder.mkdir(parents=True)
    (folder / "config.toml").write_text(
        'model = "member"\n\n[mcp_servers.mine]\ncommand = "mine"\n\n'
        '[mcp_servers.compchem-memory]\ncommand = "stale"\n', encoding="utf-8",
    )
    member_hook = {"hooks": [{"type": "command", "command": "member-hook"}]}
    stale = {"hooks": [{"type": "command", "command": "py -m compchem_memory.hook codex pre-tool"}]}
    (folder / "hooks.json").write_text(
        json.dumps({"hooks": {"PreToolUse": [member_hook, stale], "Notify": [member_hook]}}),
        encoding="utf-8",
    )

    def apply(launch):
        for path, content in launch.files.items():
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(content)

    first = render(neutral, context)
    apply(first)
    config = tomllib.loads((folder / "config.toml").read_text())
    hooks = json.loads((folder / "hooks.json").read_text())["hooks"]
    assert config["model"] == "member"
    assert config["mcp_servers"]["mine"] == {"command": "mine"}
    assert config["mcp_servers"]["compchem-memory"]["command"] == "/venv/bin/python"
    assert hooks["Notify"] == [member_hook]
    assert hooks["PreToolUse"][0] == member_hook
    assert len(hooks["PreToolUse"]) == 2
    commands = [
        hook["command"]
        for group in hooks["PreToolUse"]
        for hook in group["hooks"]
    ]
    assert sum(is_magnolia_hook_command(command) for command in commands) == 1
    assert commands[-1].endswith(" codex pre-tool")

    before = {path: path.read_bytes() for path in (folder / "config.toml", folder / "hooks.json")}
    second = render(neutral, context)
    apply(second)
    assert {path: path.read_bytes() for path in before} == before


def test_codex_reconciles_owned_server_transport_sets_and_is_idempotent(make_render):
    neutral, context = make_render()
    folder = _dir(context)
    folder.mkdir(parents=True)
    path = folder / "config.toml"
    path.write_text(
        '[mcp_servers.compchem-tools]\ncommand = "old-local"\n'
        'args = ["--stdio"]\nenv = { OLD = "1" }\nmember_field = "keep-tools"\n\n'
        '[mcp_servers.compchem-memory]\nurl = "https://old.example/mcp"\n'
        'bearer_token_env_var = "OLD_TOKEN"\nhttp_headers = { X_Old = "1" }\n'
        'env_http_headers = { X_Env = "OLD_HEADER" }\nmember_field = "keep-memory"\n',
        encoding="utf-8",
    )

    first = render(neutral, context)
    path.write_bytes(first.files[path])
    servers = tomllib.loads(path.read_text(encoding="utf-8"))["mcp_servers"]

    assert servers["compchem-tools"] == {
        "url": "http://127.0.0.1:8123/mcp",
        "tool_timeout_sec": 660,
        "member_field": "keep-tools",
    }
    memory = servers["compchem-memory"]
    assert memory["command"] == "/venv/bin/python"
    assert memory["args"] == ["-m", "compchem_memory.server"]
    assert memory["env"]["MAGNOLIA_MEMORY_MODEL"] == "mem-model"
    assert memory["tool_timeout_sec"] == 660
    assert memory["member_field"] == "keep-memory"
    assert not {
        "url", "bearer_token_env_var", "http_headers", "env_http_headers",
    }.intersection(memory)
    before = path.read_bytes()
    second = render(neutral, context)
    path.write_bytes(second.files[path])
    assert path.read_bytes() == before


def test_codex_removes_stale_env_when_owned_stdio_server_env_becomes_empty(make_render):
    neutral, context = make_render()
    memory = next(server for server in neutral.servers if server.name == "compchem-memory")
    memory.env = {}
    folder = _dir(context)
    folder.mkdir(parents=True)
    path = folder / "config.toml"
    path.write_text(
        '[mcp_servers.compchem-memory]\ncommand = "old"\nenv = { OLD = "stale" }\n',
        encoding="utf-8",
    )

    config = tomllib.loads(render(neutral, context).files[path].decode())

    assert "env" not in config["mcp_servers"]["compchem-memory"]


def test_codex_reports_conflicts_and_toml_comment_backup(make_render):
    neutral, context = make_render()
    folder = _dir(context)
    folder.mkdir(parents=True)
    original = b'# my notes\n[mcp_servers.mine]\ncommand = "mine"\n'
    (folder / "config.toml").write_bytes(original)
    launch = render(neutral, context)
    assert launch.files[folder / "config.toml.magnolia-backup"] == original
    assert launch.conflicts == []

    (folder / "hooks.json").write_text(
        json.dumps({"hooks": [{"name": "x"}]}), encoding="utf-8"
    )
    # A non-dict-shaped hooks table in the member file is reported, not clobbered.
    (folder / "config.toml").write_text(
        '[mcp_servers.perspicacite]\ncommand = "member-owned-name"\n', encoding="utf-8",
    )
    launch = render(neutral, context)
    assert "perspicacite" in launch.files[folder / "config.toml"].decode()


def test_codex_hooks_map_every_native_event_to_its_neutral_event(make_render):
    neutral, context = make_render(python="/my venv/bin/python")
    hooks = json.loads(
        render(neutral, context).files[_dir(context) / "hooks.json"]
    )["hooks"]
    expected = {
        "SessionStart": "session-start", "UserPromptSubmit": "prompt",
        "PreToolUse": "pre-tool", "PostToolUse": "post-tool", "Stop": "turn-end",
    }
    assert set(hooks) == set(expected)
    for event, name in expected.items():
        (group,) = hooks[event]
        (hook,) = group["hooks"]
        assert hook["type"] == "command"
        assert is_magnolia_hook_command(hook["command"])
        assert hook["command"].endswith(f" codex {name}")
        assert hook["timeout"] == (10 if name == "post-tool" else 30)


def test_codex_mixed_matcher_group_keeps_member_hook_and_runs_ours_once(make_render):
    neutral, context = make_render()
    folder = _dir(context)
    folder.mkdir(parents=True)
    member = {"type": "command", "command": "member-hook"}
    ours = {"type": "command", "command": "py -m compchem_memory.hook codex pre-tool"}
    (folder / "hooks.json").write_text(
        json.dumps({"hooks": {"PreToolUse": [{"matcher": "Bash", "hooks": [member, ours]}]}}),
        encoding="utf-8",
    )

    def apply(launch):
        for path, content in launch.files.items():
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(content)

    apply(render(neutral, context))
    first = (folder / "hooks.json").read_bytes()
    groups = json.loads(first)["hooks"]["PreToolUse"]
    commands = [h["command"] for g in groups for h in g["hooks"]]
    assert groups[0] == {"matcher": "Bash", "hooks": [member]}
    assert commands.count("member-hook") == 1
    assert sum(is_magnolia_hook_command(command) for command in commands) == 1
    apply(render(neutral, context))
    assert (folder / "hooks.json").read_bytes() == first


def test_codex_migrates_legacy_hook_to_isolated_runner(make_render):
    neutral, context = make_render()
    folder = _dir(context)
    folder.mkdir(parents=True)
    member = {"type": "command", "command": "member-hook"}
    legacy = {
        "type": "command",
        "command": "python -m compchem_memory.hook codex pre-tool",
    }
    (folder / "hooks.json").write_text(
        json.dumps({"hooks": {"PreToolUse": [{"hooks": [member, legacy]}]}}),
        encoding="utf-8",
    )

    launch = render(neutral, context)
    hooks = json.loads(launch.files[folder / "hooks.json"])["hooks"]["PreToolUse"]
    commands = [hook["command"] for group in hooks for hook in group["hooks"]]

    assert commands.count("member-hook") == 1
    assert sum(is_magnolia_hook_command(command) for command in commands) == 1
    assert all("-m compchem_memory.hook" not in command for command in commands)
    assert any("compchem_memory/hook/runner.py" in command for command in commands)
