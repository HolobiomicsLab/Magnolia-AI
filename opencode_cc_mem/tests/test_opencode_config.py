"""Verify opencode.json keeps bash disabled and references our memory tools."""

import json
from pathlib import Path

CONFIG_PATH = Path(__file__).resolve().parents[1] / "opencode.json"


def test_bash_tool_is_disabled():
    cfg = json.loads(CONFIG_PATH.read_text())
    assert cfg.get("tools", {}).get("bash") is False, (
        "opencode.json must disable bash; otherwise the LLM can bypass run_shell."
    )


def test_mcp_servers_registered():
    cfg = json.loads(CONFIG_PATH.read_text())
    assert "compchem-memory" in cfg["mcp"]
    assert "compchem-tools" in cfg["mcp"]


def test_instructions_include_boot_context():
    cfg = json.loads(CONFIG_PATH.read_text())
    instructions = cfg.get("instructions", [])
    joined = " ".join(instructions)
    assert "boot-context.md" in joined, "instructions must load boot-context.md"


def test_instructions_include_audit_report():
    cfg = json.loads(CONFIG_PATH.read_text())
    instructions = cfg.get("instructions", [])
    joined = " ".join(instructions)
    assert "audit-report.md" in joined, "instructions must load audit-report.md"
