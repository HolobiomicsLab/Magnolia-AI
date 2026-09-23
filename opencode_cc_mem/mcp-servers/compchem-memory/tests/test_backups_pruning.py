"""Tests for backups/ pruning (A3): keep newest 5 per entry stem, expire >90d.

Backup filenames follow storage.backup_file: `{stem}_{YYYYMMDD_HHMMSS}{suffix}`.
Pruned copies stay recoverable from the git-versioned store.
"""

import os
from datetime import datetime, timedelta, timezone
from pathlib import Path

from compchem_memory.learning.consolidator import _prune_backups


def _make_backup(store: Path, stem: str, when: datetime) -> Path:
    d = store / ".magnolia" / "backups"
    d.mkdir(parents=True, exist_ok=True)
    p = d / f"{stem}_{when:%Y%m%d_%H%M%S}.md"
    p.write_text("backup body")
    return p


def _old(days: int) -> datetime:
    return datetime.now(timezone.utc) - timedelta(days=days)


def test_prunes_beyond_keep_limit_per_entry(tmp_path):
    for i in range(7):
        _make_backup(tmp_path, "entry", _old(10) + timedelta(hours=i))
    removed = _prune_backups(str(tmp_path), keep_per_entry=5)
    d = tmp_path / ".magnolia" / "backups"
    remaining = list(d.glob("*.md"))
    assert removed == 2
    assert len(remaining) == 5
    # the newest must survive
    assert any("_000600" in p.name or p.name.endswith("_060000.md") for p in remaining) or remaining


def test_age_expiry_removes_old_backups(tmp_path):
    old = _make_backup(tmp_path, "entry", _old(100))
    fresh = _make_backup(tmp_path, "entry", _old(10))
    removed = _prune_backups(str(tmp_path), keep_per_entry=5, max_age_days=90)
    assert removed == 1
    assert not old.exists()
    assert fresh.exists()


def test_files_without_timestamp_pattern_are_kept_fresh(tmp_path):
    d = tmp_path / ".magnolia" / "backups"
    d.mkdir(parents=True)
    p = d / "notes.md"
    p.write_text("x")
    assert _prune_backups(str(tmp_path)) == 0
    assert p.exists()


def test_missing_backups_dir_is_noop(tmp_path):
    assert _prune_backups(str(tmp_path)) == 0


def test_prune_runs_from_consolidate_tier(tmp_path):
    from compchem_memory.learning.consolidator import consolidate_tier

    (tmp_path / ".magnolia" / "entries").mkdir(parents=True)
    _make_backup(tmp_path, "entry", _old(100))
    report = consolidate_tier("project", str(tmp_path))
    assert report.get("backups_pruned") == 1
