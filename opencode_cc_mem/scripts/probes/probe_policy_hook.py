"""Plan 3 Task 0 probe hook: log every hook event and enforce a small probe policy.

Not the production hook (Task 6 owns that). It exists to measure each client's
native PreToolUse/pre_tool payloads and denial semantics under normal trust.

Environment:
  PROBE_POLICY  path of the probe policy JSON (see run_probes_p3.sh); when it is
                missing or unreadable every tool call is denied (fail closed)
  PROBE_LOG     JSON-lines log; one line per hook call:
                {argv, stdin, decision, reason, class, policy_sha256, mode}
argv[1] is the client: claude | codex | vibe.

Policy keys: mode (enforce | crash | silent), read_roots, read_deny_globs,
edit_roots, mcp_allow (["server.tool", ...]), allow_native (subset of read,
glob, grep, edit, write, notebook, patch, meta), deny_shell, deny_delegation,
deny_code, deny_unknown; allow_code_outer (probe variant only) lets the outer
code-mode call through so that its inner tool calls can be observed.
mode crash: log, then raise (exit 1, traceback on stderr, nothing on stdout).
mode silent: log, print nothing, exit 0.
Paths are checked in two forms: requested (lexical normpath against cwd) and
resolved (realpath). Both must lie inside an allowed root and no path
component of either may match a deny glob.
"""
import fnmatch
import hashlib
import json
import os
import re
import sys

CLIENT = sys.argv[1] if len(sys.argv) > 1 else "unknown"
PRE_EVENTS = {"PreToolUse", "pre_tool"}

SHELL = {"Bash", "BashOutput", "KillShell", "KillBash", "shell", "exec_command", "local_shell",
         "unified_exec", "write_stdin", "file_system.bash", "bash", "process.start",
         "process.write", "process.output", "process.list", "process.stop", "git_bash"}
DELEGATE = {"Agent", "Task", "spawn_agent", "send_input", "resume_agent", "wait", "close_agent",
            "send_message", "followup_task", "interrupt_agent", "list_agents",
            "multi_agent_v1wait_agent", "agents.spawn", "agents.send_message", "agents.list",
            "agents.list_configs", "agents.interrupt", "task", "subagent.spawn"}
CODE = {"run_typescript", "js_repl", "code_mode", "exec_code", "exec"}
META = {"ToolSearch"}
READ = {"Read": "file_path", "read_file": "path", "file_system.read_file": "path"}
GLOB = {"Glob": "path", "list_dir": "path", "list_files": "path"}
GREP = {"Grep": "path", "grep": "path", "grep_files": "path", "file_system.grep": "path"}
EDIT = {"Edit": "file_path", "MultiEdit": "file_path", "edit": "file_path",
        "file_system.edit": "file_path", "search_replace": "file_path"}
WRITE = {"Write": "file_path", "write_file": "path", "file_system.write_file": "path"}
NOTEBOOK = {"NotebookEdit": "notebook_path"}
PATCH = {"apply_patch"}
PATH_KEYS = ("file_path", "path", "notebook_path", "file", "filename", "target", "source",
             "destination", "dir", "directory")


def load_policy():
    p = os.environ.get("PROBE_POLICY")
    try:
        raw = open(p, "rb").read()
        return json.loads(raw), hashlib.sha256(raw).hexdigest()
    except Exception:  # noqa: BLE001 - any failure means no policy: fail closed
        return None, None


def forms(path, cwd):
    if not os.path.isabs(path):
        path = os.path.join(cwd or os.getcwd(), path)
    return os.path.normpath(path), os.path.realpath(path)


def root_forms(roots):
    out = []
    for r in roots:
        out += [os.path.normpath(r), os.path.realpath(r)]
    return out


def inside(p, roots):
    return any(p == r or p.startswith(r.rstrip("/") + "/") for r in roots)


def deny_glob_hit(p, globs):
    parts = [x for x in p.split("/") if x]
    return any(fnmatch.fnmatch(part, g) for part in parts for g in globs)


def check_paths(paths, cwd, roots, globs, what):
    rf = root_forms(roots)
    if not paths:
        return f"{what}: no path argument found"
    for raw in paths:
        req, res = forms(raw, cwd)
        for form, p in (("requested", req), ("resolved", res)):
            if deny_glob_hit(p, globs):
                return f"{what}: {form} path matches a deny pattern"
            if not inside(p, rf):
                return f"{what}: {form} path outside the allowed roots"
    return None


def tree_has_denied(root, globs, limit=5000):
    n = 0
    for d, dirs, files in os.walk(root, followlinks=False):
        for name in dirs + files:
            n += 1
            if n > limit or any(fnmatch.fnmatch(name, g) for g in globs):
                return True
        for name in list(dirs) + list(files):
            if os.path.islink(os.path.join(d, name)):
                return True  # a link may alias a denied file; refuse recursive search
    return False


