import tomllib
import json
from pathlib import Path

from compchem_memory.harness import RENDERERS
from compchem_memory.harness.ownership import is_magnolia_hook_command
from compchem_memory.harness.render_vibe import render


def _dir(context):
    return context.project_dir / ".vibe"


def _policy(context):
    return context.project_dir / ".magnolia" / "harness" / "vibe" / "policy.json"


def _config(launch, context):
    return tomllib.loads(launch.files[_dir(context) / "config.toml"].decode())


def _hooks(launch, context):
    return tomllib.loads(launch.files[_dir(context) / "hooks.toml"].decode())["hooks"]


def test_vibe_renderer_is_registered_with_its_binary():
    assert RENDERERS["vibe"] is render
    assert render.binary == "vibe"


def test_vibe_argv_has_no_model_flag(make_render):
    neutral, context = make_render(model="m")
    assert render(neutral, context).argv == ["vibe", "--workdir", str(context.project_dir)]


def test_vibe_mcp_timeout_is_660(make_render):
    neutral, context = make_render(model="devstral")
    config = _config(render(neutral, context), context)
    servers = {server["name"]: server for server in config["mcp_servers"]}

    assert {s["tool_timeout_sec"] for s in servers.values()} == {660}
    assert servers["compchem-tools"]["transport"] == "http"
    assert servers["compchem-tools"]["url"] == "http://127.0.0.1:8123/mcp"
    memory = servers["compchem-memory"]
    assert memory["transport"] == "stdio"
    assert memory["command"] == "/venv/bin/python"
    assert memory["args"] == ["-m", "compchem_memory.server"]
    assert config["active_model"] == "devstral"
    assert config["disabled_tools"] == ["bash"]


def test_vibe_http_memory_uses_native_api_key_env_auth(make_render, monkeypatch):
    neutral, context = make_render(model="devstral")
    context.memory_service = {
        "transport": "http",
        "url": "http://127.0.0.1:43101/mcp",
        "authorization_env": "MAGNOLIA_MEMORY_SERVICE_TOKEN",
    }
    for server in neutral.servers:
        if server.name == "compchem-memory":
            server.kind = "remote"
            server.command = None
            server.url = context.memory_service["url"]
    rendered = _config(render(neutral, context), context)["mcp_servers"]
    memory = next(server for server in rendered if server["name"] == "compchem-memory")

    assert memory["auth"] == {
        "type": "static",
        "api_key_env": "MAGNOLIA_MEMORY_SERVICE_TOKEN",
    }
    assert "headers" not in memory
    assert "${" not in repr(memory)

    monkeypatch.setenv("MAGNOLIA_MEMORY_SERVICE_TOKEN", "recorded-token")
    try:
        from vibe.core.config.models import MCPHttp
    except ImportError:
        fixture = Path(__file__).parents[1] / "fixtures" / "vibe_http_auth.json"
        assert json.loads(fixture.read_text(encoding="utf-8")) == {
            "input": memory["auth"],
            "expected_headers": {"Authorization": "Bearer recorded-token"},
        }
    else:
        assert MCPHttp.model_validate(memory).http_headers() == {
            "Authorization": "Bearer recorded-token"
        }


def test_vibe_without_model_does_not_pin_active_model(make_render):
    neutral, context = make_render()
    assert "active_model" not in _config(render(neutral, context), context)


def test_vibe_pre_tool_hook_is_strict(make_render):
    neutral, context = make_render()
    hooks = {hook["name"]: hook for hook in _hooks(render(neutral, context), context)}

    assert set(hooks) == {
        "magnolia-session-start", "magnolia-prompt", "magnolia-pre-tool",
        "magnolia-post-tool", "magnolia-turn-end", "magnolia-session-end",
    }
    assert hooks["magnolia-pre-tool"]["strict"] is True
    assert all("strict" not in h for n, h in hooks.items() if n != "magnolia-pre-tool")
    assert {n: h["type"] for n, h in hooks.items()} == {
        "magnolia-session-start": "session_start", "magnolia-prompt": "pre_agent",
        "magnolia-pre-tool": "pre_tool", "magnolia-post-tool": "post_tool",
        "magnolia-turn-end": "post_agent", "magnolia-session-end": "session_end",
    }
    pre_tool = hooks["magnolia-pre-tool"]["command"]
    assert is_magnolia_hook_command(pre_tool)
    assert pre_tool.endswith(" vibe pre-tool")
    assert hooks["magnolia-post-tool"]["timeout"] == 10
    assert hooks["magnolia-prompt"]["timeout"] == 30
    assert hooks["magnolia-prompt"]["match"] == "main"
    assert all("match" not in hook for name, hook in hooks.items()
               if name != "magnolia-prompt")


