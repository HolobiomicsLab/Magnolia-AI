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

# Codex: event streams (hooks did not load, see report)
for run, scen in (("first", "parent"), ("tool-error", "tool-error"), ("deny-write", "deny-write")):
    ev = [json.loads(l) for l in open(f"{T}/probe-codex-{run}/events.jsonl")]
    write("codex", scen, [(e["type"].replace(".", "-") + (("-" + e["item"]["type"]) if "item" in e else ""), e) for e in ev],
          {"client": "codex", "version": "0.154.0",
           "command": f"run_probes.sh codex_run {run} <prompt> [-m gpt-5.6-luna]  (codex exec --json -C <dir> -s workspace-write)",
           "observed": "event stream only; project .codex/hooks.json did NOT load (no hook trust), so there are no hook payloads"})
for scen in ("subagent", "context-small", "context-max", "deny-shell", "two-hooks"):
    write("codex", scen, [], {"client": "codex", "version": "0.154.0", "command": "not run",
                              "observed": "not probed: project hooks need persisted hook trust (see report)"})

# Codex turn.failed shape seen when the configured model is refused (captured text, run not retained)
write("codex", "turn-failed", [
    ("error", {"type": "error", "message": "{\"type\":\"error\",\"status\":400,\"error\":{\"type\":\"invalid_request_error\",\"message\":\"The '<model>' model is not supported when using Codex with a ChatGPT account.\",\"code\":\"model_not_found\"}}"}),
    ("turn-failed", {"type": "turn.failed", "error": {"message": "{\"type\":\"error\",\"status\":400,\"error\":{\"type\":\"invalid_request_error\",\"message\":\"The '<model>' model is not supported when using Codex with a ChatGPT account.\",\"code\":\"model_not_found\"}}"}})],
    {"client": "codex", "version": "0.154.0", "command": "run_probes.sh codex_run deny-write <prompt>  (default model from the user config, refused by the account)",
     "observed": "hand-transcribed from the console output of a refused run; shape of error and turn.failed events"})
