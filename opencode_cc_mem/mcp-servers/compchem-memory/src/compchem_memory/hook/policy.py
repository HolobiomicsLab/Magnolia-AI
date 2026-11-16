"""Hook policy for interactive compatibility and required campaign roles.

Interactive mode retains the historical fail-open policy-file behavior. Required
mode validates the complete role policy and denies malformed or unsafe tool calls.
"""

from __future__ import annotations

import fnmatch
import json
import os
import time
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path
from typing import Any, Mapping

from compchem_memory.hook.events import Block, HookEvent, ToolTarget, canonical_tool


REASON = (
    "Magnolia disables this client's built-in shell. Use the compchem-tools run_shell "
    "tool instead; it records the command for the project's memory."
)
REQUIRED_REASON = "Magnolia required role policy denied this operation."
REQUIRED_ENV = "MAGNOLIA_HOOK_POLICY_REQUIRED"
REQUIRED_SCHEMA = "magnolia-role-v1"
BROAD_SEARCH_MAX_ENTRIES = 10_000
BROAD_SEARCH_MAX_SECONDS = 1.0


@dataclass(frozen=True)
class RequiredPolicy:
    """Validated fields consumed by the required decision path."""

    read_roots: tuple[Path, ...]
    deny_patterns: tuple[str, ...]
    edit_roots: tuple[Path, ...]
    capabilities: frozenset[str]
    mcp_tools: frozenset[str]
    deny_unknown: bool
    deny_shell: bool
    deny_code: bool


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


def required_mode(env: Mapping[str, str]) -> bool:
    """Return whether fail-closed role enforcement was requested."""
    return env.get(REQUIRED_ENV) == "1"


def _read_required(path: str | None) -> RequiredPolicy:
    if not path:
        raise ValueError("required hook policy path is missing")
    payload = json.loads(Path(path).read_text(encoding="utf-8"))
    if not isinstance(payload, dict) or payload.get("schema_version") != REQUIRED_SCHEMA:
        raise ValueError("required hook policy schema mismatch")
    _required_text(payload, "role")
    _required_text(payload, "policy_role")
    _required_text(payload, "required_capability_identity")
    if payload.get("deny_path_forms") != ["requested", "resolved"]:
        raise ValueError("required hook policy path forms mismatch")
    if payload.get("delegation_policy") != "deny":
        raise ValueError("required hook policy must deny delegation")
    booleans = {
        key: _required_bool(payload, key)
        for key in ("deny_unknown_tools", "deny_shell", "deny_unconfined_code_execution")
    }
    if not all(booleans.values()):
        raise ValueError("required hook policy denial switches must be enabled")
    _required_strings(payload, "deny_tools")
    read_roots = _required_roots(payload, "canonical_read_roots")
    edit_roots = _required_roots(payload, "edit_roots")
    return RequiredPolicy(
        read_roots=read_roots,
        deny_patterns=_required_strings(payload, "read_deny_patterns"),
        edit_roots=edit_roots,
        capabilities=frozenset(_required_strings(payload, "effective_capabilities")),
        mcp_tools=frozenset(_required_strings(payload, "mcp_tool_allowlist")),
        deny_unknown=booleans["deny_unknown_tools"],
        deny_shell=booleans["deny_shell"],
        deny_code=booleans["deny_unconfined_code_execution"],
    )


def _required_text(payload: dict[str, Any], key: str) -> str:
    value = payload.get(key)
    if not isinstance(value, str) or not value:
        raise ValueError(f"required hook policy {key} must be non-empty text")
    return value


def _required_strings(payload: dict[str, Any], key: str) -> tuple[str, ...]:
    values = payload.get(key)
    if not isinstance(values, list) or not all(
        isinstance(value, str) and value for value in values
    ):
        raise ValueError(f"required hook policy {key} must be a string list")
    return tuple(values)


def _required_bool(payload: dict[str, Any], key: str) -> bool:
    value = payload.get(key)
    if not isinstance(value, bool):
        raise ValueError(f"required hook policy {key} must be boolean")
    return value