def mcp_name(tool, payload):
    m = re.match(r"^mcp__(.+?)__(.+)$", tool)
    if m:
        return f"{m.group(1)}.{m.group(2)}"
    m = re.match(r"^mcp_([^.]+)\.(.+)$", tool)
    if m:
        return f"{m.group(1)}.{m.group(2)}"
    return None


def classify(tool):
    for cls, table in (("read", READ), ("glob", GLOB), ("grep", GREP), ("edit", EDIT),
                       ("write", WRITE), ("notebook", NOTEBOOK)):
        if tool in table:
            return cls, table[tool]
    if tool in PATCH:
        return "patch", None
    if tool in SHELL:
        return "shell", None
    if tool in DELEGATE:
        return "delegate", None
    if tool in CODE:
        return "code", None
    if tool in META:
        return "meta", None
    return "unknown", None


def patch_paths(ti):
    text = json.dumps(ti)
    text = text.encode().decode("unicode_escape", errors="ignore")
    return re.findall(r"\*\*\* (?:Add File|Update File|Delete File|Move to): ([^\n\"]+)", text)


def decide(policy, payload):
    tool = str(payload.get("tool_name", ""))
    ti = payload.get("tool_input") or {}
    if not isinstance(ti, dict):
        ti = {"_raw": ti}
    cwd = payload.get("cwd") or os.getcwd()
    globs = policy.get("read_deny_globs", [])
    mcp = mcp_name(tool, payload)
    if mcp is not None:
        if mcp in policy.get("mcp_allow", []):
            return "allow", "mcp", "mcp tool on the allowlist"
        return "deny", "mcp", f"mcp tool {mcp} not on the role allowlist"
    cls, key = classify(tool)
    allow_native = set(policy.get("allow_native", []))
    if cls == "meta":
        return ("allow", cls, "meta tool") if "meta" in allow_native else ("deny", cls, "meta tool not allowed")
    if cls == "code" and policy.get("allow_code_outer"):
        return "allow", cls, "outer code-mode call allowed (probe variant: inner calls observed)"
    if cls in ("shell", "delegate", "code", "unknown"):
        return "deny", cls, f"{cls} tool {tool} denied by role policy"
    if cls not in allow_native:
        return "deny", cls, f"native {cls} not allowed for this role"
    if cls in ("read", "glob", "grep"):
        p = ti.get(key) or ti.get("path") or ti.get("file_path")
        if cls == "read":
            why = check_paths([p] if p else [], cwd, policy.get("read_roots", []), globs, "read")
        else:
            root = p or cwd
            why = check_paths([root], cwd, policy.get("read_roots", []), globs, cls)
            if why is None:
                pat = " ".join(str(ti.get(k, "")) for k in ("pattern", "glob", "include"))
                if any(fnmatch.fnmatch(".env", g) and ".env" in pat for g in globs) or \
                        tree_has_denied(forms(root, cwd)[1], globs):
                    why = f"{cls}: recursive search root contains a denied or linked path"
        return ("deny", cls, why) if why else ("allow", cls, "inside read roots")
    if cls in ("edit", "write", "notebook"):
        p = ti.get(key) or ti.get("path") or ti.get("file_path")
        why = check_paths([p] if p else [], cwd, policy.get("edit_roots", []), globs, cls)
        return ("deny", cls, why) if why else ("allow", cls, "inside edit roots")
    if cls == "patch":
        paths = patch_paths(ti)
        why = check_paths(paths, cwd, policy.get("edit_roots", []), globs, "patch")
        return ("deny", cls, why) if why else ("allow", cls, "every patch path inside edit roots")
    return "deny", cls, "unclassified"


def emit(decision, reason):
    if decision != "deny":
        return  # pass through: no output means no decision
    if CLIENT == "vibe":
        print(json.dumps({"decision": "deny", "reason": reason}))
    else:
        print(json.dumps({"hookSpecificOutput": {"hookEventName": "PreToolUse",
                                                 "permissionDecision": "deny",
                                                 "permissionDecisionReason": reason}}))


def main():
    raw = sys.stdin.read()
    try:
        payload = json.loads(raw)
    except ValueError:
        payload = {"_unparsed": raw}
    policy, digest = load_policy()
    mode = (policy or {}).get("mode", "enforce")
    ev = payload.get("hook_event_name") if isinstance(payload, dict) else None
    decision = reason = cls = None
    if ev in PRE_EVENTS:
        if policy is None:
            decision, cls, reason = "deny", "no-policy", "probe policy missing or unreadable"
        else:
            decision, cls, reason = decide(policy, payload)
    with open(os.environ["PROBE_LOG"], "a", encoding="utf-8") as fh:
        fh.write(json.dumps({"argv": sys.argv[1:], "stdin": payload, "decision": decision,
                             "class": cls, "reason": reason, "policy_sha256": digest,
                             "mode": mode, "pid": os.getpid()}) + "\n")
    if ev not in PRE_EVENTS:
        return
    if mode == "crash":
        raise RuntimeError("probe hook crash requested by policy")
    if mode == "silent":
        return
    emit(decision, reason)


if __name__ == "__main__":
    main()
