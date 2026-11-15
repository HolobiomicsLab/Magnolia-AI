"""Hook probe: append {argv, stdin} as one JSON line to $PROBE_LOG.

Prints nothing, except when $PROBE_INJECT names a file: then it prints
that file's content as a SessionStart additionalContext JSON (Claude/Codex).
Optional $PROBE_DENY=<ToolName-substring> makes a PreToolUse hook print a deny.
"""
import json
import os
import sys

raw = sys.stdin.read()
try:
    payload = json.loads(raw)
except ValueError:
    payload = {"_unparsed": raw}
with open(os.environ["PROBE_LOG"], "a", encoding="utf-8") as fh:
    fh.write(json.dumps({"argv": sys.argv[1:], "stdin": payload}) + "\n")

inject = os.environ.get("PROBE_INJECT")
if inject and isinstance(payload, dict) and payload.get("hook_event_name") == "SessionStart":
    with open(inject, encoding="utf-8") as fh:
        text = fh.read()
    print(json.dumps({"hookSpecificOutput": {"hookEventName": "SessionStart",
                                            "additionalContext": text}}))

deny = os.environ.get("PROBE_DENY")
if deny and isinstance(payload, dict) and payload.get("hook_event_name") == "PreToolUse":
    if deny in json.dumps(payload.get("tool_name", "")):
        print(json.dumps({"hookSpecificOutput": {
            "hookEventName": "PreToolUse", "permissionDecision": "deny",
            "permissionDecisionReason": "probe deny"}}))
