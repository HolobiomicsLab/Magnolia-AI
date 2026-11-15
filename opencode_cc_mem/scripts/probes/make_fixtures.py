"""Turn raw probe logs into scrubbed fixtures.

Usage: make_fixtures.py <probe_tmp_dir> <fixtures_root>
       make_fixtures.py --check <fixtures_root>

What the scrubber changes (field names are never rewritten):
- home and temp paths: the home directory becomes /HOME; a path under /HOME
  other than ~/.codex or ~/.claude becomes /HOME/<elided>; macOS per-user temp
  directories become /TMPDIR/ (also inside Claude's project directory names);
- session and thread ids (keys session_id, thread_id, sessionId) become
  ses-fixture-N;
- output of built-in shell calls (tool_response of a Bash/shell tool,
  aggregated_output of a command_execution item) becomes
  "<shell output elided, N chars>": a probe agent may read the owner's own
  configuration files at start, and that text must not be published;
- values of secret-named keys (api key, password, secret, authorization,
  access/refresh/id token, ...) become "<redacted>"; Bearer and sk- strings in
  any value become "<redacted>"; e-mail addresses become <email>;
- credential_expires_at and apiKeySource are dropped.
Kept as captured: every other key and value, including usage and modelUsage.

--check scans a fixtures tree for the home directory, the login and full
name, e-mail addresses, /Users/ paths and Bearer/sk- strings, and exits 1 if
any is found (it reports file and kind, never the matched text).
"""
import json
import os
import pwd
import re
import sys

HOME = os.path.expanduser("~")
SECRET_KEY = re.compile(
    r"(?i)^(api[_-]?key|apikey|password|passwd|secret|client[_-]?secret|authorization|"
    r"(access|refresh|id|auth|bearer|session)[_-]?token|token)$")
SHELL_TOOLS = ("Bash", "shell", "exec_command", "local_shell", "file_system.bash")
sess = {}


def sid(v):
    return sess.setdefault(v, f"ses-fixture-{len(sess) + 1}")


def scrub_str(s):
    s = re.sub(r"/private/var/folders/[^\"' ]*?/T/", "/TMPDIR/", s)
    s = re.sub(r"/var/folders/[^\"' ]*?/T/", "/TMPDIR/", s)
    s = re.sub(r"-private-var-folders-[A-Za-z0-9]+-[A-Za-z0-9]+-T-", "-TMPDIR-", s)
    s = re.sub(r"/private/tmp/claude-\d+/[^\"' ]*", "/TMPDIR/claude-scratchpad", s)
    s = s.replace(HOME, "/HOME").replace(os.path.basename(HOME), "USER").replace("/Users/", "/HOME-")
    s = re.sub(r"/HOME/(?!\.codex/|\.claude/)[^\s'\"]+", "/HOME/<elided>", s)
    s = re.sub(r"[\w.+-]+@[\w-]+\.[\w.-]+", "<email>", s)
    s = re.sub(r"(?i)\bbearer\s+[A-Za-z0-9._~+/=-]+", "<redacted>", s)
    s = re.sub(r"\bsk-[A-Za-z0-9_-]{16,}", "<redacted>", s)
    for k, v in sess.items():
        s = s.replace(k, v)
    return s


def elided(v):
    n = len(v) if isinstance(v, str) else len(json.dumps(v))
    return f"<shell output elided, {n} chars>"


def scrub(o):
    if isinstance(o, dict):
        shell = o.get("tool_name") in SHELL_TOOLS
        cmd_item = o.get("type") == "command_execution"
        out = {}
        for k, v in o.items():
            if k in ("session_id", "thread_id", "sessionId") and isinstance(v, str):
                out[k] = sid(v)
            elif k in ("credential_expires_at", "apiKeySource"):
                continue
            elif SECRET_KEY.match(k) and v not in (None, "", [], {}):
                out[k] = "<redacted>"
            elif shell and k in ("tool_response", "tool_output", "tool_output_text") and v or cmd_item and k == "aggregated_output" and v:
                out[k] = elided(v)
            else:
                out[k] = scrub(v)
        return out
    if isinstance(o, list):
        return [scrub(x) for x in o]
    if isinstance(o, str):
        return scrub_str(o)
    return o


