"""Fail-closed shell-policy decisions for neutral hook events."""

from __future__ import annotations

import json

from compchem_memory.hook.events import HookEvent, ToolCall
from compchem_memory.hook.policy import REASON, decide


def _event(kind: str = "pre-tool", raw_name: str = "Bash") -> HookEvent:
    return HookEvent(
        "vibe", kind, "s1", "/project",
        tool=ToolCall("bash", None, raw_name, "c1", {"cmd": "pwd"}),
    )


def test_policy_blocks_bash_on_pre_tool(tmp_path):
    policy = tmp_path / "policy.json"
    policy.write_text(json.dumps({"deny_tools": ["bash"]}), encoding="utf-8")

    block = decide(_event(raw_name="file_system.bash"), {"MAGNOLIA_HOOK_POLICY": str(policy)})

    assert block is not None and block.reason == REASON


def test_unreadable_policy_still_blocks_bash(tmp_path):
    block = decide(_event(), {"MAGNOLIA_HOOK_POLICY": str(tmp_path / "missing.json")})

    assert block is not None and block.reason == REASON


def test_bad_policy_json_still_blocks_bash(tmp_path):
    policy = tmp_path / "policy.json"
    policy.write_text("not json", encoding="utf-8")

    assert decide(_event(), {"MAGNOLIA_HOOK_POLICY": str(policy)}).reason == REASON


def test_no_policy_blocks_nothing():
    assert decide(_event(), {}) is None


def test_policy_ignores_post_tool(tmp_path):
    policy = tmp_path / "policy.json"
    policy.write_text(json.dumps({"deny_tools": ["bash"]}), encoding="utf-8")

    assert decide(_event("post-tool"), {"MAGNOLIA_HOOK_POLICY": str(policy)}) is None
