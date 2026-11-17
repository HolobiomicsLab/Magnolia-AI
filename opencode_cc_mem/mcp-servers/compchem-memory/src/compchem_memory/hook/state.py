"""Per-session state persisted under an exclusive file lock.

Callers hold the context only long enough to read what they need or to commit
a result. The lock is released before any slow work (search, store scan, LLM
call) and reacquired by a later ``open`` to commit, so a slow worker never
blocks another session's read.

The lock file is separate from the data file: the data file is replaced
atomically, which would otherwise discard a lock held on the old inode.
"""

from __future__ import annotations

import fcntl
import hashlib
import json
import re
from contextlib import contextmanager
from pathlib import Path
from typing import Iterator

from compchem_memory.atomic_io import atomic_write_text


_UNSAFE = re.compile(r"[^A-Za-z0-9_.-]")
_SAFE = re.compile(r"[A-Za-z0-9_-][A-Za-z0-9_.-]{0,99}")


def safe_component(value: str) -> str:
    """Map a harness or session id onto one safe, distinct file-name component.

    Ids already made of ``[A-Za-z0-9_.-]`` (not starting with a dot, at most
    100 characters) are kept as they are. Any other id becomes a cleaned,
    shortened prefix followed by ``=`` and a SHA-256 digest of the original;
    ``=`` never occurs in a kept id, so two ids never share a name, and no
    result is empty, ``.``, ``..`` or contains a path separator.
    """
    if _SAFE.fullmatch(value):
        return value
    digest = hashlib.sha256(value.encode("utf-8", "surrogatepass")).hexdigest()[:24]
    prefix = _UNSAFE.sub("_", value)[:40].lstrip(".")
    return f"{prefix}={digest}"


_sanitise = safe_component


def _read_data(path: Path) -> dict:
    """Return the stored object, or ``{}`` for a missing or corrupt file."""
    try:
        raw = path.read_text(encoding="utf-8")
    except OSError:
        return {}
    try:
        data = json.loads(raw)
    except (ValueError, TypeError):
        return {}
    return data if isinstance(data, dict) else {}


class SessionState:
    """Context manager exposing ``data`` and saving it on clean exit."""

    def __init__(self, path: Path, lock_path: Path, data: dict) -> None:
        self.path = path
        self.lock_path = lock_path
        self.data = data

    @classmethod
    @contextmanager
    def open(cls, store: Path, harness: str, session_id: str) -> Iterator["SessionState"]:
        """Lock, load, yield, then save atomically; save nothing on error."""
        directory = Path(store) / "hook-state"
        directory.mkdir(parents=True, exist_ok=True)
        name = f"{_sanitise(harness)}-{_sanitise(session_id)}"
        path = directory / f"{name}.json"
        lock_path = directory / f"{name}.json.lock"

        with open(lock_path, "a+", encoding="utf-8") as lock_file:
            fcntl.flock(lock_file.fileno(), fcntl.LOCK_EX)
            try:
                state = cls(path, lock_path, _read_data(path))
                yield state
                atomic_write_text(path, json.dumps(state.data, ensure_ascii=False))
            finally:
                fcntl.flock(lock_file.fileno(), fcntl.LOCK_UN)
