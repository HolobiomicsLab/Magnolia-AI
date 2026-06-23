"""Resource-request helpers shared across scheduler backends."""
from __future__ import annotations
import logging
import re

_log = logging.getLogger(__name__)

_MEM_RE = re.compile(r'^(\d+(?:\.\d+)?)\s*([KMGTP]?B?)$', re.IGNORECASE)
_MEM_MULT: dict[str, int] = {
    '': 1, 'B': 1,
    'K': 1 << 10, 'KB': 1 << 10,
    'M': 1 << 20, 'MB': 1 << 20,
    'G': 1 << 30, 'GB': 1 << 30,
    'T': 1 << 40, 'TB': 1 << 40,
}

# Per-tool minimum memory per allocated CPU core, in bytes.
# Populated from project memory (OOM field observations):
#   "HADDOCK3 memory baseline on Azzurra — 2GB/core minimum for refinement runs"
# Each HADDOCK3 CNS worker peaks at ~2 GB during flexref/emref/mdref.
_TOOL_MEM_PER_CORE: dict[str, int] = {
    'haddock3': 2 * (1 << 30),  # 2 GB/core
}


def parse_mem_to_bytes(memory: str) -> int | None:
    """Parse '4GB' / '4G' / '64GB' / '512MB' -> bytes. None if unparseable."""
    if not memory:
        return None
    m = _MEM_RE.match(memory.strip())
    if not m:
        return None
    val, unit = m.group(1), m.group(2).upper()
    mult = _MEM_MULT.get(unit)
    if mult is None:
        return None
    return int(float(val) * mult)


def apply_tool_memory_floor(
    tool: str | None, ncores: int, memory: str,
) -> str:
    """Enforce a per-tool minimum total memory based on ncores.

    HADDOCK3 launches up to `ncores` concurrent CNS workers, each ~2 GB peak
    during refinement. A 32-core job needs >= 64 GB. The submit_job default
    of 4 GB (a per-node total, not per-core) caused repeated OOM kills on
    Azzurra cpucourt in June 2026 -- see project memory entry
    "HADDOCK3 OOM kill on Azzurra -- 32 concurrent CNS jobs exceed 32GB memory".

    If `memory` is below the tool's floor, bump up to the floor and warn.
    If `memory` is at/above the floor, the tool has no floor, or the string
    can't be parsed, return `memory` unchanged.
    """
    if not tool:
        return memory
    per_core = _TOOL_MEM_PER_CORE.get(tool.lower())
    if not per_core:
        return memory
    requested = parse_mem_to_bytes(memory)
    if requested is None:
        return memory
    floor = per_core * max(ncores, 1)
    if requested >= floor:
        return memory
    floor_gb = floor // (1 << 30)
    per_core_gb = per_core // (1 << 30)
    _log.warning(
        "submit_job memory floor: tool=%s ncores=%d requested=%s below "
        "floor %dGB (%dGB/core x %d cores); bumping --mem to %dGB",
        tool, ncores, memory, floor_gb, per_core_gb, ncores, floor_gb,
    )
    return f"{floor_gb}GB"
