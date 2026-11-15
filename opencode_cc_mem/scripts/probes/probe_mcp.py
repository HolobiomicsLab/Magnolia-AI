"""Stdio MCP server for hook probes: one tool, echo(text) -> text.

If the text is 'fail' the tool raises, to probe the tool-error path.
"""
from fastmcp import FastMCP

mcp = FastMCP("probe")


@mcp.tool()
def echo(text: str) -> str:
    """Return the text unchanged."""
    if text == "fail":
        raise ValueError("probe failure requested")
    return text


if __name__ == "__main__":
    mcp.run()
