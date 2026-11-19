"""Merge-preserving JSON and TOML writers for project-owned files."""

from __future__ import annotations

import json
import os
import tomllib
from collections.abc import Callable, Iterable
from datetime import date, datetime, time
from pathlib import Path

Owned = Callable[[str, str | None, dict], bool]
_SERVER_COLLECTIONS = {"mcp", "mcp_servers", "mcpServers"}


def merge_json(path: Path, owned: Owned, magnolia: dict) -> list[str]:
    """Merge entries selected by owned(kind, name, entry).

    ``kind`` is ``"server"`` for MCP server dictionaries and ``"hook"`` for
    hook dictionaries. Owned entries absent from ``magnolia`` are removed.
    """
    content, conflicts = merged_json(path, owned, magnolia)
    atomic_write(path, content)
    return conflicts


def merged_json(
    path: Path,
    owned: Owned,
    magnolia: dict,
    prepare: Callable[[dict], dict] | None = None,
) -> tuple[bytes, list[str]]:
    """Return the bytes :func:`merge_json` would write, and its conflicts; write nothing.

    ``prepare`` may rewrite the parsed current file before merging.
    """
    current = _read_json(path)
    if prepare is not None:
        current = prepare(current)
    conflicts: list[str] = []
    merged = _merge_dict(current, owned, magnolia, conflicts=conflicts)
    return _json_bytes(merged), conflicts


def merge_toml(path: Path, owned: Owned, magnolia: dict) -> list[str]:
    """Merge TOML with the same ownership contract as :func:`merge_json`.

    The first byte-changing rewrite of a file with a real comment saves the original
    bytes to ``<name>.magnolia-backup`` and never replaces that backup.
    """
    content, conflicts, original = merged_toml(path, owned, magnolia)
    if original is not None:
        backup = path.with_name(path.name + ".magnolia-backup")
        if not backup.exists():
            backup.write_bytes(original)
    atomic_write(path, content)
    return conflicts


def merged_toml(
    path: Path,
    owned: Owned,
    magnolia: dict,
    prepare: Callable[[dict], dict] | None = None,
) -> tuple[bytes, list[str], bytes | None]:
    """Return the bytes :func:`merge_toml` would write, its conflicts, and the original
    bytes to back up (or None when no backup is due); write nothing."""
    original = path.read_bytes() if path.exists() else b""
    current = tomllib.loads(original.decode("utf-8")) if original else {}
    if prepare is not None:
        current = prepare(current)
    conflicts: list[str] = []
    merged = _merge_dict(current, owned, magnolia, conflicts=conflicts)
    content = _toml_bytes(merged)
    due = content != original and original and _has_comment(original.decode("utf-8"))
    return content, conflicts, (original if due else None)


def validate_project_path(path: Path, project_dir: Path) -> None:
    """Reject a file whose resolved path or parent leaves its project."""
    boundary = project_dir.resolve()
    if (
        not path.resolve().is_relative_to(boundary)
        or not path.parent.resolve().is_relative_to(boundary)
    ):
        raise ValueError(f"configuration path leaves the project: {path}")


def validate_project_paths(paths: Iterable[Path], project_dir: Path) -> None:
    """Validate every path in an iterable before any one of them is written."""
    for path in paths:
        validate_project_path(Path(path), project_dir)


def atomic_write(path: Path, content: bytes) -> None:
    """Replace a file atomically, preserving its mode and any symlink.

    When ``path`` is a symlink, its target is replaced instead of the link.
    """
    destination = path.resolve() if path.is_symlink() else path
    mode = destination.stat().st_mode if destination.exists() else None
    destination.parent.mkdir(parents=True, exist_ok=True)
    temporary = destination.with_name(f".{destination.name}.{os.getpid()}.tmp")
    try:
        temporary.write_bytes(content)
        if mode is not None:
            os.chmod(temporary, mode)
        os.replace(temporary, destination)
    finally:
        temporary.unlink(missing_ok=True)


def _read_json(path: Path) -> dict:
    if not path.exists():
        return {}
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise ValueError(f"expected a JSON object in {path}")
    return value


def _json_bytes(value: dict) -> bytes:
    return (json.dumps(value, indent=2, ensure_ascii=False) + "\n").encode("utf-8")


def _merge_dict(
    current: dict,
    owned: Owned,
    magnolia: dict,
    path: tuple = (),
    *,
    all_owned: bool = False,
    conflicts: list[str],
) -> dict:
    merged = {}
    for key, value in current.items():
        item_path = (*path, key)
        kind = _entry_kind(path, current)
        if kind and not all_owned and owned(kind, key, value) and key not in magnolia:
            continue
        merged[key] = value
        if key in magnolia:
            if kind and owned(kind, key, value) and isinstance(value, dict):
                merged[key] = _merge_dict(
                    value, owned, magnolia[key], item_path,
                    all_owned=True,
                    conflicts=conflicts,
                )
            elif kind and isinstance(value, dict):
                conflicts.append(_conflict(path, key))
            elif kind:
                continue
            elif all_owned:
                merged[key] = magnolia[key]
            else:
                merged[key] = _merge_value(
                    value, magnolia[key], owned, item_path, conflicts
                )
    for key, value in magnolia.items():
        if key not in merged:
            merged[key] = value
    return merged


def _merge_value(current, replacement, owned: Owned, path: tuple, conflicts: list[str]):
    if isinstance(current, dict) and isinstance(replacement, dict):
        return _merge_dict(current, owned, replacement, path, conflicts=conflicts)
    if isinstance(current, list) and isinstance(replacement, list):
        return _merge_list(current, replacement, owned, path, conflicts)
    return replacement