def _required_roots(payload: dict[str, Any], key: str) -> tuple[Path, ...]:
    roots = tuple(Path(value) for value in _required_strings(payload, key))
    if any(not root.is_absolute() for root in roots):
        raise ValueError(f"required hook policy {key} must be absolute")
    return roots


def _required_decision(event: HookEvent, policy: RequiredPolicy) -> Block | None:
    if event.tool is None or event.agent is not None:
        return Block(REQUIRED_REASON)
    server, name = canonical_tool(event.tool.raw_name)
    if server is not None:
        mcp_name = f"mcp:{server}.{name}"
        if policy.deny_shell and _is_shell(name):
            return Block(REQUIRED_REASON)
        return None if mcp_name in policy.mcp_tools else Block(REQUIRED_REASON)
    if policy.deny_shell and _is_shell(name):
        return Block(REQUIRED_REASON)
    if _is_delegation(name) or (policy.deny_code and _is_code(name)):
        return Block(REQUIRED_REASON)
    action = event.tool.policy_action
    if action not in policy.capabilities:
        return Block(REQUIRED_REASON) if policy.deny_unknown else None
    if not event.tool.targets:
        return Block(REQUIRED_REASON)
    return None if _targets_allowed(event, policy) else Block(REQUIRED_REASON)


def _is_shell(name: str) -> bool:
    return name in {"bash", "shell", "run_shell", "process_start"}


def _is_delegation(name: str) -> bool:
    return name in {
        "agent", "task", "spawn_agent", "agents.spawn",
        "multi_agent_v1spawn_agent", "multi_agent_v1wait_agent",
    }


def _is_code(name: str) -> bool:
    return name in {"code", "run_typescript", "python", "notebook"}


def _targets_allowed(event: HookEvent, policy: RequiredPolicy) -> bool:
    assert event.tool is not None
    for target in event.tool.targets:
        if target.access not in {"read", "search", "edit"}:
            return False
        roots = policy.edit_roots if target.access == "edit" else policy.read_roots
        if not roots or not _path_forms_allowed(target.path, event.cwd, roots, policy):
            return False
        if target.broad and not _broad_search_allowed(target, event.cwd, roots, policy):
            return False
    return True


def _path_forms_allowed(
    value: str,
    cwd: str,
    roots: tuple[Path, ...],
    policy: RequiredPolicy,
) -> bool:
    if not value or "\x00" in value or value.startswith("~"):
        return False
    raw = value if os.path.isabs(value) else os.path.join(cwd, value)
    requested = Path(os.path.abspath(raw))
    resolved = Path(os.path.realpath(raw))
    if any(_matches_denied(path, policy.deny_patterns) for path in (requested, resolved)):
        return False
    lexical_roots = tuple(Path(os.path.abspath(str(root))) for root in roots)
    resolved_roots = tuple(root.resolve(strict=False) for root in roots)
    return _within_any(requested, lexical_roots) and _within_any(resolved, resolved_roots)


def _within_any(path: Path, roots: tuple[Path, ...]) -> bool:
    return any(
        path == root or (root.is_dir() and path.is_relative_to(root))
        for root in roots
    )


def _matches_denied(path: Path, patterns: tuple[str, ...]) -> bool:
    path_parts = tuple(
        part.casefold() for part in path.as_posix().split("/") if part
    )
    return any(_recursive_glob(path_parts, pattern) for pattern in patterns)


def _recursive_glob(path_parts: tuple[str, ...], pattern: str) -> bool:
    """Match case-folded segments with recursive ``**`` at any relative depth."""
    normalised = pattern.replace("\\", "/").casefold()
    pattern_parts = tuple(part for part in normalised.split("/") if part)
    if not pattern_parts:
        return False

    @lru_cache(maxsize=None)
    def matches(path_index: int, pattern_index: int) -> bool:
        if pattern_index == len(pattern_parts):
            return path_index == len(path_parts)
        token = pattern_parts[pattern_index]
        if token == "**":
            return matches(path_index, pattern_index + 1) or (
                path_index < len(path_parts)
                and matches(path_index + 1, pattern_index)
            )
        return (
            path_index < len(path_parts)
            and fnmatch.fnmatchcase(path_parts[path_index], token)
            and matches(path_index + 1, pattern_index + 1)
        )

    starts = (0,) if normalised.startswith("/") else range(len(path_parts) + 1)
    return any(matches(start, 0) for start in starts)