def check(root):
    user = pwd.getpwuid(os.getuid())
    names = [n for n in re.split(r"[\s,]+", user.pw_gecos or "") if len(n) > 2]
    kinds = {
        "home": re.compile(re.escape(HOME)),
        "/Users/ path": re.compile(r"/Users/"),
        "login": re.compile(re.escape(user.pw_name), re.I),
        "full name": re.compile("|".join(map(re.escape, names)), re.I) if names else None,
        "e-mail": re.compile(r"[\w.+-]+@[\w-]+\.[a-z]{2,}", re.I),
        "bearer/sk-": re.compile(r"(?i)\bbearer\s+[A-Za-z0-9._~+/=-]{8,}|\bsk-[A-Za-z0-9_-]{16,}"),
    }
    bad = 0
    for d, _, fs in os.walk(root):
        for f in sorted(fs):
            p = os.path.join(d, f)
            text = open(p, encoding="utf-8", errors="replace").read()
            for kind, rx in kinds.items():
                if rx is not None and rx.search(text):
                    print(f"{p}: {kind}")
                    bad += 1
    return 1 if bad else 0


if sys.argv[1] == "--check":
    sys.exit(check(sys.argv[2]))
T, OUT = sys.argv[1], sys.argv[2]


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


# Vibe: hook payloads (run by the lead with --trust on temp projects) + a reduced export.json
def vibe_items(run):
    out = []
    for line in open(f"{T}/probe-vibe-{run}/log.jsonl"):
        s = json.loads(line)["stdin"]
        out.append((s["hook_event_name"] + (".sub" if s.get("parent_session_id") else ""), s))
    return out


EXPORT_KEEP = ("outcome", "stop_reason", "error", "usage", "cost_usd", "steps", "limits", "exit_code",
               "schema_version", "vibe_version", "subagents")


def vibe_export(run):  # config, warnings, journal paths and timestamps are dropped (private configuration)
    e = json.load(open(f"{T}/probe-vibe-{run}/out/export.json"))
    e = {k: e[k] for k in EXPORT_KEEP if k in e}
    e["subagents"] = [{k: v for k, v in a.items() if k != "journal_dir"} for a in e.get("subagents", [])]
    return [("export", e)]


VB = ("VIBE_DISABLE_TELEMETRY=true vibe -p <prompt> --output json --workdir <tmp> --trust --max-turns 8 "
      "--max-price 0.05 --output-dir <tmp>/out [--auto-approve ...] (temp project only, run by the lead on the owner's go)")
vm = lambda obs: {"client": "vibe", "version": "3.1.2", "command": VB, "observed": obs}
vp = vibe_items("parent")
write("vibe", "parent", vp + vibe_export("parent"), vm("--auto-approve --disabled-tools file_system.bash/write_file/edit; parent plus two agents.spawn subagents; export.json reduced"))
write("vibe", "subagent", [x for x in vp if ".sub" in x[0]], vm("events inside agents.spawn subagents carry parent_session_id and their own child-... session_id"))
write("vibe", "two-hooks", [x for x in vp if x[0].startswith("post_tool")], vm("five post_tool events; second independent hook wrote five lines"))
for run, scen, obs in (
    ("parent-noapprove", "headless-no-approval", "without --auto-approve the MCP tool is denied (needs approval a headless run cannot give); stopped at turn_limit"),
    ("tool-error", "tool-error", "failing MCP tool: post_tool with tool_status failure and tool_error; no separate failure event"),
    ("deny-shell", "deny-shell", "PROBE_DENY=bash: pre_tool hook denied file_system.bash; no post_tool; file not created"),
    ("deny-write", "deny-write", "no deny hook: the shell wrote outside the project and TMPDIR successfully; Vibe has no sandbox"),
    ("ctx-small", "context-small", "session_start additional_context of 2000 bytes; model did not report the marker (inconclusive)"),
    ("ctx-max", "context-max", "session_start additional_context of 256043 bytes; model reported MAGNOLIA-END-7Q")):
    write("vibe", scen, vibe_items(run) + vibe_export(run), vm(obs))
write("vibe", "enabled-tools-hides-mcp", vibe_items("parent-filtered") if os.path.exists(f"{T}/probe-vibe-parent-filtered/log.jsonl") else [] , vm("--enabled-tools mcp_probe.echo: the MCP tool vanished from the tool list, no tool call (enabled-tools names do not match MCP tools)"))
