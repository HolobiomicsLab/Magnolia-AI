"""Client adapter contract for Magnolia's neutral hook core."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol

from compchem_memory.hook.events import HookEvent, HookResponse


@dataclass(frozen=True)
class Rendered:
    """Rendered client output and the response fields it actually delivers."""

    stdout: str
    stderr: str
    exit_code: int
    emitted: frozenset[str]


class Adapter(Protocol):
    """Translate one client payload and render one neutral response."""

    def parse(self, raw: dict, event: str) -> HookEvent:
        """Build a neutral event from a native payload."""
        ...

    def render(self, resp: HookResponse, event: HookEvent) -> Rendered:
        """Render a neutral response for the native client."""
        ...


from compchem_memory.hook.adapters import json_adapter  # noqa: E402

ADAPTERS: dict[str, Adapter] = {"json": json_adapter}

__all__ = ["ADAPTERS", "Adapter", "Rendered"]

