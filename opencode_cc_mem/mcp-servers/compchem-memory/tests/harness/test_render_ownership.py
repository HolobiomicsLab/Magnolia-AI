import json
import shlex
import tomllib
from dataclasses import replace

import pytest

from compchem_memory.harness import render_codex, render_vibe
from compchem_memory.harness.ownership import is_magnolia_hook_command
from compchem_memory.harness.render_common import _required_pre_tool_command, hook_command
from compchem_memory.hook.events import EVENTS


@pytest.mark.parametrize("client", ("claude", "codex", "vibe"))
@pytest.mark.parametrize("legacy", (False, True))
@pytest.mark.parametrize("directory", ("with spaces", "quote's $literal `literal` [literal]"))
def test_ownership_accepts_complete_commands_and_required_wrapper(make_render, client, legacy, directory):
    _, context = make_render(python=f"/{directory}/bin/python3")
    context = replace(context, root=context.root / directory)
    for event in EVENTS:
        command = (
            f"{shlex.quote(context.python)} -m compchem_memory.hook {client} {event}"
            if legacy else hook_command(context, client, event)
        )
        assert is_magnolia_hook_command(command, client)
        if event == "pre-tool":
            wrapped = _required_pre_tool_command(command, client, 12)
            assert is_magnolia_hook_command(wrapped, client)
            assert not is_magnolia_hook_command(wrapped + " && ./audit.sh", client)
            assert not is_magnolia_hook_command(wrapped.replace("sleep 1", "sleep 2"), client)


MEMBER_COMMANDS = (
    "echo see compchem_memory/hook/runner.py",
    "/opt/my_compchem_memory/hook/runner.py lint",
    "python -m compchem_memory.hook {client} pre-tool && ./lint.sh",
    "python -I /repo/mcp-servers/compchem-memory/src/compchem_memory/hook/runner.py {client} pre-tool; ./audit.sh",
    "grep -m compchem_memory.hook notes.txt",
    "grep -m compchem_memory.hook {client} pre-tool",
    "python -m compchem_memory.hook {other} pre-tool",
    "python -m compchem_memory.hook {client} pre-tool extra",
    "python -m compchem_memory.hook {client} Pre_Tool",
    "python -I -m compchem_memory.hook {client} pre-tool",
    "python -I /opt/my_compchem_memory/hook/runner.py {client} pre-tool",
    "python -m compchem_memory.hook {client} pre-tool > /tmp/log",
    "python -m compchem_memory.hook {client} pre-tool # comment",
    "python -m compchem_memory.hook {client} pre-tool\n./audit.sh",
    "python -m compchem_memory.hook {client} 'pre-tool'$(./audit.sh)",
    "python -m compchem_memory.hook {client} pre-tool | cat",
    "/tmp/`./audit.sh`/python -m compchem_memory.hook {client} pre-tool",
    '"/tmp/$ROOT/python" -m compchem_memory.hook {client} pre-tool',
    "/tmp/*/python -m compchem_memory.hook {client} pre-tool",
    "'unterminated",
)


@pytest.mark.parametrize("client", ("codex", "vibe"))
@pytest.mark.parametrize("template", MEMBER_COMMANDS)
def test_render_preserves_spoofed_and_other_client_hooks(make_render, client, template):
    neutral, context = make_render()
    command = template.format(client=client, other="vibe" if client == "codex" else "codex")
    if client == "codex":
        path = context.project_dir / ".codex/hooks.json"
        member = {"type": "command", "command": command}
        old = {"type": "command", "command": "python -m compchem_memory.hook codex pre-tool"}
        payload = {"hooks": {"PreToolUse": [{"matcher": "Bash", "hooks": [member, old]}]}}
        original = json.dumps(payload)
        renderer = render_codex.render
    else:
        path = context.project_dir / ".vibe/hooks.toml"
        original = (
            '[[hooks]]\nname = "magnolia-member-audit"\ntype = "pre_tool"\n'
            f"command = {json.dumps(command)}\n"
            '[[hooks]]\nname = "old-hook"\ntype = "pre_tool"\n'
            'command = "python -m compchem_memory.hook vibe pre-tool"\n'
        )
        renderer = render_vibe.render
    path.parent.mkdir(parents=True)
    path.write_text(original)
    first = renderer(neutral, context).files[path]
    path.write_bytes(first)
    assert renderer(neutral, context).files[path] == first
    if client == "codex":
        groups = json.loads(first)["hooks"]["PreToolUse"]
        commands = [hook["command"] for group in groups for hook in group["hooks"]]
        assert groups[0] == {"matcher": "Bash", "hooks": [member]}
    else:
        commands = [hook["command"] for hook in tomllib.loads(first.decode())["hooks"] if hook["type"] == "pre_tool"]
    assert commands.count(command) == 1
    assert not is_magnolia_hook_command(command, client)
    assert sum(is_magnolia_hook_command(item, client) for item in commands) == 1
