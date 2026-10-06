"""Tests for the distill log and notices queue."""

from pathlib import Path

import pytest

from compchem_memory import distill_log


@pytest.fixture
def project_dir(tmp_path):
    pd = tmp_path / "proj"
    (pd / ".magnolia").mkdir(parents=True)
    return pd


def test_append_distill_log_creates_and_appends(project_dir):
    distill_log.append_distill_log(str(project_dir), "QUOTE-A", "2 learnings from session X")
    log = project_dir / ".magnolia" / "distill.log"
    assert log.exists()
    text = log.read_text()
    assert "QUOTE-A" in text
    assert "2 learnings from session X" in text

    distill_log.append_distill_log(str(project_dir), "QUOTE-B", "1 learning from session Y")
    text = log.read_text()
    assert "QUOTE-A" in text and "QUOTE-B" in text
    assert text.count("\n") >= 2


def test_push_and_drain_notices_roundtrip(project_dir):
    distill_log.push_distill_notice(str(project_dir), "QUOTE-1", "summary 1")
    distill_log.push_distill_notice(str(project_dir), "QUOTE-2", "summary 2")

    notices = distill_log.drain_distill_notices(str(project_dir))
    assert len(notices) == 2
    assert any("QUOTE-1" in n and "summary 1" in n for n in notices)
    assert any("QUOTE-2" in n and "summary 2" in n for n in notices)


def test_drain_empties_the_queue(project_dir):
    distill_log.push_distill_notice(str(project_dir), "Q", "s")
    first = distill_log.drain_distill_notices(str(project_dir))
    assert len(first) == 1
    second = distill_log.drain_distill_notices(str(project_dir))
    assert second == []


def test_drain_on_missing_queue_returns_empty(project_dir):
    assert distill_log.drain_distill_notices(str(project_dir)) == []


def test_identical_pending_notice_is_latched(project_dir):
    # Repeated identical pushes (e.g. idle sweeps) collapse to one pending
    # copy, so a single drain surfaces the notice once (2026-10-05: 6
    # identical "cleanup ready" notices arrived in one tool result).
    for _ in range(3):
        distill_log.push_distill_notice(str(project_dir), "QUOTE-1", "summary 1")
    distill_log.push_distill_notice(str(project_dir), "QUOTE-1", "summary DIFFERENT")
    distill_log.push_distill_notice(str(project_dir), "QUOTE-2", "summary 1")

    notices = distill_log.drain_distill_notices(str(project_dir))
    assert len(notices) == 3
    assert sum("summary 1 — QUOTE-1" in n for n in notices) == 1


def test_latch_releases_after_drain(project_dir):
    distill_log.push_distill_notice(str(project_dir), "Q", "s")
    distill_log.push_distill_notice(str(project_dir), "Q", "s")
    assert len(distill_log.drain_distill_notices(str(project_dir))) == 1
    # After the queue is cleared the same notice may surface again.
    distill_log.push_distill_notice(str(project_dir), "Q", "s")
    assert len(distill_log.drain_distill_notices(str(project_dir))) == 1
