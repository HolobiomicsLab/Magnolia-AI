"""The always-loaded rules must read correctly in every client, not only OpenCode."""
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
FILES = [ROOT / "AGENTS.md", *sorted((ROOT / "rules").glob("*.md"))]
FORBIDDEN = ["opencode's bash tool", "restart opencode", "loaded by opencode", "opencode session"]


def test_no_opencode_only_phrasing():
    for path in FILES:
        text = path.read_text(encoding="utf-8").lower()
        for phrase in FORBIDDEN:
            assert phrase not in text, f"{path.name}: {phrase!r}"


def test_agents_md_has_tool_names_section():
    text = (ROOT / "AGENTS.md").read_text(encoding="utf-8")
    assert "## Tool names in other clients" in text
    for name in ("compchem-tools_run_shell", "mcp__compchem-tools__run_shell",
                 "mcp_compchem-tools.run_shell"):
        assert name in text
    assert text.index("## Shell commands") < text.index("## Tool names in other clients") < text.index("## Periodically")
