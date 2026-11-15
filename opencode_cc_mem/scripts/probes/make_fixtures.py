"""Turn raw probe logs into scrubbed fixtures.

Usage: make_fixtures.py <probe_tmp_dir> <fixtures_root>
Scrubs: home and temp paths, session/thread ids, token/usage fields, e-mail-like text.
"""
import json
import os
import re
import sys

T, OUT = sys.argv[1], sys.argv[2]
HOME = os.path.expanduser("~")
sess = {}


def sid(v):
    return sess.setdefault(v, f"ses-fixture-{len(sess) + 1}")


def scrub_str(s):
    s = re.sub(r"/private/var/folders/[^\"' ]*?/T/", "/TMPDIR/", s)
    s = re.sub(r"/var/folders/[^\"' ]*?/T/", "/TMPDIR/", s)
    s = re.sub(r"/private/tmp/claude-\d+/[^\"' ]*", "/TMPDIR/claude-scratchpad", s)
    s = s.replace(HOME, "/HOME").replace("/Users/", "/HOME-")
    s = re.sub(r"[\w.+-]+@[\w-]+\.[\w.-]+", "<email>", s)
    s = re.sub(r"(?i)token", "tok", s)
    s = s.replace("@", "(at)")
    for k, v in sess.items():
        s = s.replace(k, v)
    return s


def scrub(o):
    if isinstance(o, dict):
        out = {}
        for k, v in o.items():
            if k in ("session_id", "thread_id", "sessionId") and isinstance(v, str):
                out[k] = sid(v)
            elif k in ("credential_expires_at", "apiKeySource"):
                continue
            elif k in ("usage", "modelUsage"):
                out[k] = "<dropped>"
            else:
                out[scrub_str(k)] = scrub(v)
        return out
    if isinstance(o, list):
        return [scrub(x) for x in o]
    if isinstance(o, str):
        return scrub_str(o)
    return o


def write(client, scenario, items, meta):
    d = os.path.join(OUT, client, scenario)
    os.makedirs(d, exist_ok=True)
    for i, (name, obj) in enumerate(items, 1):
        with open(os.path.join(d, f"{i:02d}-{name}.json"), "w") as fh:
            json.dump(scrub(obj), fh, indent=1, sort_keys=True)
            fh.write("\n")
    with open(os.path.join(d, "scenario.json"), "w") as fh:
        json.dump(meta, fh, indent=1)
        fh.write("\n")


def claude_items(run):
    items = []
    for line in open(f"{T}/probe-claude-{run}/log.jsonl"):
        j = json.loads(line)
        s = j["stdin"]
        tag = s["hook_event_name"] + (".async" if j["argv"] == ["claude-async"] else "")
        if s.get("agent_id"):
            tag += ".sub"
        items.append((tag, s))
    return items


def result_line(run):
    for line in open(f"{T}/probe-claude-{run}/stream.jsonl"):
        j = json.loads(line)
        if j.get("type") == "result":
            return j


VER = "2.1.325"
meta = lambda cmd, obs: {"client": "claude", "version": VER, "command": cmd, "observed": obs}
base_cmd = ("run_probes.sh claude_%s  (claude -p <prompt> --settings <f> --setting-sources user "
            "--mcp-config <f> --strict-mcp-config --output-format stream-json --verbose "
            "--allowedTools mcp__probe__echo Task [--model haiku])")

items = claude_items("parent")
write("claude", "parent", items + [("result-stream-line", result_line("parent"))],
      meta(base_cmd % "parent_sub", "full ordered run; async hook entries tagged .async; subagent entries tagged .sub"))
write("claude", "subagent", [x for x in items if ".sub" in x[0] or x[0].startswith("Subagent")],
      meta(base_cmd % "parent_sub", "tool events inside the subagent carry agent_id and agent_type"))
write("claude", "two-hooks", [x for x in items if x[0].startswith("PostToolUse") and "mcp__probe__echo" == x[1]["tool_name"]],
      meta(base_cmd % "parent_sub", "PostToolUse for the echo tool: sync hook x2 (log + second_hook.py), async hook with sleep 2 logged later"))
write("claude", "tool-error", claude_items("tool-error") + [("result-stream-line", result_line("tool-error"))],
      meta(base_cmd % "tool_error", "a failing MCP tool fires PostToolUseFailure, not PostToolUse"))
