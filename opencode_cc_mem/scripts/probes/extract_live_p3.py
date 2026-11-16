"""Reduce Plan 3 Task 0 live probe logs to bounded fixtures, then scrub them.

Usage: extract_live_p3.py <claude_root> <codex_root> <vibe_root> <fixtures_root> [codex_rollout.jsonl] [x9_rollout.jsonl]

Each root is a run_probes_p3.sh RUNROOT. For every run tag it writes, under
<fixtures_root>/<client>/live-p3/<tag>/:
  NN-<event>[-<tool>].json  hook calls in arrival order: {stdin, decision, class,
                            reason, policy_sha256, mode} (decision = what the probe
                            hook computed; in mode "crash" nothing was printed)
  stream-*.json             selected client stream lines (Claude init reduced to
                            model/tools/permission fields, result line with usage
                            and modelUsage; Codex thread/turn/item events; Vibe
                            export.json reduced, its config cut to a few keys)
  run.json                  argv (temp paths kept for the scrubber), exit status
Every file then goes through make_fixtures.py --scrub-json (numeric usage kept).
"""
import json
import os
import re
import subprocess
import sys
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
CLAUDE_INIT_KEEP = ("type", "subtype", "model", "tools", "permissionMode", "mcp_servers", "cwd",
                    "session_id", "claude_code_version", "apiKeySource", "output_style")
VIBE_EXPORT_KEEP = ("outcome", "stop_reason", "error", "session_id", "usage", "cost_usd", "steps",
                    "limits", "exit_code", "schema_version", "vibe_version", "subagents")
VIBE_CONFIG_KEEP = ("active_model", "disabled_tools", "enabled_tools", "enable_telemetry",
                    "enable_subagents", "enable_background_processes", "bypass_tool_permissions")


def jl(path):
    out = []
    if not os.path.exists(path):
        return out
    for line in open(path, encoding="utf-8"):
        line = line.strip()
        if not line:
            continue
        try:
            out.append(json.loads(line))
        except ValueError:
            pass
    return out


def safe(s):
    return re.sub(r"[^A-Za-z0-9_.-]+", "_", s)[:60]


def hook_items(root, tag):
    items = []
    for j in jl(f"{root}/logs/{tag}.hook.jsonl"):
        s = j.get("stdin", {})
        name = s.get("hook_event_name", "unparsed")
        if s.get("tool_name"):
            name += "-" + s["tool_name"]
        if s.get("agent_id") or s.get("parent_session_id"):
            name += ".sub"
        items.append((name, {k: j.get(k) for k in ("stdin", "decision", "class", "reason",
                                                    "policy_sha256", "mode")}))
    return items


def claude_stream(root, tag):
    items = []
    for j in jl(f"{root}/logs/{tag}.stream"):
        if j.get("type") == "system" and j.get("subtype") == "init":
            items.append(("stream-init", {k: j.get(k) for k in CLAUDE_INIT_KEEP if k in j}))
        elif j.get("type") == "result":
            items.append(("stream-result", j))
        elif j.get("type") == "user" and "permission_decision" in json.dumps(j.get("tool_result_meta", "")):
            items.append(("stream-tool-result-with-permission-meta", j))
    return items


def codex_stream(root, tag):
    items = []
    for j in jl(f"{root}/logs/{tag}.stream"):
        it = j.get("item") or {}
        if it.get("type") == "reasoning":
            continue
        name = "stream-" + j.get("type", "x").replace(".", "-") + ("-" + it["type"] if it.get("type") else "")
        items.append((name, j))
    return items


def vibe_stream(root, tag):
    p = f"{root}/logs/{tag}.out/export.json"
    if not os.path.exists(p):
        return []
    e = json.load(open(p, encoding="utf-8"))
    red = {k: e[k] for k in VIBE_EXPORT_KEEP if k in e}
    cfg = e.get("config", {})
    red["config_subset"] = {k: cfg.get(k) for k in VIBE_CONFIG_KEEP}
    red["config_subset"]["model_aliases"] = sorted((cfg.get("models") or {}).keys())
    notices = [l.strip() for l in open(f"{root}/logs/{tag}.stderr", encoding="utf-8")
               if "not available" in l or "Stopped:" in l]
    red["stderr_notices"] = notices
    items = [("stream-export", red)]
    try:
        entries = json.load(open(f"{root}/logs/{tag}.stream", encoding="utf-8"))
    except (OSError, ValueError):
        entries = []
    for e in entries if isinstance(entries, list) else []:
        d = e.get("detail") or {}
        if e.get("type") == "notice" and d.get("kind") == "hook_completed" and d.get("status") == "error":
            items.append(("stream-hook-denial-notice", e))
        elif e.get("type") == "effect":
            items.append(("stream-effect-" + str(d.get("toolName")), e))
    return items


