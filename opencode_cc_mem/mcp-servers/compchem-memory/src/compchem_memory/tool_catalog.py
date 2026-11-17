"""Canonical compchem-memory tool catalog and allowlist resolution."""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Mapping, Sequence


MEMORY_TOOL_PREFIX = "mcp:compchem-memory."
MEMORY_TOOL_ORDER = (
    "memory_get_context",
    "memory_record_session",
    "memory_record_learning",
    "memory_search",
    "memory_get_run_history",
    "memory_record_run",
    "memory_consolidate",
    "memory_verify_claim",
    "memory_confirm",
    "memory_park",
    "memory_unpark",
    "memory_select_relevant",
    "memory_extract_from_session",
    "memory_compact_session",
    "memory_search_errors",
    "memory_distill_session",
    "memory_review_consolidation",
    "memory_apply_consolidation",
    "memory_review_promotions",
    "memory_apply_promotions",
    "memory_scan_headers",
    "memory_health_check",
    "memory_notebook",
    "memory_annotate",
    "memory_set_goal",
    "memory_get_goal",
)
MEMORY_TOOL_NAMES = frozenset(MEMORY_TOOL_ORDER)
MEMORY_TOOL_ALLOWLIST_ENV = "MAGNOLIA_MEMORY_TOOL_ALLOWLIST"
MEMORY_TOOL_ALLOWLIST_REQUIRED_ENV = "MAGNOLIA_MEMORY_ALLOWLIST_REQUIRED"
MEMORY_TOOL_ALLOWLIST_ARG = "--tool-allowlist-json"
CANONICAL_MEMORY_TOOL_ALLOWLIST = tuple(
    f"{MEMORY_TOOL_PREFIX}{name}" for name in MEMORY_TOOL_ORDER
)
CAMPAIGN_ROLE_NAMES = frozenset({"worker", "reviewer", "helper", "evaluator"})

MUTATING_MEMORY_TOOLS = frozenset(
    {
        "memory_record_session",
        "memory_record_learning",
        "memory_record_run",
        "memory_consolidate",
        "memory_confirm",
        "memory_park",
        "memory_unpark",
        "memory_extract_from_session",
        "memory_compact_session",
        "memory_distill_session",
        "memory_review_consolidation",
        "memory_apply_consolidation",
        "memory_review_promotions",
        "memory_apply_promotions",
        "memory_health_check",
        "memory_annotate",
        "memory_set_goal",
    }
)
READ_ONLY_MEMORY_TOOLS = frozenset(
    {
        "memory_get_context",
        "memory_search",
        "memory_get_run_history",
        "memory_verify_claim",
        "memory_select_relevant",
        "memory_search_errors",
        "memory_scan_headers",
        "memory_notebook",
        "memory_get_goal",
    }
)
# Mutating tools whose body can call an LLM (directly or through extraction,
# distillation or compaction). They are not leased as a whole: each of their
# store writes takes the project lease for its short critical section only.
SELF_LEASED_MEMORY_TOOLS = frozenset(
    {
        "memory_extract_from_session",
        "memory_compact_session",
        "memory_distill_session",
    }
)


@dataclass(frozen=True)
class ResolvedMemoryToolAllowlist:
    """The same exact allowlist in neutral-policy and server spellings."""

    neutral_names: tuple[str, ...]
    server_names: tuple[str, ...]


def resolve_memory_tool_allowlist(
    entries: Sequence[str] | None,
    *,
    required: bool,
    legacy_stdio: bool = False,
) -> ResolvedMemoryToolAllowlist:
    """Validate exact known names and resolve both consumer spellings."""
    if entries is None:
        if legacy_stdio and not required:
            server_names = MEMORY_TOOL_ORDER
        else:
            raise ValueError("memory tool allowlist is required")
    else:
        if isinstance(entries, (str, bytes)) or not isinstance(entries, Sequence):
            raise ValueError("memory tool allowlist must be a list")
        neutral_names = tuple(entries)
        if not all(isinstance(name, str) and name for name in neutral_names):
            raise ValueError("memory tool allowlist contains an invalid entry")
        if len(set(neutral_names)) != len(neutral_names):
            raise ValueError("memory tool allowlist contains duplicate entries")
        server_names = tuple(
            name.removeprefix(MEMORY_TOOL_PREFIX) for name in neutral_names
        )
        if any(
            not name.startswith(MEMORY_TOOL_PREFIX)
            for name in neutral_names
        ):
            raise ValueError("memory tool allowlist entries must use canonical names")
        unknown = sorted(set(server_names) - MEMORY_TOOL_NAMES)
        if unknown:
            raise ValueError(
                "memory tool allowlist contains unknown tools: " + ", ".join(unknown)
            )
        if required and not server_names:
            raise ValueError("memory tool allowlist must be non-empty")
    neutral_names = tuple(f"{MEMORY_TOOL_PREFIX}{name}" for name in server_names)
    return ResolvedMemoryToolAllowlist(neutral_names, tuple(server_names))


def resolve_memory_tool_environment(
    environment: Mapping[str, str], *, legacy_stdio: bool
) -> ResolvedMemoryToolAllowlist:
    """Resolve one server process's explicit JSON allowlist environment."""
    required = (
        environment.get(MEMORY_TOOL_ALLOWLIST_REQUIRED_ENV) == "1"
        or environment.get("MAGNOLIA_ROLE") in CAMPAIGN_ROLE_NAMES
    )
    raw = environment.get(MEMORY_TOOL_ALLOWLIST_ENV)
    entries = None
    if raw is not None:
        try:
            entries = json.loads(raw)
        except json.JSONDecodeError as exc:
            raise ValueError("memory tool allowlist is not valid JSON") from exc
    return resolve_memory_tool_allowlist(
        entries, required=required, legacy_stdio=legacy_stdio
    )


def resolve_memory_server_launch(
    arguments: Sequence[str], environment: Mapping[str, str]
) -> ResolvedMemoryToolAllowlist:
    """Resolve a stdio server's argv-bound allowlist, with legacy env fallback."""
    if not arguments:
        return resolve_memory_tool_environment(environment, legacy_stdio=True)
    if len(arguments) != 2 or arguments[0] != MEMORY_TOOL_ALLOWLIST_ARG:
        raise ValueError("memory server launch arguments are invalid")
    try:
        entries = json.loads(arguments[1])
    except json.JSONDecodeError as exc:
        raise ValueError("memory tool allowlist is not valid JSON") from exc
    return resolve_memory_tool_allowlist(entries, required=True)


def memory_tool_is_mutating(
    name: str, arguments: Mapping[str, object] | None = None
) -> bool:
    """Return whether a tool can change durable project memory state."""
    return name in MUTATING_MEMORY_TOOLS


def memory_tool_requires_lease(
    name: str, arguments: Mapping[str, object] | None = None
) -> bool:
    """Return whether a whole call runs under the project writer lease.

    Short mutating tools are leased as a whole. Tools that can call an LLM lease
    only their store writes; read tools, ``memory_get_context`` included, write
    nothing but append-only telemetry and are never leased.
    """
    return (
        memory_tool_is_mutating(name, arguments)
        and name not in SELF_LEASED_MEMORY_TOOLS
    )
