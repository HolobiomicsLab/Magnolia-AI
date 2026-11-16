"""Which entries in a member's client configuration belong to Magnolia."""

from __future__ import annotations

import re

MAGNOLIA_SERVERS = frozenset({"compchem-tools", "compchem-memory", "perspicacite"})
VIBE_HOOK_PREFIX = "magnolia-"
_COMMAND_BOUNDARY = r"(?:^|[\s;&|()])"
_COMMAND_END = r"(?=$|[\s;&|()])"
_LEGACY_HOOK = re.compile(
    rf"{_COMMAND_BOUNDARY}-m\s+['\"]?compchem_memory\.hook['\"]?{_COMMAND_END}"
)
_RUNNER_HOOK = re.compile(
    rf"compchem_memory[/\\]hook[/\\]runner\.py['\"]?{_COMMAND_END}"
)


def is_magnolia_hook_command(command: object) -> bool:
    """Recognise legacy module hooks and the isolated repository runner."""
    if not isinstance(command, str):
        return False
    return bool(_LEGACY_HOOK.search(command) or _RUNNER_HOOK.search(command))


def owned(kind: str, name: str | None, entry: dict) -> bool:
    """Ownership predicate for :mod:`compchem_memory.harness.files`.

    Servers are owned by name. Hooks are owned when their command runs the Magnolia
    hook module (for Claude and Codex matcher groups: any inner hook), or when a Vibe
    hook's name starts with ``magnolia-``.
    """
    if kind == "server":
        return name in MAGNOLIA_SERVERS
    if not isinstance(entry, dict):
        return False
    hook_name = name if name is not None else entry.get("name")
    if isinstance(hook_name, str) and hook_name.startswith(VIBE_HOOK_PREFIX):
        return True
    if _runs_hook_module(entry):
        return True
    inner = entry.get("hooks")
    # A matcher group is Magnolia's only when every hook in it is; a mixed group
    # belongs to the member (see strip_owned_hooks).
    return (
        isinstance(inner, list)
        and bool(inner)
        and all(isinstance(hook, dict) and _runs_hook_module(hook) for hook in inner)
    )


def strip_owned_hooks(current: dict) -> dict:
    """Remove Magnolia hooks, including groups under obsolete event keys."""
    hooks = current.get("hooks")
    if not isinstance(hooks, dict):
        return current
    cleaned = {}
    for event, groups in hooks.items():
        if not isinstance(groups, list):
            cleaned[event] = groups
            continue
        kept = [
            _strip_group(group)
            for group in groups
            if not (isinstance(group, dict) and owned("hook", None, group))
        ]
        if kept:
            cleaned[event] = kept
    return {**current, "hooks": cleaned}


def _strip_group(group):
    inner = group.get("hooks") if isinstance(group, dict) else None
    if not isinstance(inner, list) or owned("hook", None, group):
        return group
    kept = [h for h in inner if not (isinstance(h, dict) and _runs_hook_module(h))]
    return group if len(kept) == len(inner) else {**group, "hooks": kept}


def _runs_hook_module(hook: dict) -> bool:
    return is_magnolia_hook_command(hook.get("command"))
