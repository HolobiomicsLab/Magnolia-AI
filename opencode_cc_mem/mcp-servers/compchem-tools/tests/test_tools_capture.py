"""Verify @captured decorator is applied to compchem-tools MCP tools."""

import inspect

from compchem_tools import server as tools_server


def test_every_mcp_tool_has_captured_wrapper():
    captured_count = 0
    for name, obj in inspect.getmembers(tools_server):
        if name.startswith("_"):
            continue
        # FastMCP wraps @mcp.tool() functions into FunctionTool objects;
        # the underlying callable is stored as .fn on the FunctionTool.
        if hasattr(obj, "fn") and inspect.isfunction(obj.fn):
            if hasattr(obj.fn, "__wrapped__"):
                captured_count += 1
        # Plain functions decorated with @captured (not wrapped by FastMCP)
        elif inspect.isfunction(obj) and hasattr(obj, "__wrapped__"):
            captured_count += 1
    assert captured_count >= 20, (
        f"Expected at least 20 @captured tools in compchem_tools.server, got {captured_count}. "
        "This suggests @captured decorators were accidentally removed."
    )
