"""Values and files every client renderer builds the same way."""

from __future__ import annotations

import json
import re
import shlex
from pathlib import Path

from compchem_memory.hook.policy import REQUIRED_REASON
from compchem_memory.harness.neutral import McpServer, NeutralConfig
from compchem_memory.harness.protocol import LaunchContext

# Neutral event names; each client maps its own event onto one of these.
POST_TOOL_TIMEOUT_S = 10
DEFAULT_HOOK_TIMEOUT_S = 30
MCP_TOOL_TIMEOUT_MS = "660000"
REQUIRED_HOOK_TIMEOUT_S = 12
_OPEN_CODE_TOOL = re.compile(r"\b(compchem-(?:tools|memory))_([A-Za-z][A-Za-z0-9_]*)\b")
_OTHER_CLIENTS = re.compile(
    r"(?ms)^## Tool names in other clients\s*\n.*?(?=^##\s|\Z)"
)
_REQUIRED_HOOK_SCRIPT = """
fallback={fallback}
block() {{ printf '%s\\n' "$fallback"; }}
exec 3<&0 || {{ block; exit $?; }}
out=$(mktemp "${{TMPDIR:-/tmp}}/magnolia-hook.XXXXXX") || {{ block; exit $?; }}
cleanup() {{ rm -f "$out"; }}
trap cleanup 0
{command} <&3 3<&- >"$out" &
hook_pid=$!
exec 3<&-
(
  elapsed=0
  while kill -0 "$hook_pid" 2>/dev/null; do
    if [ "$elapsed" -ge {timeout_s} ]; then
      kill -TERM "$hook_pid" 2>/dev/null
      sleep 1
      kill -KILL "$hook_pid" 2>/dev/null
      exit 0
    fi
    sleep 1
    elapsed=$((elapsed + 1))
  done
) </dev/null >/dev/null 2>&1 &
timer_pid=$!
wait "$hook_pid"
status=$?
kill "$timer_pid" 2>/dev/null
wait "$timer_pid" 2>/dev/null
if [ "$status" -eq 0 ]; then cat "$out" || block; else block; fi
exit $?
""".strip()


def delivered_rule_text(text: str, harness: str) -> str:
    """Adapt OpenCode-authored rule prose for one rendered client copy."""
    from compchem_memory.hook.events import native_tool

    without_cross_client_help = _OTHER_CLIENTS.sub("", text)
    return _OPEN_CODE_TOOL.sub(
        lambda match: native_tool(harness, match.group(1), match.group(2)),
        without_cross_client_help,
    ).strip()


def hook_command(context: LaunchContext, harness: str, event: str) -> str:
    """The shell command a client runs for one hook event."""
    command = f"{shlex.quote(context.python)} -m compchem_memory.hook {harness} {event}"
    if not context.noninteractive or event != "pre-tool":
        return command
    return _required_pre_tool_command(command, harness, REQUIRED_HOOK_TIMEOUT_S)


def _required_pre_tool_command(command: str, harness: str, timeout_s: int) -> str:
    """Wrap a required pre-tool hook so every inner failure emits a native block."""
    if harness == "vibe":
        fallback = json.dumps({"decision": "deny", "reason": REQUIRED_REASON})
    else:
        fallback = json.dumps({
            "hookSpecificOutput": {
                "hookEventName": "PreToolUse",
                "permissionDecision": "deny",
                "permissionDecisionReason": REQUIRED_REASON,
            }
        })
    script = _REQUIRED_HOOK_SCRIPT.format(
        fallback=shlex.quote(fallback), command=command, timeout_s=timeout_s
    )
    return f"sh -c {shlex.quote(script)}"


def hook_timeout(event: str) -> int:
    """Hook timeout in seconds (Claude, Codex and Vibe all use seconds)."""
    return POST_TOOL_TIMEOUT_S if event == "post-tool" else DEFAULT_HOOK_TIMEOUT_S


def harness_dir(context: LaunchContext, harness: str) -> Path:
    if context.noninteractive:
        if context.control_dir is None or not context.invocation_id:
            raise ValueError(
                "noninteractive launch requires control_dir and invocation_id"
            )
        return context.control_dir / "executors" / context.invocation_id
    return context.project_dir / ".magnolia" / "harness" / harness


def policy_files(
    neutral: NeutralConfig, context: LaunchContext, harness: str
) -> tuple[dict[Path, bytes], dict[str, str]]:
    """The hook policy file and the environment variable that points the hook at it."""
    path = harness_dir(context, harness) / "policy.json"
    body = dict(context.role_policy or {})
    body["deny_tools"] = sorted(neutral.deny_tools)
    env = {"MAGNOLIA_HOOK_POLICY": str(path.resolve())}
    if context.noninteractive:
        env.update(
            MAGNOLIA_HOOK_POLICY_REQUIRED="1",
            MAGNOLIA_DOORBELL="0",
        )
    return {path: (json.dumps(body, sort_keys=True) + "\n").encode("utf-8")}, env


def active_servers(neutral: NeutralConfig) -> list[McpServer]:
    return [server for server in neutral.servers if server.enabled]


def split_command(server: McpServer) -> tuple[str, list[str]]:
    command = server.command or []
    return command[0], list(command[1:])


def timeout_seconds(server: McpServer) -> int:
    return server.timeout_ms // 1000


def skill_links(context: LaunchContext, subdirectory: str) -> dict[Path, Path]:
    """One link per skill under ``<project>/<subdirectory>/``, pointing at the shared skills."""
    source = context.root / ".opencode" / "skills"
    if not source.is_dir():
        return {}
    return {
        context.project_dir / subdirectory / skill.name: skill
        for skill in sorted(source.iterdir())
        if skill.is_dir()
    }
