"""Load neutral role policies and render frozen OpenCode compatibility files."""

from __future__ import annotations

import argparse
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Mapping, Sequence

import yaml


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
    edit_roots: tuple[str, ...]
    allowed_native_capabilities: tuple[str, ...]
    mcp_tool_allowlist: tuple[str, ...]
    delegation_policy: str
    default_cap: int
    opencode_markdown: str | None = field(default=None, repr=False, compare=False)


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
        edit_roots=_strings(value, "edit_roots", path),
        allowed_native_capabilities=_strings(
            value, "allowed_native_capabilities", path
        ),
        mcp_tool_allowlist=_strings(value, "mcp_tool_allowlist", path),
        delegation_policy=_text(value, "delegation_policy", path),
        default_cap=_positive_int(value, "default_cap", path),
        opencode_markdown=_optional_text(value.get("opencode_markdown"), path),
    )
    if policy.role != role:
        raise ValueError(f"role policy name mismatch in {path}: {policy.role!r}")
    if policy.delegation_policy != "deny":
        raise ValueError(f"delegation must be denied in {path}")
    return policy


def load_policies(config_dir: Path = CONFIG_DIR) -> dict[str, RolePolicy]:
    """Return every registered execution policy keyed by role name."""
    return {role: load_role(role, config_dir) for role in ROLE_NAMES}


def render_opencode(role: str, config_dir: Path = CONFIG_DIR) -> bytes:
    """Render a frozen OpenCode campaign agent byte-for-byte."""
    if role not in OPENCODE_ROLES:
        raise ValueError(f"no OpenCode compatibility target for role: {role}")
    body = load_role(role, config_dir).opencode_markdown
    if body is None:
        raise ValueError(f"missing OpenCode compatibility body for role: {role}")
    return body.encode("utf-8")


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


def _positive_int(value: Mapping[str, Any], key: str, path: Path) -> int:
    item = value.get(key)
    if not isinstance(item, int) or isinstance(item, bool) or item < 1:
        raise ValueError(f"{key} must be a positive integer in {path}")
    return item


def _optional_text(value: Any, path: Path) -> str | None:
    if value is None:
        return None
    if not isinstance(value, str):
        raise ValueError(f"opencode_markdown must be text in {path}")
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
