import json

from compchem_memory.harness import RENDERERS
from compchem_memory.harness.ownership import is_magnolia_hook_command
from compchem_memory.harness.render_claude import render


def _harness(context):
    return context.project_dir / ".magnolia" / "harness" / "claude"


def test_claude_renderer_is_registered_with_its_binary():
    assert RENDERERS["claude"] is render
    assert render.binary == "claude"


def test_claude_argv_keeps_project_settings(make_render):
    neutral, context = make_render(model="sonnet")
    launch = render(neutral, context)
    folder = _harness(context)

    assert launch.argv == [
        "claude", "--setting-sources", "project,local",
        "--settings", str(folder / "settings.json"),
        "--mcp-config", str(folder / "mcp.json"), "--strict-mcp-config",
        "--disallowedTools", "Bash",
        "--append-system-prompt-file", str(folder / "system-prompt.md"),
        "--model", "sonnet",
    ]
    assert "user" not in launch.argv[2].split(",")
    assert context.project_dir / ".claude" / "settings.json" not in launch.files
    assert all(folder in path.parents for path in launch.files)


def test_claude_omits_model_flag_without_a_model(make_render):
    neutral, context = make_render()
    assert "--model" not in render(neutral, context).argv


def test_memory_disabled_drops_memory_server(make_render):
    neutral, context = make_render(setup={})
    servers = json.loads(
        render(neutral, context).files[_harness(context) / "mcp.json"]
    )["mcpServers"]
    assert set(servers) == {"compchem-tools", "perspicacite"}

    neutral, context = make_render()
    servers = json.loads(
        render(neutral, context).files[_harness(context) / "mcp.json"]
    )["mcpServers"]
    assert set(servers) == {"compchem-tools", "compchem-memory", "perspicacite"}


def test_claude_mcp_shapes(make_render):
    neutral, context = make_render()
    servers = json.loads(
        render(neutral, context).files[_harness(context) / "mcp.json"]
    )["mcpServers"]

    assert servers["compchem-tools"] == {"type": "http", "url": "http://127.0.0.1:8123/mcp"}
    memory = servers["compchem-memory"]
    assert memory["command"] == "/venv/bin/python"
    assert memory["args"] == ["-m", "compchem_memory.server"]
    assert memory["env"]["MAGNOLIA_MEMORY_MODEL"] == "mem-model"
    assert "type" not in memory


def test_claude_settings_hooks_cover_every_event(make_render):
    neutral, context = make_render(python="/my venv/bin/python")
    hooks = json.loads(
        render(neutral, context).files[_harness(context) / "settings.json"]
    )["hooks"]
    expected = {
        "SessionStart": "session-start", "UserPromptSubmit": "prompt",
        "PreToolUse": "pre-tool", "PostToolUse": "post-tool",
        "PostToolUseFailure": "post-tool", "Stop": "turn-end",
        "SubagentStart": "agent-start", "SubagentStop": "agent-end",
        "SessionEnd": "session-end",
    }
    assert set(hooks) == set(expected)
    for event, name in expected.items():
        (group,) = hooks[event]
        (hook,) = group["hooks"]
        assert hook["type"] == "command"
        assert is_magnolia_hook_command(hook["command"])
        assert hook["command"].endswith(f" claude {name}")
        assert hook["timeout"] == (10 if name == "post-tool" else 30)
        assert hook.get("async") is None
    settings = json.loads(
        render(neutral, context).files[_harness(context) / "settings.json"]
    )
    assert settings["permissions"]["allow"] == [
        "mcp__compchem-tools", "mcp__compchem-memory",
    ]


def test_claude_policy_env_present(make_render):
    neutral, context = make_render()
    launch = render(neutral, context)
    policy = _harness(context) / "policy.json"

    assert launch.env == {
        "MAGNOLIA_HOOK_POLICY": str(policy), "MCP_TOOL_TIMEOUT": "660000",
    }
    assert json.loads(launch.files[policy]) == {"deny_tools": ["bash"]}


def test_claude_never_uses_bare_or_bypass(make_render):
    neutral, context = make_render(model="m")
    launch = render(neutral, context)
    text = " ".join(launch.argv) + " ".join(launch.env) + b" ".join(
        launch.files.values()
    ).decode()
    for forbidden in (
        "--bare", "--dangerously", "--auto-approve", "--trust", "CLAUDE_CONFIG_DIR",
        "CODEX_HOME", "VIBE_HOME", "bypass",
    ):
        assert forbidden not in text


def test_claude_system_prompt_is_agents_then_sorted_rules(make_render):
    neutral, context = make_render()
    prompt = render(neutral, context).files[_harness(context) / "system-prompt.md"].decode()

    assert prompt == "AGENTS BODY\n\n--- rules/a.md ---\nrule a\n\n--- rules/b.md ---\nrule b\n"


def test_claude_system_prompt_rewrites_rule_tool_names(make_render):
    neutral, context = make_render()
    source = context.root / "rules" / "a.md"
    original = (
        "Use compchem-tools_run_shell and compchem-memory_memory_search.\n\n"
        "## Tool names in other clients\nDo not deliver this paragraph.\n\n## Keep\nKept.\n"
    )
    source.write_text(original, encoding="utf-8")

    prompt = render(neutral, context).files[_harness(context) / "system-prompt.md"].decode()

    assert "mcp__compchem-tools__run_shell" in prompt
    assert "mcp__compchem-memory__memory_search" in prompt
    assert "Tool names in other clients" not in prompt
    assert source.read_text(encoding="utf-8") == original


def test_claude_links_each_skill(make_render):
    neutral, context = make_render()
    launch = render(neutral, context)

    assert launch.links == {
        context.project_dir / ".claude" / "skills" / "alpha":
            context.root / ".opencode" / "skills" / "alpha",
    }
