#!/usr/bin/env bash
# Probe Claude Code and Codex hook payloads. Each probe runs in its own temp dir
# under $TMPDIR. Usage: run_probes.sh <function> ; functions below.
# Never uses `claude --bare`; never touches global configs.
set -u
HERE="$(cd "$(dirname "$0")" && pwd)"
PY="${PROBE_PY:?set PROBE_PY to a Python that has fastmcp or mcp}"
BASE="${TMPDIR:-/tmp}"

claude_cfg() { # $1=dir ; writes settings.json and mcp.json
  local d="$1" ev
  python3 - "$d" "$PY" "$HERE" <<'PYEOF'
import json, sys
d, py, here = sys.argv[1:4]
cmd = f"{py} {here}/log_hook.py claude"
h = lambda c, **k: {"hooks": [dict(type="command", command=c, **k)]}
hooks = {e: [h(cmd)] for e in ["SessionStart", "UserPromptSubmit", "PreToolUse",
         "PostToolUse", "PostToolUseFailure", "Stop", "SubagentStart", "SubagentStop", "SessionEnd"]}
hooks["PostToolUse"].append(h(f"{py} {here}/second_hook.py"))
hooks["PostToolUse"].append(h(f"sleep 2; {py} {here}/log_hook.py claude-async", **{"async": True}))
json.dump({"hooks": hooks}, open(f"{d}/settings.json", "w"), indent=1)
json.dump({"mcpServers": {"probe": {"command": py, "args": [f"{here}/probe_mcp.py"]}}},
          open(f"{d}/mcp.json", "w"), indent=1)
PYEOF
}

claude_run() { # $1=name $2=prompt [$3...=extra args]
  local name="$1" prompt="$2"; shift 2
  local d="$BASE/probe-claude-$name"; rm -rf "$d"; mkdir -p "$d"; claude_cfg "$d"
  ( cd "$d" && PROBE_LOG="$d/log.jsonl" PROBE_LOG2="$d/log2.txt" \
    claude -p "$prompt" --settings "$d/settings.json" --setting-sources user \
      --mcp-config "$d/mcp.json" --strict-mcp-config --output-format stream-json --verbose \
      --allowedTools mcp__probe__echo Task "$@" > "$d/stream.jsonl" 2> "$d/stderr.txt" )
  echo "$d"
}

claude_parent_sub() { claude_run parent "Call the echo tool with text 'top'. Then ask a subagent to call echo with text 'sub'." --model haiku; }
claude_tool_error() { claude_run tool-error "Call the echo tool with text 'fail' and report the outcome in five words." --model haiku; }

claude_context() { # $1=small|max ; default model
  local kind="$1"; local d="$BASE/probe-claude-ctx-$kind"; rm -rf "$d"; mkdir -p "$d"; claude_cfg "$d"
  python3 - "$d" "$kind" <<'PYEOF'
import sys
d, kind = sys.argv[1:3]
if kind == "small":
    body = "A" * (2000 - len("MAGNOLIA-END-7Q") - 1) + "\n"
else:
    body = ("B" * 256000) + "\n\n[truncated at 256k chars]\n"
open(f"{d}/inject.txt", "w").write(body + "MAGNOLIA-END-7Q")
PYEOF
  wc -c < "$d/inject.txt" > "$d/inject.bytes"
  ( cd "$d" && PROBE_LOG="$d/log.jsonl" PROBE_LOG2="$d/log2.txt" PROBE_INJECT="$d/inject.txt" \
    claude -p "Reply with the last token of your session context." --settings "$d/settings.json" \
      --setting-sources user --strict-mcp-config --output-format stream-json --verbose \
      > "$d/stream.jsonl" 2> "$d/stderr.txt" )
  echo "$d"
}

codex_cfg() { # $1=dir
  python3 - "$1" "$PY" "$HERE" <<'PYEOF'
import json, os, sys
d, py, here = sys.argv[1:4]
os.makedirs(f"{d}/.codex", exist_ok=True)
open(f"{d}/.codex/config.toml", "w").write(
    f'[mcp_servers.probe]\ncommand = "{py}"\nargs = ["{here}/probe_mcp.py"]\n')
cmd = f"{py} {here}/log_hook.py codex"
h = lambda c: {"hooks": [{"type": "command", "command": c}]}
hooks = {e: [h(cmd)] for e in ["SessionStart", "UserPromptSubmit", "PreToolUse",
                              "PostToolUse", "Stop"]}
hooks["PostToolUse"].append(h(f"{py} {here}/second_hook.py"))
json.dump({"hooks": hooks}, open(f"{d}/.codex/hooks.json", "w"), indent=1)
PYEOF
  git -C "$1" init -q 2>/dev/null
}

codex_run() { # $1=name $2=prompt [extra codex args]
  local name="$1" prompt="$2"; shift 2
  local d="$BASE/probe-codex-$name"; rm -rf "$d"; mkdir -p "$d"; codex_cfg "$d"
  PROBE_LOG="$d/log.jsonl" PROBE_LOG2="$d/log2.txt" \
    codex exec --json -C "$d" -s workspace-write "$@" "$prompt" > "$d/events.jsonl" 2> "$d/stderr.txt"
  echo "$d"
}
"${1:?function name}" "${@:2}"
