"""Tests for the shared project-guard helper."""

from pathlib import Path

import pytest

from compchem_memory.project_guard import check_project, GuardResult


def test_match_when_same_path(tmp_path):
    pinned = str(tmp_path / "proj_a")
    Path(pinned).mkdir()
    res = check_project(pinned, pinned_dir=pinned, is_write=False)
    assert isinstance(res, GuardResult)
    assert res.kind == "match"
    assert res.is_cross_project is False


def test_match_when_requested_is_none(tmp_path):
    pinned = str(tmp_path / "proj_a")
    Path(pinned).mkdir()
    res = check_project(None, pinned_dir=pinned, is_write=False)
    assert res.kind == "match"


def test_cross_read(tmp_path):
    pinned = str(tmp_path / "proj_a")
    other = str(tmp_path / "proj_b")
    Path(pinned).mkdir()
    Path(other).mkdir()
    res = check_project(other, pinned_dir=pinned, is_write=False)
    assert res.kind == "cross_read"
    assert res.is_cross_project is True
    assert res.requested == str(Path(other).resolve())
    assert res.pinned == str(Path(pinned).resolve())


def test_cross_write(tmp_path):
    pinned = str(tmp_path / "proj_a")
    other = str(tmp_path / "proj_b")
    Path(pinned).mkdir()
    Path(other).mkdir()
    res = check_project(other, pinned_dir=pinned, is_write=True)
    assert res.kind == "cross_write"
    assert res.is_cross_project is True


def test_resolved_path_comparison(tmp_path):
    """A non-normalized path to the pinned dir still counts as a match."""
    pinned = str(tmp_path / "proj_a")
    Path(pinned).mkdir()
    same_messy = str(tmp_path / "proj_a" / ".")  # resolves to proj_a
    res = check_project(same_messy, pinned_dir=pinned, is_write=False)
    assert res.kind == "match"
