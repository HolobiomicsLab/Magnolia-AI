"""Verify AGENTS.md exists and contains the required prompting sections + tool names."""

from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[3]  # opencode_cc_mem/
AGENTS_MD = REPO_ROOT / "AGENTS.md"


def test_agents_md_exists():
    assert AGENTS_MD.exists(), f"AGENTS.md must exist at {AGENTS_MD}"


def test_agents_md_references_memory_tools():
    text = AGENTS_MD.read_text()
    for tool in (
        "memory_get_context",
        "memory_record_learning",
        "memory_confirm",
        "run_shell",
    ):
        assert tool in text, f"AGENTS.md missing reference to {tool}"


def test_agents_md_has_required_sections():
    text = AGENTS_MD.read_text()
    for section in (
        "Before any task",
        "After resolving a tool error",
        "significant scientific result",
        "approach does NOT work",
        "memory is wrong",
        "Shell commands",
    ):
        assert section in text, f"AGENTS.md missing section: {section!r}"


def test_agents_md_specifies_error_resolution_structure():
    text = AGENTS_MD.read_text()
    for marker in ("Symptoms", "Cause", "Fix", "Also"):
        assert marker in text, f"AGENTS.md missing error_resolution sub-section: {marker}"


def test_agents_md_specifies_caveat_for_success():
    text = AGENTS_MD.read_text()
    assert "CAVEAT" in text, "AGENTS.md must mandate CAVEAT for success_pattern entries"


import inspect
from compchem_memory import server as mem_server


def test_every_memory_tool_docstring_has_call_when():
    """Every MCP tool in compchem_memory.server must have 'Call this when:' in its docstring."""
    failures = []
    checked = 0
    for name in dir(mem_server):
        if name.startswith("_"):
            continue
        obj = getattr(mem_server, name)
        fn = None
        if hasattr(obj, "fn") and callable(getattr(obj, "fn", None)):
            fn = obj.fn
        elif inspect.isfunction(obj) and hasattr(obj, "__wrapped__"):
            fn = obj
        if fn is None:
            continue
        checked += 1
        doc = (fn.__doc__ or "")
        if "Call this when:" not in doc:
            failures.append(name)
    assert checked >= 15, f"Test scanned only {checked} tools — FastMCP API may have changed"
    assert not failures, f"Missing 'Call this when:' in: {failures}"
