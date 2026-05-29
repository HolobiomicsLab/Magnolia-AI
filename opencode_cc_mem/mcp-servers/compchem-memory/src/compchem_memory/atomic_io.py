"""Atomic file writes via same-directory temp + os.replace.

os.replace is atomic on POSIX and Windows *within one filesystem*. Keeping
the temp file in the same directory as the target guarantees this.
"""
from __future__ import annotations
import os
import tempfile
from pathlib import Path


def atomic_write_text(path: Path, text: str) -> None:
    """Write `text` to `path` atomically.

    A concurrent reader sees either the previous file contents or the new
    contents — never a partial/torn file. A crash mid-write leaves either
    the previous file intact or (worst case) a leftover temp file that is
    not the target path; the target is never corrupted.
    """
    path = Path(path)
    parent = path.parent
    parent.mkdir(parents=True, exist_ok=True)
    fd_tmp = tempfile.NamedTemporaryFile(
        mode="w",
        encoding="utf-8",
        dir=parent,
        prefix=f".{path.name}.",
        suffix=".tmp",
        delete=False,
    )
    tmp_path = Path(fd_tmp.name)
    try:
        fd_tmp.write(text)
        fd_tmp.flush()
        os.fsync(fd_tmp.fileno())
        fd_tmp.close()
        os.replace(tmp_path, path)
    except Exception:
        try:
            fd_tmp.close()
        except Exception:
            pass
        if tmp_path.exists():
            try:
                tmp_path.unlink()
            except Exception:
                pass
        raise
