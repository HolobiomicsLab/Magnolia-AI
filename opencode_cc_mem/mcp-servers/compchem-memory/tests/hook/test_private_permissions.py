"""Private modes for the hook's small standalone persistence paths."""

import os

from compchem_memory.hook.errors import log_error


def test_hook_error_log_is_private_regardless_of_umask(tmp_path):
    store = tmp_path / "nested" / "store"
    previous_umask = os.umask(0o022)
    try:
        log_error(RuntimeError("private"), "claude", "post-tool", store, {})
    finally:
        os.umask(previous_umask)

    assert store.stat().st_mode & 0o777 == 0o700
    assert (store / "hook-errors.jsonl").stat().st_mode & 0o777 == 0o600
