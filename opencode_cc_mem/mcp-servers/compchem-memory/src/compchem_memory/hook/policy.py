"""Fail-closed pre-tool policy shared by client adapters.

When ``MAGNOLIA_HOOK_POLICY`` is set, a denied tool (the built-in shell) is
blocked even if the policy file is unreadable, ``handle`` raises, or the
adapter cannot parse the payload but a tool name is recoverable from it.

Remaining paths on which a denied shell still runs, each a deliberate
fail-open choice: stdin that is not a JSON object, or an unknown harness; a
payload with no recoverable tool name (``tool_name``, ``tool.raw_name``,
``tool.name``); the adapter's ``render`` raising; and, for clients whose denial
is stdout-only with exit 0, a failed stdout write. A known denial is written
before ``handle`` runs, so slow capture cannot delay it. Clients without a sandbox (Vibe) also get the
shell removed from their tool list by the rendered configuration.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Mapping

from compchem_memory.hook.events import Block, HookEvent, canonical_tool


REASON = (
    "Magnolia disables this client's built-in shell. Use the compchem-tools run_shell "
    "tool instead; it records the command for the project's memory."
)


def _canonical_name(event: HookEvent) -> str | None:
    """Return the event tool's canonical policy name."""
    if event.tool is None:
        return None
    server, name = canonical_tool(event.tool.raw_name)
    return f"{server}.{name}" if server else name


def _read_denied(path: str) -> frozenset[str]:
    """Read and validate one policy file's canonical denied names."""
    payload = json.loads(Path(path).read_text(encoding="utf-8"))
    if not isinstance(payload, dict) or not isinstance(payload.get("deny_tools"), list):
        raise ValueError("hook policy must contain a deny_tools list")
    denied = payload["deny_tools"]
    if not all(isinstance(name, str) for name in denied):
        raise ValueError("hook policy deny_tools entries must be strings")
    return frozenset(denied)


def decide(event: HookEvent, env: Mapping[str, str]) -> Block | None:
    """Block a denied pre-tool call; unreadable configured policy denies bash."""
    if event.event != "pre-tool" or not (policy_path := env.get("MAGNOLIA_HOOK_POLICY")):
        return None
    tool_name = _canonical_name(event)
    try:
        denied = _read_denied(policy_path)
    except (OSError, ValueError, TypeError):
        denied = frozenset({"bash"})
    return Block(REASON) if tool_name in denied else None