for k in ("small", "max"):
    n = {"small": 2000, "max": 256043}[k]
    its = [x for x in claude_items(f"ctx-{k}") if x[0] in ("SessionStart", "UserPromptSubmit", "Stop")]
    write("claude", f"context-{k}", its + [("result-stream-line", result_line(f"ctx-{k}"))],
          meta(f"run_probes.sh claude_context {k}  (default model)",
               f"SessionStart additionalContext of {n} bytes returned; see report for what the model saw"))
write("claude", "deny-shell", [], meta("not run for Claude", "not probed (Task 0 only requires Codex denial probes)"))
write("claude", "deny-write", [], meta("not run for Claude", "not probed (Task 0 only requires Codex denial probes)"))

# Codex: hook payloads (hooks loaded with the owner-approved bypass flag on temp projects, run by the lead)
def codex_items(run):
    out = []
    for line in open(f"{T}/probe-codex-{run}/log.jsonl"):
        s = json.loads(line)["stdin"]
        out.append((s["hook_event_name"] + (".sub" if s.get("agent_id") else ""), s))
    return out


def codex_events(run):
    return [(e["type"].replace(".", "-") + (("-" + e["item"]["type"]) if "item" in e else ""), e)
            for e in (json.loads(l) for l in open(f"{T}/probe-codex-{run}/events.jsonl"))]


CX = "codex exec --json -C <tmp> -s workspace-write --dangerously-bypass-hook-trust -m gpt-5.6-luna <prompt> (temp project only, owner's request 15 Nov)"
cm = lambda obs: {"client": "codex", "version": "0.154.0", "command": CX, "observed": obs}
hp = codex_items("hooks-parent")
write("codex", "parent", hp, cm("full ordered hook run; .sub = events carrying agent_id"))
write("codex", "subagent", [x for x in hp if ".sub" in x[0]], cm("tool events inside the spawn_agent subagent carry agent_id (= its thread id) and agent_type"))
write("codex", "two-hooks", [x for x in hp if x[0] == "PostToolUse"], cm("six PostToolUse events; second independent hook wrote six marker lines"))
write("codex", "parent-events", codex_events("hooks-parent"), cm("the codex --json stream of the same run"))
write("codex", "deny-shell", codex_items("deny-shell") + codex_events("deny-shell"),
      cm("PROBE_DENY=Bash: PreToolUse deny; no PostToolUse; the --json stream has no command_execution item; agent reported the block; file not created"))
write("codex", "deny-write", codex_items("deny-write") + codex_events("deny-write"),
      cm("no deny hook; write to /private/var/tmp/magnolia-probe-deny/x.txt; PostToolUse fired, agent reported permission denied, no file/dir existed: sandbox"))
for k, n in (("small", 2000), ("max", 256043)):
    write("codex", f"context-{k}", codex_items(f"ctx-{k}") + codex_events(f"ctx-{k}"),
          cm(f"SessionStart additionalContext of {n} bytes; model answered MAGNOLIA-END-7Q"))
for run, scen in (("first", "mcp-events-no-hooks"), ("tool-error", "tool-error")):
    write("codex", scen, codex_events(run), {"client": "codex", "version": "0.154.0",
          "command": f"codex exec --json -C <tmp> -s workspace-write -m gpt-5.6-luna (hooks not loaded: no trust bypass)",
          "observed": "event stream only"})

# Codex turn.failed shape seen when the configured model is refused (captured text, run not retained)
write("codex", "turn-failed", [
    ("error", {"type": "error", "message": "{\"type\":\"error\",\"status\":400,\"error\":{\"type\":\"invalid_request_error\",\"message\":\"The '<model>' model is not supported when using Codex with a ChatGPT account.\",\"code\":\"model_not_found\"}}"}),
    ("turn-failed", {"type": "turn.failed", "error": {"message": "{\"type\":\"error\",\"status\":400,\"error\":{\"type\":\"invalid_request_error\",\"message\":\"The '<model>' model is not supported when using Codex with a ChatGPT account.\",\"code\":\"model_not_found\"}}"}})],
    {"client": "codex", "version": "0.154.0", "command": "run_probes.sh codex_run deny-write <prompt>  (default model from the user config, refused by the account)",
     "observed": "hand-transcribed from the console output of a refused run; shape of error and turn.failed events"})