def _merge_list(
    current: list, replacement: list, owned: Owned, path: tuple, conflicts: list[str]
) -> list:
    merged = []
    remaining = list(replacement)
    kind = _entry_kind(path, current)
    if kind is None and len(path) >= 2 and path[-2] in {"hooks", *_SERVER_COLLECTIONS}:
        kind = "hook" if path[-2] == "hooks" else "server"
    for value in current:
        if not isinstance(value, dict) or not kind:
            merged.append(value)
            continue
        name = value.get("name")
        # Nameless items (Claude/Codex hook matcher groups) never match by name:
        # a member group must not consume or conflict with Magnolia's group.
        matching = None if name is None else next(
            (
                (index, candidate)
                for index, candidate in enumerate(remaining)
                if isinstance(candidate, dict) and candidate.get("name") == name
            ),
            None,
        )
        if owned(kind, name, value):
            if matching is not None:
                merged.append(matching[1])
                remaining.pop(matching[0])
            elif remaining:
                merged.extend(remaining)
                remaining.clear()
            continue
        merged.append(value)
        if matching is not None:
            conflicts.append(_conflict(path, name))
            remaining.pop(matching[0])
    merged.extend(remaining)
    return merged


def _conflict(path: tuple, name: str | None) -> str:
    display_name = repr(name) if path and path[-1] in _SERVER_COLLECTIONS | {"hooks"} else name
    location = ".".join(str(part) for part in (*path, display_name or "<unnamed>"))
    return f"configuration conflict: {location} (member entry preserved)"


def _entry_kind(path: tuple, collection: dict) -> str | None:
    if path and path[-1] in _SERVER_COLLECTIONS and isinstance(collection, list):
        return "server"
    if path and path[-1] == "hooks" and isinstance(collection, list):
        return "hook"
    if path and path[-1] in _SERVER_COLLECTIONS and any(
        isinstance(value, list) for value in collection.values()
    ):
        return None
    if path and path[-1] == "hooks" and all(
        isinstance(value, list) for value in collection.values()
    ):
        return None
    if path and path[-1] in _SERVER_COLLECTIONS:
        return "server"
    if path and path[-1] == "hooks":
        return "hook"
    return None


def _has_comment(text: str) -> bool:
    quote = None
    triple = False
    escaped = False
    index = 0
    while index < len(text):
        if quote:
            if triple and text.startswith(quote * 3, index):
                quote = None
                triple = False
                escaped = False
                index += 3
                continue
            character = text[index]
            if quote == '"' and escaped:
                escaped = False
            elif quote == '"' and character == "\\":
                escaped = True
            elif not triple and character == quote:
                quote = None
        elif text.startswith(('"""', "'''"), index):
            quote = text[index]
            triple = True
            index += 2
        elif text[index] in {'"', "'"}:
            quote = text[index]
        elif text[index] == "#":
            return True
        index += 1
    return False


def _toml_bytes(value: dict) -> bytes:
    lines: list[str] = []
    _emit_table(value, (), lines)
    return ("\n".join(lines) + "\n").encode("utf-8")


def toml_bytes(value: dict) -> bytes:
    """Serialize a native TOML artifact with the harness's stable writer."""
    return _toml_bytes(value)


def _emit_table(table: dict, path: tuple[str, ...], lines: list[str]) -> None:
    scalars = {key: value for key, value in table.items() if not _is_container(value)}
    children = {key: value for key, value in table.items() if _is_container(value)}
    if path and (scalars or not children):
        _blank_line(lines)
        lines.append(f"[{_table_key(path)}]")
    for key, value in scalars.items():
        lines.append(f"{_key(key)} = {_value(value)}")
    for key, value in children.items():
        _emit_child(value, (*path, key), lines)


def _emit_child(value, path: tuple[str, ...], lines: list[str]) -> None:
    if isinstance(value, dict):
        _emit_table(value, path, lines)
        return
    for item in value:
        _blank_line(lines)
        lines.append(f"[[{_table_key(path)}]]")
        _emit_array_item(item, path, lines)


def _emit_array_item(item: dict, path: tuple[str, ...], lines: list[str]) -> None:
    scalars = {key: value for key, value in item.items() if not _is_container(value)}
    children = {key: value for key, value in item.items() if _is_container(value)}
    for key, value in scalars.items():
        lines.append(f"{_key(key)} = {_value(value)}")
    for key, value in children.items():
        _emit_child(value, (*path, key), lines)


def _is_container(value) -> bool:
    return isinstance(value, dict) or (
        isinstance(value, list) and bool(value) and all(isinstance(item, dict) for item in value)
    )


def _table_key(path: tuple[str, ...]) -> str:
    return ".".join(_key(part) for part in path)


def _key(value: str) -> str:
    if value and all(
        character.isascii() and (character.isalnum() or character in "-_")
        for character in value
    ):
        return value
    return _string(value)


def _string(value: str) -> str:
    escaped = json.dumps(value, ensure_ascii=False)
    for code in (*range(0x20), 0x7F):
        character = chr(code)
        if character not in "\b\t\n\f\r":
            escaped = escaped.replace(character, f"\\u{code:04X}")
    return escaped


def _value(value) -> str:
    if isinstance(value, str):
        return _string(value)
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, int):
        return str(value)
    if isinstance(value, float):
        return repr(value)
    if isinstance(value, (datetime, date, time)):
        return value.isoformat()
    if isinstance(value, list):
        return "[" + ", ".join(_value(item) for item in value) + "]"
    if isinstance(value, dict):
        fields = (f"{_key(key)} = {_value(item)}" for key, item in value.items())
        return "{ " + ", ".join(fields) + " }"
    raise TypeError(f"unsupported TOML value: {value!r}")


def _blank_line(lines: list[str]) -> None:
    if lines and lines[-1] != "":
        lines.append("")