def _broad_search_allowed(
    target: ToolTarget,
    cwd: str,
    roots: tuple[Path, ...],
    policy: RequiredPolicy,
) -> bool:
    if not policy.deny_patterns and set(policy.deny_patterns).issubset(target.exclusions):
        return True
    if any(
        _unsafe_search_pattern(pattern, cwd, policy)
        for pattern in target.inclusions
    ):
        return False
    root = Path(target.path)
    root = root if root.is_absolute() else Path(cwd) / root
    root = root.resolve(strict=False)
    if not root.exists():
        return True
    if not root.is_dir():
        return True
    started = time.monotonic()
    resolved_roots = tuple(path.resolve(strict=False) for path in roots)
    try:
        for count, path in enumerate(root.rglob("*"), start=1):
            if (
                count > BROAD_SEARCH_MAX_ENTRIES
                or time.monotonic() - started > BROAD_SEARCH_MAX_SECONDS
            ):
                return False
            if not _search_entry_allowed(path, resolved_roots, policy):
                return False
        return True
    except (OSError, RuntimeError):
        return False


def _search_entry_allowed(
    path: Path,
    resolved_roots: tuple[Path, ...],
    policy: RequiredPolicy,
) -> bool:
    resolved = path.resolve(strict=False)
    escapes = path.is_symlink() and not _within_any(resolved, resolved_roots)
    denied = any(
        _matches_denied(candidate, policy.deny_patterns)
        for candidate in (path, resolved)
    )
    return not escapes and not denied


def _unsafe_search_pattern(
    pattern: str,
    cwd: str,
    policy: RequiredPolicy,
) -> bool:
    """Reject patterns whose client-side path interpretation can escape."""
    parts = _safe_search_parts(pattern)
    if parts is None:
        return True
    literal_parts = _literal_search_root(parts)
    effective_root = os.path.join(cwd, *literal_parts) if literal_parts else cwd
    return not _path_forms_allowed(effective_root, cwd, policy.read_roots, policy)


def _safe_search_parts(pattern: str) -> tuple[str, ...] | None:
    """Return relative pattern segments, or ``None`` for unsafe forms."""
    parts = tuple(
        part for part in pattern.replace("\\", "/").split("/")
        if part not in {"", "."}
    )
    if (
        not pattern
        or pattern.startswith("~")
        or os.path.isabs(pattern)
        or (len(pattern) > 2 and pattern[1] == ":" and pattern[2] in "/\\")
        or ".." in parts
    ):
        return None
    return parts


def _literal_search_root(parts: tuple[str, ...]) -> tuple[str, ...]:
    """Return the literal directory prefix before the first glob token."""
    literal_parts = []
    for part in parts:
        if any(marker in part for marker in "*?[]{}"):
            break
        literal_parts.append(part)
    return tuple(literal_parts)


def decide(event: HookEvent, env: Mapping[str, str]) -> Block | None:
    """Return the optional shell or required role-policy decision."""
    if event.event != "pre-tool":
        return None
    policy_path = env.get("MAGNOLIA_HOOK_POLICY")
    if required_mode(env):
        try:
            return _required_decision(event, _read_required(policy_path))
        except (OSError, ValueError, TypeError):
            return Block(REQUIRED_REASON)
    if not policy_path:
        return None
    tool_name = _canonical_name(event)
    try:
        denied = _read_denied(policy_path)
    except (OSError, ValueError, TypeError):
        denied = frozenset({"bash"})
    return Block(REASON) if tool_name in denied else None
