"""Campaign-suite isolation from the owner's Magnolia state."""

from __future__ import annotations

import os
import tempfile
from pathlib import Path

import pytest


_REAL_HOME_MARKER = "MAGNOLIA_PYTEST_REAL_HOME"
_REAL_HOME = Path(os.environ.get(_REAL_HOME_MARKER, Path.home())).resolve()
_OWNER_STATE_ROOTS = (
    _REAL_HOME / ".local" / "state" / "magnolia",
    _REAL_HOME / ".config" / "magnolia",
    _REAL_HOME / ".magnolia",
)
_TEST_ROOT = Path(tempfile.mkdtemp(prefix="magnolia-pytest-state-"))
_ISOLATED_ENV = {
    "HOME": str(_TEST_ROOT / "home"),
    "XDG_STATE_HOME": str(_TEST_ROOT / "state"),
    "XDG_CONFIG_HOME": str(_TEST_ROOT / "config"),
    _REAL_HOME_MARKER: str(_REAL_HOME),
}
_ORIGINAL_ENV = {name: os.environ.get(name) for name in _ISOLATED_ENV}


def _tree_metadata(root: Path) -> tuple[tuple[str, int], ...]:
    """List path names and mtimes without reading file contents."""
    if not root.exists() and not root.is_symlink():
        return ()
    rows = [(".", root.lstat().st_mtime_ns)]
    for path in sorted(root.rglob("*"), key=lambda item: str(item)):
        rows.append((str(path.relative_to(root)), path.lstat().st_mtime_ns))
    return tuple(rows)


_OWNER_STATE_BEFORE = {
    root: _tree_metadata(root) for root in _OWNER_STATE_ROOTS
}
for _name, _value in _ISOLATED_ENV.items():
    os.environ[_name] = _value
Path(_ISOLATED_ENV["HOME"]).mkdir(parents=True)


@pytest.fixture(scope="session", autouse=True)
def _isolate_magnolia_state():
    """Redirect state and fail if the owner trees change during this suite."""
    try:
        yield
        after = {root: _tree_metadata(root) for root in _OWNER_STATE_ROOTS}
        assert after == _OWNER_STATE_BEFORE, (
            "tests created or modified owner Magnolia state; "
            "compare listings and mtimes under the guarded roots"
        )
    finally:
        for name, value in _ORIGINAL_ENV.items():
            if value is None:
                os.environ.pop(name, None)
            else:
                os.environ[name] = value