def generic_overrides(argv):
    """Name the owner's own MCP servers/plugins generically (they are user config, not probe data)."""
    out, n = [], {"mcp": 0, "plugin": 0}
    for a in argv or []:
        m = re.match(r'^mcp_servers\.[^.]+\.enabled=false$', a)
        if m:
            n["mcp"] += 1
            a = f"mcp_servers.<user-server-{n['mcp']}>.enabled=false"
        elif re.match(r'^plugins\."[^"]+"\.enabled=false$', a):
            n["plugin"] += 1
            a = f'plugins."<user-plugin-{n["plugin"]}>".enabled=false'
        out.append(a)
    return out


def run_meta(root, tag, client):
    argv = json.load(open(f"{root}/logs/{tag}.argv")) if os.path.exists(f"{root}/logs/{tag}.argv") else None
    argv = generic_overrides(argv)
    rc = open(f"{root}/logs/{tag}.rc").read().strip() if os.path.exists(f"{root}/logs/{tag}.rc") else None
    err = []
    if os.path.exists(f"{root}/logs/{tag}.stderr"):
        err = [l.rstrip() for l in open(f"{root}/logs/{tag}.stderr", encoding="utf-8")
               if re.search(r"error|blocked|not available|unexpected argument|Stopped", l, re.I)][:12]
    return {"client": client, "tag": tag, "argv": argv, "exit_status": rc, "stderr_lines": err,
            "synthetic": False}


def write_all(out_dir, items, stage):
    os.makedirs(out_dir, exist_ok=True)
    for i, (name, obj) in enumerate(items, 1):
        raw = os.path.join(stage, f"{i:02d}.json")
        json.dump(obj, open(raw, "w"), indent=1)
        dst = os.path.join(out_dir, f"{i:02d}-{safe(name)}.json")
        subprocess.run([sys.executable, f"{HERE}/make_fixtures.py", "--scrub-json", raw, dst], check=True)


def main():
    croot, xroot, vroot, out = sys.argv[1:5]
    rollout = sys.argv[5] if len(sys.argv) > 5 else None
    rollouts = [("x5", "x5-rollout-code-mode", rollout)]
    if len(sys.argv) > 6:
        rollouts.append(("x9", "x9-rollout-code-mode-host-disabled", sys.argv[6]))
    stage = tempfile.mkdtemp()
    plan = [("claude", croot, claude_stream), ("codex", xroot, codex_stream), ("vibe", vroot, vibe_stream)]
    for client, root, stream in plan:
        tags = sorted({f.split(".")[0] for f in os.listdir(f"{root}/logs") if f.endswith(".argv")})
        for tag in tags:
            items = [("run", run_meta(root, tag, client))] + hook_items(root, tag) + stream(root, tag)
            write_all(os.path.join(out, client, "live-p3", tag), items, stage)
    # Codex app-server hooks/list (trust): project hook rows only
    for tag in ("x0a-hooks-list", "x0b-hooks-list"):
        p = f"{xroot}/logs/{tag}.json"
        if os.path.exists(p):
            data = json.load(open(p))["result"]["data"][0]
            rows = [{k: h.get(k) for k in ("key", "eventName", "currentHash", "trustStatus", "enabled",
                                             "source", "sourcePath")}
                    for h in data["hooks"] if h.get("source") == "project"]
            write_all(os.path.join(out, "codex", "live-p3", tag),
                      [("run", {"client": "codex", "tag": tag, "argv": ["codex", "app-server", "-c", "notify=[]"],
                                "request": {"method": "hooks/list", "params": {"cwds": ["<project>"]}},
                                "synthetic": False}),
                       ("hooks-list-project-rows", {"errors": data.get("errors"), "warnings": data.get("warnings"),
                                                    "hooks": rows})], stage)
    # Codex code mode: outer exec calls and their outputs from the probe session's own rollout
    for rtag, rdir, rpath in rollouts:
        if not rpath:
            continue
        keep = []
        for j in jl(rpath):
            p = j.get("payload", {})
            if p.get("type") in ("custom_tool_call", "custom_tool_call_output", "function_call",
                                 "function_call_output"):
                keep.append((p.get("type") + ("-" + p["name"] if p.get("name") else ""), p))
        write_all(os.path.join(out, "codex", "live-p3", rdir),
                  [("run", {"client": "codex", "tag": rtag, "source": f"the {rtag} probe session's rollout file, "
                            "tool-call entries only", "synthetic": False})] + keep, stage)


if __name__ == "__main__":
    main()
