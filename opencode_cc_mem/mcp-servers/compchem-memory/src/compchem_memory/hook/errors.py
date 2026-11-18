"""Payload-free error rows for the hook, with a fallback when the store is unwritable."""

from __future__ import annotations

import json
import os
from datetime import datetime, timezone
from pathlib import Path
from typing import Mapping

from compchem_memory.hook.state import private_mkdir


def log_error(
    error: BaseException,
    harness: str,
    event_name: str,
    store: Path | None,
    env: Mapping[str, str],
    behaviour: str | None = None,
) -> None:
    """Append one row to ``<store>/hook-errors.jsonl``, else to Magnolia's state home.

    Never raises: a failure to log is the last thing that may go wrong.
    """
    row = {
        "ts": datetime.now(timezone.utc).isoformat(),
        "harness": harness,
        "event": event_name,
        "error": f"{type(error).__name__}: {error}",
    }
    if behaviour is not None:
        row["behaviour"] = behaviour
    data = (json.dumps(row, ensure_ascii=False) + "\n").encode("utf-8")
    for destination in _destinations(store, env):
        try:
            private_mkdir(destination)
            descriptor = os.open(
                destination / "hook-errors.jsonl",
                os.O_WRONLY | os.O_CREAT | os.O_APPEND,
                0o600,
            )
            try:
                os.write(descriptor, data)
            finally:
                os.close(descriptor)
            return
        except Exception:  # noqa: BLE001 - try the next destination
            continue


def _destinations(store: Path | None, env: Mapping[str, str]):
    if store is not None:
        yield Path(store)
    try:
        from compchem_memory.hook.project import state_home

        yield state_home(env)
    except Exception:  # noqa: BLE001 - no state home, nowhere left to log
        return
