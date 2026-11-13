"""Tests: a sweep surfaces a notice when consolidation proposals are pending,
so the review->apply loop can't silently stall (pending freezes regeneration AND
nothing else surfaces it -> deadlock). The notice rides the existing
.distill-notices queue, drained by the @captured decorator on the next tool call.
"""

import json
from pathlib import Path

from compchem_memory.storage import ensure_project_store
from compchem_memory import distill_log
from compchem_memory.startup_scan import (
    _surface_pending_consolidation,
    scan_and_distill,
)


def _write_proposals(store: Path, proposals, applied=None, rejected=None):
    staging = store / "staging"
    staging.mkdir(parents=True, exist_ok=True)
    enriched = []
    for p in proposals:
        p = dict(p)
        if "sources" not in p:
            # Live proposals need real source files on disk; otherwise the
            # stale-proposal cleanup dismisses them before surfacing.
            idx = p.get("i", len(enriched))
            names = [f"s{idx}a.md", f"s{idx}b.md"]
            for n in names:
                (staging / n).write_text("---\ntitle: placeholder\n---\n\nbody\n")
            p["sources"] = [str(staging / n) for n in names]
            p.setdefault("confidence", 0.9)
            p.setdefault("merged_preview", {"title": f"proposal {idx}", "body": "body"})
        enriched.append(p)
    reflex = store / "reflex"
    reflex.mkdir(parents=True, exist_ok=True)
    (reflex / "consolidation-proposal.json").write_text(
        json.dumps(
            {
                "proposals": enriched,
                "applied": applied or [],
                "rejected": rejected or [],
            }
        )
    )


def test_surfaces_notice_when_proposals_pending(tmp_path):
    pd = tmp_path / "proj"
    ensure_project_store(str(pd))
    store = pd / ".magnolia"
    _write_proposals(store, [{"i": 0}, {"i": 1}])  # 2 pending, 0 handled

    _surface_pending_consolidation(str(pd), store)

    notices = distill_log.drain_distill_notices(str(pd))
    assert len(notices) == 1
    assert "2" in notices[0]
    assert "consolidation" in notices[0].lower()


def test_no_notice_when_nothing_pending(tmp_path):
    pd = tmp_path / "proj"
    ensure_project_store(str(pd))
    store = pd / ".magnolia"
    _write_proposals(store, [{"i": 0}], applied=[0])  # all handled

    _surface_pending_consolidation(str(pd), store)

    assert distill_log.drain_distill_notices(str(pd)) == []


def test_no_notice_when_no_artifact(tmp_path):
    pd = tmp_path / "proj"
    ensure_project_store(str(pd))
    store = pd / ".magnolia"

    _surface_pending_consolidation(str(pd), store)

    assert distill_log.drain_distill_notices(str(pd)) == []


def test_scan_and_distill_wires_surfacing(tmp_path, monkeypatch):
    """End-to-end: a sweep with pending proposals surfaces the notice. LLM is
    off so distillation/consolidation add no competing notices."""
    pd = tmp_path / "proj"
    ensure_project_store(str(pd))
    store = pd / ".magnolia"
    _write_proposals(store, [{"i": 0}, {"i": 1}, {"i": 2}])  # 3 pending
    monkeypatch.setattr("compchem_memory.startup_scan.is_llm_available", lambda: False)

    scan_and_distill(str(pd))

    notices = distill_log.drain_distill_notices(str(pd))
    assert any("consolidation" in n.lower() and "3" in n for n in notices)
