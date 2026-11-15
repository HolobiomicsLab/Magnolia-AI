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

# ---- Mistral Vibe 3.1.2. NOTE: --trust (project trust for hooks/MCP) is passed ONLY on throwaway temp
# probe projects, by whoever the owner authorised; telemetry off; stdin closed; capped price and turns.
vibe_cfg() { # $1=dir
  mkdir -p "$1/.vibe"
  cat > "$1/.vibe/config.toml" <<TOML
[[mcp_servers]]
name = "probe"
transport = "stdio"
command = "$PY"
args = ["$HERE/probe_mcp.py"]
TOML
  python3 - "$1" "$PY" "$HERE" <<'PYEOF'
import sys
d, py, here = sys.argv[1:4]
out = []
for t in ["session_start", "session_end", "pre_agent", "post_agent", "pre_tool", "post_tool", "notification"]:
    out.append(f'[[hooks]]\nname = "log-{t}"\ntype = "{t}"\ncommand = "{py} {here}/log_hook.py vibe"\n')
out.append(f'[[hooks]]\nname = "second-post-tool"\ntype = "post_tool"\ncommand = "{py} {here}/second_hook.py"\n')
open(f"{d}/.vibe/hooks.toml", "w").write("\n".join(out))
PYEOF
}

vibe_run() { # $1=name $2=prompt [extra vibe args]; env PROBE_DENY / PROBE_INJECT pass through
  local name="$1" prompt="$2"; shift 2
  local d="$BASE/probe-vibe-$name"; rm -rf "$d"; mkdir -p "$d"; vibe_cfg "$d"
  ( cd "$d" && VIBE_DISABLE_TELEMETRY=true PROBE_LOG="$d/log.jsonl" PROBE_LOG2="$d/log2.txt" \
    vibe -p "$prompt" --output json --workdir "$d" --trust --max-turns 8 --max-price 0.05 \
      --output-dir "$d/out" "$@" > "$d/stdout.json" 2> "$d/stderr.txt" < /dev/null )
  echo "$d"
}
vibe_parent_sub() { vibe_run parent "Call the probe echo tool with text 'top'. Then spawn one subagent and have it call the probe echo tool with text 'sub'. Reply done."; }
vibe_tool_error() { vibe_run tool-error "Call the probe echo tool with text 'fail' and report the outcome in five words."; }
vibe_deny_shell() { PROBE_DENY=bash vibe_run deny-shell "Run the shell command: echo hi > shell-ran.txt . Then report what happened."; }
vibe_deny_write() { vibe_run deny-write "Run the shell command: mkdir -p /private/var/tmp/magnolia-probe-deny-vibe && echo hi > /private/var/tmp/magnolia-probe-deny-vibe/x.txt . Then report the result."; }
vibe_context() { # $1=small|max
  local kind="$1" f="$BASE/probe-vibe-inject-$1.txt"
  python3 - "$f" "$kind" <<'PYEOF'
import sys
f, kind = sys.argv[1:3]
body = "A" * (2000 - 16) + "\n" if kind == "small" else ("B" * 256000) + "\n\n[truncated at 256k chars]\n"
open(f, "w").write(body + "MAGNOLIA-END-7Q")
PYEOF
  PROBE_INJECT="$f" vibe_run "ctx-$kind" "Reply with the last token of your session context."
}
"${1:?function name}" "${@:2}"
