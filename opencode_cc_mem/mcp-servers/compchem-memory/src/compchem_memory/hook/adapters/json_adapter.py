"""Generic JSON hook adapter used by tests and simple clients."""

from __future__ import annotations

import json

from compchem_memory.hook.events import HookEvent, HookResponse

from . import Rendered

_FIELDS = ("context", "notice", "tool_note")


def parse(raw: dict, event: str) -> HookEvent:
    """Build a neutral event, overriding any payload event with the CLI event."""
    return HookEvent.from_json({**raw, "event": event})


def render(resp: HookResponse, event: HookEvent) -> Rendered:
    """Emit all present text fields, or block with exit 2 and a stderr reason."""
    payload = {
        field: value
        for field in _FIELDS
        if (value := getattr(resp, field)) is not None
    }
    stdout = f"{json.dumps(payload, ensure_ascii=False)}\n" if payload else ""
    if resp.block is not None:
        return Rendered(stdout, f"{resp.block.reason}\n", 2, frozenset(payload))
    return Rendered(stdout, "", 0, frozenset(payload))
