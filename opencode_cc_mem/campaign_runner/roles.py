"""Load neutral role policies and render frozen OpenCode compatibility files."""

from __future__ import annotations

import argparse
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Mapping, Sequence

import yaml

from compchem_memory.tool_catalog import resolve_memory_tool_allowlist


ROLE_NAMES = ("campaign-worker", "campaign-reviewer", "helper", "evaluator")
OPENCODE_ROLES = ("campaign-worker", "campaign-reviewer")
CONFIG_DIR = Path(__file__).resolve().parents[1] / "agents"


@dataclass(frozen=True)
class RolePolicy:
    """Harness-neutral permissions and cap for one execution role."""

    version: str
    role: str
    canonical_read_roots: tuple[str, ...]
    read_deny_patterns: tuple[str, ...]
    opencode_omitted_read_deny_patterns: tuple[str, ...]
    edit_roots: tuple[str, ...]
    allowed_native_capabilities: tuple[str, ...]
    mcp_tool_allowlist: tuple[str, ...]
    delegation_policy: str
    default_cap: int
    opencode_description: str | None = None
    opencode_mode: str | None = None
    opencode_prose: str | None = None


def load_role(role: str, config_dir: Path = CONFIG_DIR) -> RolePolicy:
    """Load and validate one checked-in neutral role policy."""
    if role not in ROLE_NAMES:
        raise ValueError(f"unknown role policy: {role}")
    path = config_dir / f"{role}.yaml"
    value = yaml.safe_load(path.read_text(encoding="utf-8"))
    if not isinstance(value, Mapping):
        raise ValueError(f"role policy is not a mapping: {path}")
    policy = RolePolicy(
        version=_text(value, "version", path),
        role=_text(value, "role", path),
        canonical_read_roots=_strings(value, "canonical_read_roots", path),
        read_deny_patterns=_strings(value, "read_deny_patterns", path),
        opencode_omitted_read_deny_patterns=_optional_strings(
            value, "opencode_omitted_read_deny_patterns", path
        ),
        edit_roots=_strings(value, "edit_roots", path),
        allowed_native_capabilities=_strings(
            value, "allowed_native_capabilities", path
        ),
        mcp_tool_allowlist=_strings(value, "mcp_tool_allowlist", path),
        delegation_policy=_text(value, "delegation_policy", path),
        default_cap=_positive_int(value, "default_cap", path),
        opencode_description=_optional_text(value.get("opencode_description"), path),
        opencode_mode=_optional_text(value.get("opencode_mode"), path),
        opencode_prose=_optional_text(value.get("opencode_prose"), path),
    )
    if policy.role != role:
        raise ValueError(f"role policy name mismatch in {path}: {policy.role!r}")
    if policy.delegation_policy != "deny":
        raise ValueError(f"delegation must be denied in {path}")
    unknown_omissions = set(policy.opencode_omitted_read_deny_patterns) - set(
        policy.read_deny_patterns
    )
    if unknown_omissions:
        raise ValueError(f"OpenCode omissions are not read denials in {path}")
    if policy.opencode_omitted_read_deny_patterns and role not in OPENCODE_ROLES:
        raise ValueError(f"OpenCode omissions are invalid for {role} in {path}")
    resolve_memory_tool_allowlist(
        policy.mcp_tool_allowlist,
        required=role == "campaign-worker",
    )
    return policy


def load_policies(config_dir: Path = CONFIG_DIR) -> dict[str, RolePolicy]:
    """Return every registered execution policy keyed by role name."""
    return {role: load_role(role, config_dir) for role in ROLE_NAMES}


def render_opencode(role: str, config_dir: Path = CONFIG_DIR) -> bytes:
    """Generate an OpenCode campaign agent from its neutral role fields."""
    if role not in OPENCODE_ROLES:
        raise ValueError(f"no OpenCode compatibility target for role: {role}")
    policy = load_role(role, config_dir)
    if not all(
        (policy.opencode_description, policy.opencode_mode, policy.opencode_prose)
    ):
        raise ValueError(f"missing OpenCode compatibility fields for role: {role}")
    lines = [
        "---",
        f"description: {policy.opencode_description}",
        f"mode: {policy.opencode_mode}",
        f"steps: {policy.default_cap}",
        "permissions:",
    ]
    for action, resource, effect in _opencode_permissions(policy):
        lines.extend(
            (
                f'  - action: "{action}"' if action == "*" else f"  - action: {action}",
                f'    resource: "{resource}"',
                f"    effect: {effect}",
            )
        )
    lines.extend(("---", "", policy.opencode_prose.rstrip(), ""))
    return "\n".join(lines).encode("utf-8")


