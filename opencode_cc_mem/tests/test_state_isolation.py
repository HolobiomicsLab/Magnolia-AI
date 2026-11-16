"""Prove the campaign suite uses temporary Magnolia state roots."""

import os
from pathlib import Path

def test_magnolia_state_roots_are_test_local():
    """Keep HOME and both XDG roots inside the session test directory."""
    home = Path(os.environ["HOME"])
    assert Path(os.environ["XDG_STATE_HOME"]).parent == home.parent
    assert Path(os.environ["XDG_CONFIG_HOME"]).parent == home.parent
    assert home != Path(os.environ["MAGNOLIA_PYTEST_REAL_HOME"])


def test_owner_state_guard_detects_create_then_remove_directory(
    tmp_path, owner_state_metadata
):
    guarded = tmp_path / "magnolia"
    before = owner_state_metadata((guarded,))
    guarded.mkdir()
    guarded.rmdir()

    assert owner_state_metadata((guarded,)) != before
