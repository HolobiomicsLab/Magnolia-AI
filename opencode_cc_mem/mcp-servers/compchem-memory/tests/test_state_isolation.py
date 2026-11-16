"""Prove the memory suite uses temporary Magnolia state roots."""

import os
from pathlib import Path


def test_magnolia_state_roots_are_test_local():
    """Keep HOME and both XDG roots inside the session test directory."""
    home = Path(os.environ["HOME"])
    assert Path(os.environ["XDG_STATE_HOME"]).parent == home.parent
    assert Path(os.environ["XDG_CONFIG_HOME"]).parent == home.parent
    assert home != Path(os.environ["MAGNOLIA_PYTEST_REAL_HOME"])
