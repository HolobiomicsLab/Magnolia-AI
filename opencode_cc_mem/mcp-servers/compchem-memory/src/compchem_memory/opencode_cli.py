"""How the memory server reaches OpenCode: which binary, which flags, or none.

The server runs as an MCP child of an OpenCode session. Under OpenCode v2
(``MAGNOLIA_OPENCODE_V2`` set, the launcher's default) two things differ from v1:

- A bare ``opencode`` from PATH can be another version (v1 on a machine that
  has both), so the v2 binary is named explicitly: ``MAGNOLIA_OPENCODE_V2_BIN``,
  else the launcher's default path.
- Observed on OpenCode 2.3.0: a CLI call with neither ``--standalone`` nor
  ``--server`` starts (or reuses) one shared background service per user
  account. That service keeps the first caller's ``OPENCODE_DB`` and
  environment, and on start it tried to resume the session still running in
  that database and wrote a failed turn into it. So from inside a session the
  v2 database is read directly where Magnolia can, and any v2 CLI call passes
  ``--standalone``. ``opencode export <id>`` does not exist on 2.3.0 (only
  ``opencode session export <id>``).

v1 behaviour is unchanged: bare ``opencode`` from PATH.
"""

import os
import shutil
from pathlib import Path
from typing import Optional

DEFAULT_V2_BIN = "~/.local/share/magnolia-v2/bin/opencode"
_TRUE = ("1", "true", "yes", "on")


def is_v2() -> bool:
    """True when this process belongs to an OpenCode v2 session."""
    return os.environ.get("MAGNOLIA_OPENCODE_V2", "").strip().lower() in _TRUE


def v2_binary() -> str:
    """The v2 binary, resolved as the launcher resolves it."""
    return os.environ.get("MAGNOLIA_OPENCODE_V2_BIN") or os.path.expanduser(DEFAULT_V2_BIN)


def command(*args: str) -> list[str]:
    """Argument vector for one OpenCode CLI call.

    v2: the v2 binary with ``--standalone`` (a private server that exits with
    the call). v1: bare ``opencode`` from PATH, as before."""
    if is_v2():
        return [v2_binary(), "--standalone", *args]
    return ["opencode", *args]


def v2_db_path() -> Optional[str]:
    """The v2 session database (``OPENCODE_DB``) when it exists, else None."""
    db = os.environ.get("OPENCODE_DB")
    if db and Path(db).is_file():
        return db
    return None


def available() -> bool:
    """True when session transcripts can be read.

    v2: the session database exists or the v2 binary is executable.
    v1: ``opencode`` is on PATH."""
    if is_v2():
        if v2_db_path():
            return True
        b = v2_binary()
        return os.path.isfile(b) and os.access(b, os.X_OK)
    return shutil.which("opencode") is not None
