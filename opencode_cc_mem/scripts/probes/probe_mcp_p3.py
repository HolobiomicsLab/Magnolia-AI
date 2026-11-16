"""Stdio MCP server for Plan 3 Task 0 probes.

Tools:
  echo(text)      -> text (the role-allowlisted tool)
  shell(command)  -> never executes anything; writes a marker file
                     $PROBE_MCP_MARKER_DIR/mcp-shell-ran.txt so a hook failure
                     is visible as a side effect (an "MCP shell" stand-in)
  unlisted(text)  -> text (a member tool absent from every role allowlist)
"""
import os

from fastmcp import FastMCP

mcp = FastMCP("probe")


@mcp.tool()
def echo(text: str) -> str:
    """Return the text unchanged."""
    return text


@mcp.tool()
def shell(command: str) -> str:
    """Run a shell command (probe stand-in: records the request, executes nothing)."""
    d = os.environ.get("PROBE_MCP_MARKER_DIR")
    if d:
        with open(os.path.join(d, "mcp-shell-ran.txt"), "a", encoding="utf-8") as fh:
            fh.write(command + "\n")
    return "recorded (not executed)"


@mcp.tool()
def unlisted(text: str) -> str:
    """Return the text unchanged (not on any role allowlist)."""
    return text


if __name__ == "__main__":
    mcp.run()
