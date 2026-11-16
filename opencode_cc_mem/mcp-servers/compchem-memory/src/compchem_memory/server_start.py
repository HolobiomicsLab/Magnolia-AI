"""Process guards required before a stdio memory server imports its workload."""

from __future__ import annotations

import faulthandler
import sys
import threading
import traceback
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


_crash_file = None


class _StdoutToStderr:
    """Preserve the binary MCP stream while redirecting text diagnostics."""

    def __init__(self, real: Any):
        self._real = real
        self.buffer = getattr(real, "buffer", None)

    def write(self, data: str) -> int:
        return sys.stderr.write(data)

    def flush(self) -> None:
        sys.stderr.flush()

    def __getattr__(self, name: str) -> Any:
        return getattr(self._real, name)


def write_crash_log(
    project_dir: str, where: str, exc: BaseException | None = None
) -> None:
    """Append one best-effort diagnostic to the project's crash log."""
    try:
        path = Path(project_dir) / ".magnolia" / "crash.log"
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("a", encoding="utf-8") as stream:
            stream.write(
                f"--- {datetime.now(timezone.utc).isoformat()} {where} ---\n"
            )
            if exc is not None:
                traceback.print_exception(
                    type(exc), exc, exc.__traceback__, file=stream
                )
            stream.flush()
    except Exception:
        pass


def install_stdio_guards(project_dir: str) -> None:
    """Protect one stdio MCP launch from diagnostics and unlogged crashes."""
    global _crash_file
    try:
        crash_path = Path(project_dir) / ".magnolia" / "crash.log"
        crash_path.parent.mkdir(parents=True, exist_ok=True)
        _crash_file = crash_path.open("a", encoding="utf-8")
        faulthandler.enable(file=_crash_file, all_threads=True)
    except Exception:
        pass

    def thread_excepthook(args) -> None:
        if args.exc_type is SystemExit:
            return
        write_crash_log(
            project_dir,
            f"thread {args.thread.name if args.thread else '?'}",
            args.exc_value,
        )
        threading.__excepthook__(args)

    threading.excepthook = thread_excepthook
    if (
        getattr(sys.stdout, "buffer", None) is not None
        and not isinstance(sys.stdout, _StdoutToStderr)
    ):
        sys.stdout = _StdoutToStderr(sys.stdout)