def _opencode_permissions(policy: RolePolicy) -> list[tuple[str, str, str]]:
    """Translate neutral role capabilities to the frozen OpenCode schema."""
    permissions = [("*", "*", "deny")]
    capabilities = set(policy.allowed_native_capabilities)
    if "native_read" in capabilities and policy.canonical_read_roots:
        permissions.append(("read", "*", "allow"))
    permissions.extend(
        ("read", resource, "deny")
        for resource in _opencode_read_denials(
            tuple(
                pattern
                for pattern in policy.read_deny_patterns
                if pattern not in policy.opencode_omitted_read_deny_patterns
            )
        )
    )
    for capability in ("native_glob", "native_grep"):
        if capability in capabilities:
            permissions.append((capability.removeprefix("native_"), "*", "allow"))
    if "native_edit" in capabilities:
        for root in policy.edit_roots:
            permissions.extend(
                ("edit", resource, "allow")
                for resource in _opencode_edit_resources(root)
            )
    permissions.extend(
        (_opencode_mcp_action(tool), "*", "allow")
        for tool in policy.mcp_tool_allowlist
    )
    return permissions


def _opencode_read_denials(patterns: tuple[str, ...]) -> tuple[str, ...]:
    denials: list[str] = []
    for pattern in patterns:
        resource = "*.env*" if pattern in {"**/*.env*", "**/*.env*/**"} else pattern
        if resource not in denials:
            denials.append(resource)
    return tuple(denials)


def _opencode_edit_resources(root: str) -> tuple[str, ...]:
    if root != "{work_dir}":
        return (root,)
    return (
        "projects/*/runs/*/work/**",
        "runs/*/work/**",
        "work/**",
    )


def _opencode_mcp_action(tool: str) -> str:
    prefix = "mcp:"
    if not tool.startswith(prefix) or "." not in tool:
        raise ValueError(f"invalid neutral MCP tool name: {tool}")
    server, name = tool[len(prefix) :].split(".", 1)
    return f"{server}_{name}"


def write_opencode(target: Path, config_dir: Path = CONFIG_DIR) -> None:
    """Write the generated OpenCode compatibility targets."""
    target.mkdir(parents=True, exist_ok=True)
    for role in OPENCODE_ROLES:
        (target / f"{role}.md").write_bytes(render_opencode(role, config_dir))


def check_opencode(target: Path, config_dir: Path = CONFIG_DIR) -> list[str]:
    """Return generated target names that are absent or have drifted."""
    drifted = []
    for role in OPENCODE_ROLES:
        path = target / f"{role}.md"
        if not path.is_file() or path.read_bytes() != render_opencode(role, config_dir):
            drifted.append(path.name)
    return drifted


def _text(value: Mapping[str, Any], key: str, path: Path) -> str:
    item = value.get(key)
    if not isinstance(item, str) or not item:
        raise ValueError(f"{key} must be a non-empty string in {path}")
    return item


def _strings(value: Mapping[str, Any], key: str, path: Path) -> tuple[str, ...]:
    items = value.get(key)
    if not isinstance(items, Sequence) or isinstance(items, (str, bytes)):
        raise ValueError(f"{key} must be a list in {path}")
    if not all(isinstance(item, str) and item for item in items):
        raise ValueError(f"{key} contains an invalid item in {path}")
    return tuple(items)


def _optional_strings(
    value: Mapping[str, Any], key: str, path: Path
) -> tuple[str, ...]:
    """Return an optional string-list field as an immutable tuple."""
    if key not in value:
        return ()
    return _strings(value, key, path)


def _positive_int(value: Mapping[str, Any], key: str, path: Path) -> int:
    item = value.get(key)
    if not isinstance(item, int) or isinstance(item, bool) or item < 1:
        raise ValueError(f"{key} must be a positive integer in {path}")
    return item


def _optional_text(value: Any, path: Path) -> str | None:
    if value is None:
        return None
    if not isinstance(value, str):
        raise ValueError(f"OpenCode compatibility field must be text in {path}")
    return value


def main(argv: Sequence[str] | None = None) -> int:
    """Write or check generated OpenCode compatibility targets."""
    parser = argparse.ArgumentParser()
    action = parser.add_mutually_exclusive_group(required=True)
    action.add_argument("--write-opencode", type=Path, metavar="DIR")
    action.add_argument("--check-opencode", type=Path, metavar="DIR")
    args = parser.parse_args(argv)
    if args.write_opencode is not None:
        write_opencode(args.write_opencode)
        return 0
    drifted = check_opencode(args.check_opencode)
    if drifted:
        print("OpenCode role drift: " + ", ".join(drifted), file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
