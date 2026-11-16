"""Which entries in a member's client configuration belong to Magnolia."""

from __future__ import annotations

import re
import shlex
from pathlib import PurePosixPath

from compchem_memory.harness.render_common import (
    REQUIRED_HOOK_TIMEOUT_S,
    _required_pre_tool_command,
)

MAGNOLIA_SERVERS = frozenset({"compchem-tools", "compchem-memory", "perspicacite"})
_CLIENTS = ("claude", "codex", "vibe")
_PYTHON = re.compile(r"(?:python(?:\d+(?:\.\d+)*)?|py)")
# Any well-formed event name, so entries for events no longer rendered are still ours to remove.
_EVENT_TOKEN = re.compile(r"[a-z][a-z0-9-]{0,40}")
_RUNNER_PARTS = (
    "mcp-servers", "compchem-memory", "src", "compchem_memory", "hook", "runner.py"
)


def _shell_words(command: str, *, allow_newlines: bool = False) -> tuple[str, ...]:
    if (
        (not allow_newlines and ("\n" in command or "\r" in command))
        or _has_shell_expansion(command)
    ):
        return ()
    lexer = shlex.shlex(command, posix=True, punctuation_chars=True)
    lexer.whitespace_split = True
    lexer.commenters = ""
    return tuple(lexer)


def _has_shell_expansion(command: str) -> bool:
    quote = None
    escaped = False
    for character in command:
        if escaped:
            escaped = False
        elif character == "\\" and quote != "'":
            escaped = True
        elif quote:
            if character == quote:
                quote = None
            elif quote == '"' and character in "$`":
                return True
        elif character in "\"'":
            quote = character
        elif character in "$`*?[~":
            return True
    return False


def _direct_hook(
    words: tuple[str, ...], client: str | None, python_executable: str | None
) -> bool:
    if len(words) != 5 or words[-2] not in _CLIENTS or not _EVENT_TOKEN.fullmatch(words[-1]):
        return False
    if client is not None and words[-2] != client:
        return False
    if python_executable is not None:
        is_interpreter = words[0] == python_executable or bool(
            _PYTHON.fullmatch(PurePosixPath(words[0]).name)
        )
    else:
        is_interpreter = words[0] == "{python_executable}" or bool(
            _PYTHON.fullmatch(PurePosixPath(words[0]).name)
        )
    if not is_interpreter:
        return False
    if words[1:3] == ("-m", "compchem_memory.hook"):
        return True
    runner = PurePosixPath(words[2])
    return (
        words[1] == "-I"
        and (runner.is_absolute() or words[2].startswith("{installation_root}/"))
        and runner.parts[-len(_RUNNER_PARTS):] == _RUNNER_PARTS
    )


def parse_hook_command(
    command: object,
    client: str | None = None,
    python_executable: str | None = None,
) -> tuple[tuple[str, ...], bool] | None:
    """Return complete hook argv and wrapper presence, or None for member commands."""
    if not isinstance(command, str):
        return None
    try:
        words = _shell_words(command)
        if _direct_hook(words, client, python_executable):
            return words, False
        wrapper_words = _shell_words(command, allow_newlines=True)
        if len(wrapper_words) == 3 and wrapper_words[:2] == ("/bin/sh", "-c"):
            return _parse_required_hook(
                wrapper_words[2], client, python_executable
            )
    except ValueError:
        return None
    return None


def _parse_required_hook(
    script: str, client: str | None, python_executable: str | None
):
    marker = "__MAGNOLIA_HOOK_COMMAND__"
    for candidate in (client,) if client is not None else _CLIENTS:
        template = _shell_words(
            _required_pre_tool_command(marker, candidate, REQUIRED_HOOK_TIMEOUT_S),
            allow_newlines=True,
        )[2]
        before, after = template.split(marker)
        if not script.startswith(before) or not script.endswith(after):
            continue
        words = _shell_words(script[len(before):-len(after)])
        if (
            _direct_hook(words, candidate, python_executable)
            and words[-1] == "pre-tool"
        ):
            return words, True
    return None


def is_magnolia_hook_command(
    command: object,
    client: str | None = None,
    python_executable: str | None = None,
) -> bool:
    """Recognise only a complete rendered hook for the requested client."""
    return parse_hook_command(command, client, python_executable) is not None


def owned(
    kind: str,
    name: str | None,
    entry: dict,
    *,
    client: str | None = None,
    python_executable: str | None = None,
) -> bool:
    """Ownership predicate for :mod:`compchem_memory.harness.files`.

    Servers are owned by name. Only complete commands for the renderer's client
    own hooks; matcher groups must contain exclusively such hooks.
    """
    if kind == "server":
        return name in MAGNOLIA_SERVERS
    if not isinstance(entry, dict):
        return False
    if client is None:
        return False
    if _runs_hook_module(entry, client, python_executable):
        return True
    inner = entry.get("hooks")
    # A matcher group is Magnolia's only when every hook in it is; a mixed group
    # belongs to the member (see strip_owned_hooks).
    return (
        isinstance(inner, list)
        and bool(inner)
        and all(
            isinstance(hook, dict)
            and _runs_hook_module(hook, client, python_executable)
            for hook in inner
        )
    )


def strip_owned_hooks(
    current: dict,
    *,
    client: str = "codex",
    python_executable: str | None = None,
) -> dict:
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
            _strip_group(group, client, python_executable)
            for group in groups
            if not (
                isinstance(group, dict)
                and owned(
                    "hook",
                    None,
                    group,
                    client=client,
                    python_executable=python_executable,
                )
            )
        ]
        if kept:
            cleaned[event] = kept
    return {**current, "hooks": cleaned}


def _strip_group(group, client, python_executable):
    inner = group.get("hooks") if isinstance(group, dict) else None
    if not isinstance(inner, list) or owned(
        "hook",
        None,
        group,
        client=client,
        python_executable=python_executable,
    ):
        return group
    kept = [
        hook
        for hook in inner
        if not (
            isinstance(hook, dict)
            and _runs_hook_module(hook, client, python_executable)
        )
    ]
    return group if len(kept) == len(inner) else {**group, "hooks": kept}


def _runs_hook_module(
    hook: dict, client: str, python_executable: str | None
) -> bool:
    return is_magnolia_hook_command(
        hook.get("command"), client, python_executable
    )
