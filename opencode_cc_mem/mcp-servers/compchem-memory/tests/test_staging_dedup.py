"""The distillation save path must dedup: re-distilling the same finding across
sessions should bump an existing staging entry, not pile up near-duplicates."""

import yaml
from pathlib import Path

from compchem_memory.storage import ensure_project_store
from compchem_memory.staging_io import save_candidate


def _staging_files(store: Path):
    return list((store / "staging").glob("*.md"))


def test_save_candidate_dedups_similar(tmp_path):
    pd = tmp_path / "proj"
    ensure_project_store(str(pd))
    store = pd / ".magnolia"
    c1 = {
        "title": "TGFMALQ is the best 5/6-mer binder against 4PO2",
        "content": "AIR-free -91.00",
        "type": "scientific_finding",
        "tags": ["docking"],
    }
    save_candidate(store, c1, source="opencode_distill", opencode_session_id="s1")
    # near-identical finding distilled again in a later session
    c2 = {
        "title": "TGFMALQ is the best 5/6-mer binder against 4PO2",
        "content": "AIR-free -91.00 (re-observed)",
        "type": "scientific_finding",
        "tags": ["docking"],
    }
    save_candidate(store, c2, source="opencode_distill", opencode_session_id="s2")

    files = _staging_files(store)
    assert len(files) == 1, f"expected dedup to 1 entry, got {len(files)}"
    meta = yaml.safe_load(files[0].read_text().split("---")[1])
    assert meta["observation_count"] >= 2  # bumped, not duplicated


def test_save_candidate_distinct_titles_not_merged(tmp_path):
    """Genuinely different findings must NOT be merged."""
    pd = tmp_path / "proj"
    ensure_project_store(str(pd))
    store = pd / ".magnolia"
    save_candidate(store, {"title": "TGFMALQ best binder", "content": "x",
                           "type": "scientific_finding", "tags": []},
                   source="opencode_distill")
    save_candidate(store, {"title": "P2Rank fails on multi-model PDB", "content": "y",
                           "type": "failure_pattern", "tags": []},
                   source="opencode_distill")
    assert len(_staging_files(store)) == 2