def test_vibe_policy_env_present(make_render):
    neutral, context = make_render()
    launch = render(neutral, context)

    assert launch.env == {"MAGNOLIA_HOOK_POLICY": str(_policy(context))}
    assert launch.files[_policy(context)] == b'{"deny_tools": ["bash"]}\n'
    assert launch.links == {
        context.project_dir / ".agents" / "skills" / "alpha":
            context.root / ".opencode" / "skills" / "alpha",
    }


def test_vibe_preserves_member_entries_and_is_idempotent(make_render):
    neutral, context = make_render(model="m")
    folder = _dir(context)
    folder.mkdir(parents=True)
    (folder / "config.toml").write_text(
        'disabled_tools = ["mine.tool"]\n\n[[mcp_servers]]\nname = "mine"\n'
        'transport = "stdio"\ncommand = "mine"\n', encoding="utf-8",
    )
    (folder / "hooks.toml").write_text(
        '[[hooks]]\nname = "member"\ntype = "pre_tool"\ncommand = "m"\n'
        '[[hooks]]\nname = "magnolia-old"\ntype = "pre_tool"\ncommand = "x"\n',
        encoding="utf-8",
    )

    def apply(launch):
        for path, content in launch.files.items():
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(content)

    apply(render(neutral, context))
    config = tomllib.loads((folder / "config.toml").read_text())
    hooks = tomllib.loads((folder / "hooks.toml").read_text())["hooks"]
    assert config["disabled_tools"] == ["mine.tool", "bash"]
    assert config["mcp_servers"][0]["name"] == "mine"
    assert [h["name"] for h in hooks][0] == "member"
    assert hooks[1] == {"name": "magnolia-old", "type": "pre_tool", "command": "x"}
    assert len(hooks) == 8

    before = {p: p.read_bytes() for p in (folder / "config.toml", folder / "hooks.toml")}
    apply(render(neutral, context))
    assert {p: p.read_bytes() for p in before} == before


def _apply(launch):
    for path, content in launch.files.items():
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(content)


def test_vibe_disabled_tools_is_append_only_and_never_duplicates(make_render):
    neutral, context = make_render()
    folder = _dir(context)
    folder.mkdir(parents=True)
    (folder / "config.toml").write_text(
        'disabled_tools = ["file_system.bash", "extra.tool"]\n', encoding="utf-8"
    )
    _apply(render(neutral, context))
    _apply(render(neutral, context))
    config = tomllib.loads((folder / "config.toml").read_text())
    assert config["disabled_tools"] == ["file_system.bash", "extra.tool", "bash"]


def test_vibe_member_render_maps_all_neutral_names(make_render):
    neutral, context = make_render()
    neutral.deny_tools = frozenset({"bash", "write", "edit", "write_file", "custom"})

    config = _config(render(neutral, context), context)

    assert config["disabled_tools"] == ["bash", "custom", "edit", "write_file"]


def test_vibe_explicit_model_overwrites_member_active_model(make_render):
    neutral, context = make_render(model="ours")
    folder = _dir(context)
    folder.mkdir(parents=True)
    (folder / "config.toml").write_text('active_model = "member"\n', encoding="utf-8")
    _apply(render(neutral, context))
    assert tomllib.loads((folder / "config.toml").read_text())["active_model"] == "ours"

    neutral, context = make_render()
    (folder / "config.toml").write_text('active_model = "member"\n', encoding="utf-8")
    _apply(render(neutral, context))
    assert tomllib.loads((folder / "config.toml").read_text())["active_model"] == "member"


def test_vibe_never_uses_forbidden_flags_or_homes(make_render):
    neutral, context = make_render(model="m")
    launch = render(neutral, context)
    text = " ".join(launch.argv) + " ".join(launch.env) + b" ".join(
        launch.files.values()
    ).decode()
    for forbidden in (
        "--dangerously", "--auto-approve", "--trust", "VIBE_HOME", "CODEX_HOME",
        "CLAUDE_CONFIG_DIR", "bypass",
    ):
        assert forbidden not in text
