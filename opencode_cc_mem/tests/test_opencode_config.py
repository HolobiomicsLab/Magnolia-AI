"""Verify the opencode.json template keeps bash disabled and references our
memory tools. The rendered opencode.json is machine-local and gitignored, so
the published test validates the tracked template instead."""

import json
from pathlib import Path

TEMPLATE_PATH = Path(__file__).resolve().parents[1] / "opencode.json.template"


def _rendered_template() -> dict:
    text = TEMPLATE_PATH.read_text()
    for placeholder, value in {
        "@@PROJECT@@": "test_project",
        "@@MAGNOLIA_ROOT@@": "/tmp/magnolia-root",
        "@@PYTHON@@": "/tmp/magnolia-root/.venv/bin/python3",
        "@@MEMORY_ENABLED@@": "false",
    }.items():
        text = text.replace(placeholder, value)
    return json.loads(text)


def test_bash_tool_is_disabled():
    cfg = _rendered_template()
    assert cfg.get("tools", {}).get("bash") is False, (
        "opencode.json must disable bash; otherwise the LLM can bypass run_shell."
    )


def test_mcp_servers_registered():
    cfg = _rendered_template()
    assert "compchem-memory" in cfg["mcp"]
    assert "compchem-tools" in cfg["mcp"]


def test_instructions_include_boot_context():
    cfg = _rendered_template()
    instructions = cfg.get("instructions", [])
    joined = " ".join(instructions)
    assert "boot-context.md" in joined, "instructions must load boot-context.md"


def test_instructions_include_audit_report():
    cfg = _rendered_template()
    instructions = cfg.get("instructions", [])
    joined = " ".join(instructions)
    assert "audit-report.md" in joined, "instructions must load audit-report.md"
