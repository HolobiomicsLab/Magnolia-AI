"""Tests for claim-conflict detection and the staging-to-project promotion gate.

Motivating case (a production memory store, found 2026-09): two project
entries disagree about w_air — one correct (0.01 for rigidbody/it0, 0.1 for
flexref/emref/mdref), one overgeneralized ("0.1 across all runs"). Nothing
detected the disagreement; a replay test was misled by the wrong entry.

The pair shares ZERO significant title words, so the finder must match on
title + tags + body tokens and ignore corpus-common domain words. All
fixtures below are synthetic: no sequences, receptors, scores or identifiers
from any research project.
"""

import json
from pathlib import Path

import pytest
import yaml

from compchem_memory.contradictions import (
    ConflictCandidatesError,
    candidates_against_entry,
    find_conflict_candidates,
    load_entries,
)
from compchem_memory.health import run_health_check
from compchem_memory.storage import ensure_project_store
from compchem_memory.tiers.project import ProjectManager

# Synthetic abridgement of the real conflicting pair (topic tokens and the
# public HADDOCK3 default values preserved; no project-identifying content).
W_AIR_CORRECT = {
    "title": "HADDOCK3 scoring weights: read from weights_params.json per caprieval stage",
    "tags": ["HADDOCK3", "scoring_weights", "weight_params.json", "caprieval", "AIR-free"],
    "body": (
        "The scoring weights (w_vdw, w_elec, w_desolv, w_air) are NOT constant "
        "across a HADDOCK3 run. Only w_air differs: rigidbody (it0) = 0.01, "
        "flexref/emref/mdref (it1/it2) = 0.1. All other weights remain "
        "w_vdw=1.0, w_elec=0.2, w_desolv=1.0, w_bsa=0.0 across all stages. "
        "The caprieval module inherits these from the preceding CNS module."
    ),
}

W_AIR_OVERGENERALIZED = {
    "title": "AIR-free score computation with w_air=0.1 verified for 3 runs",
    "tags": ["air-free_scoring", "w_air_correction", "haddock3_weights", "scoring_parameters"],
    "body": (
        "Confirmed AIR-free formula: AIR_free = score - w_air x air, with "
        "w_air = 0.1 consistent across all runs (from weights_params.json). "
        "Example: testpeptide best-DockQ cluster: score -96.48, air 89.71 "
        "-> AIR-free -105.45."
    ),
}


@pytest.fixture
def project_dir(tmp_path):
    pd = tmp_path / "proj"
    ensure_project_store(str(pd))
    return pd


def _pm():
    return ProjectManager(Path.home() / ".magnolia")


def _write_entry(pd, tier, name, spec, extra_fm=None):
    d = pd / ".magnolia" / tier
    d.mkdir(parents=True, exist_ok=True)
    fm = {
        "id": name.replace(".md", ""),
        "type": "parameter_guidance",
        "title": spec["title"],
        "tags": spec["tags"],
        "confidence": 0.9,
        "observation_count": 1,
    }
    if extra_fm:
        fm.update(extra_fm)
    p = d / f"{name}.md"
    p.write_text(
        "---\n" + yaml.safe_dump(fm, default_flow_style=False) + "---\n\n"
        + spec["body"] + "\n"
    )
    return p


def _domain_fillers(pd, n=2):
    """Entries sharing only corpus-common docking vocabulary with everything."""
    for i in range(n):
        _write_entry(pd, "entries", f"filler_{i}", {
            "title": f"HADDOCK3 peptide docking run {i} scoring summary",
            "tags": ["haddock3", "docking"],
            "body": (
                "haddock3 peptide docking run scoring summary. The peptide "
                "docked against the sequence and scored. Best cluster score "
                "with restraints: -41.1 for run 1 and -43.1 for run 2 in "
                "batch 5."
            ),
        })


# --- candidate finding -------------------------------------------------------

def test_w_air_conflict_pair_is_found(project_dir):
    _write_entry(project_dir, "entries", "correct", W_AIR_CORRECT)
    _write_entry(project_dir, "entries", "overgen", W_AIR_OVERGENERALIZED)

    loaded = load_entries(project_dir / ".magnolia" / "entries")
    pairs = find_conflict_candidates(loaded)
    assert len(pairs) == 1
    names = {pairs[0]["a"]["filename"], pairs[0]["b"]["filename"]}
    assert names == {"correct.md", "overgen.md"}
    w_air = [pc for pc in pairs[0]["param_conflicts"] if pc["param"] == "w_air"]
    assert w_air, "w_air must be a conflicting parameter"
    values = set(w_air[0]["a_values"]) | set(w_air[0]["b_values"])
    assert {"0.01", "0.1"} <= values, "the it0 value 0.01 vs 0.1 must show"


def test_w_air_pair_found_through_health_check(project_dir):
    _write_entry(project_dir, "entries", "correct", W_AIR_CORRECT)
    _write_entry(project_dir, "entries", "overgen", W_AIR_OVERGENERALIZED)
    result = run_health_check(str(project_dir))
    checks = [i for i in result["details"] if i["check"] == "contradiction_candidate"]
    assert len(checks) == 1
    assert "Contradiction Candidates" in result["report_markdown"]


def test_w_air_pair_found_at_staging_project_boundary(project_dir):
    _write_entry(project_dir, "entries", "correct", W_AIR_CORRECT)
    _write_entry(project_dir, "staging", "overgen", W_AIR_OVERGENERALIZED)
    loaded = load_entries(
        project_dir / ".magnolia" / "entries",
        project_dir / ".magnolia" / "staging",
    )
    pairs = find_conflict_candidates(loaded)
    assert len(pairs) == 1
    tiers = {pairs[0]["a"]["tier"], pairs[0]["b"]["tier"]}
    assert tiers == {"project", "staging"}


def test_same_claim_pair_not_flagged(project_dir):
    same_a = {
        "title": "AIR-free score computation with w_air=0.1 for flexref runs",
        "tags": ["air-free_scoring", "w_air"],
        "body": "AIR-free = score - w_air x air with w_air = 0.1 from weights_params.json. Example: score -95.24, air 84.16, AIR-free -103.66.",
    }
    same_b = {
        "title": "w_air value for flexref caprieval scoring: read weights_params.json",
        "tags": ["caprieval", "w_air"],
        "body": "For flexref caprieval scoring, AIR-free = score - w_air x air with w_air = 0.1 from weights_params.json. Example: score -95.24, air 84.16, AIR-free -103.66.",
    }
    _write_entry(project_dir, "entries", "same_a", same_a)
    _write_entry(project_dir, "entries", "same_b", same_b)
    loaded = load_entries(project_dir / ".magnolia" / "entries")
    assert find_conflict_candidates(loaded) == []


def test_common_domain_words_only_not_flagged(project_dir):
    _domain_fillers(project_dir, n=2)
    _write_entry(project_dir, "entries", "run_a", {
        "title": "Docking of PEPTIDEONE against RECEPTA",
        "tags": ["haddock3", "docking"],
        "body": "haddock3 peptide docking run scoring summary for the PEPTIDEONE sequence. Best cluster score -41.1 with restraints on the lysine.",
    })
    _write_entry(project_dir, "entries", "run_b", {
        "title": "Docking of PEPTIDETWO against RECEPTB",
        "tags": ["haddock3", "docking"],
        "body": "haddock3 peptide docking run scoring summary for the PEPTIDETWO sequence. Best cluster score -43.1 with restraints on the threonine.",
    })
    loaded = load_entries(project_dir / ".magnolia" / "entries")
    assert find_conflict_candidates(loaded) == []


def test_prose_only_overlap_not_flagged_without_parameter_terms(project_dir):
    """The live-sweep lesson (2026-09-29): two long entries about different
    subjects can share 200+ 'rare' prose words. Without a shared parameter
    term (letter-only compound like w_air) the pair must not qualify, no
    matter how many prose tokens overlap."""
    prose_a = {
        "title": "Selecting the best score cluster without convergence analysis gives artifacts",
        "tags": ["caprieval", "analysis"],
        "body": (
            "Selecting the best score cluster without a convergence analysis "
            "produces misleading docking conclusions. The alternative analysis "
            "anchors the comparison across all models alone and adopts the "
            "alascan view against the anchor baseline."
        ),
    }
    prose_b = {
        "title": "Artifact cluster selection when the analysis alone is adopted",
        "tags": ["caprieval", "analysis"],
        "body": (
            "An artifact appears when cluster selection adopts the alternative "
            "alone; the anchor analysis across models is required against the "
            "baseline, and the alascan comparison conclusion changes with "
            "sampling effort."
        ),
    }
    _write_entry(project_dir, "entries", "prose_a", prose_a)
    _write_entry(project_dir, "entries", "prose_b", prose_b)
    loaded = load_entries(project_dir / ".magnolia" / "entries")
    assert find_conflict_candidates(loaded) == []


def test_numbers_required(project_dir):
    no_numbers = {
        "title": "Reading scoring weights from weights_params.json per caprieval stage",
        "tags": ["weights_params.json", "w_air"],
        "body": "Read the scoring weights from weights_params.json of the specific caprieval stage; w_air varies by stage. Never hardcode the AIR weight.",
    }
    _write_entry(project_dir, "entries", "with_numbers", W_AIR_CORRECT)
    _write_entry(project_dir, "entries", "no_numbers", no_numbers)
    loaded = load_entries(project_dir / ".magnolia" / "entries")
    assert find_conflict_candidates(loaded) == []


def test_same_parameter_same_value_not_flagged_despite_example_scores(project_dir):
    """Second live-sweep lesson: two entries citing the SAME weight vector are
    agreement, not contradiction, even when their example scores differ. Only
    a value difference NEAR the parameter term counts."""
    vec = (
        "Scoring weights from weights_params.json: w_air = 0.1, w_vdw = 1.0, "
        "w_elec = 0.2, w_desolv = 1.0 for the refined caprieval stage."
    )
    entry_x = {
        "title": "First working HADDOCK3 docking run scoring notes",
        "tags": ["haddock3", "w_air"],
        "body": vec + " Example run: score -95.24, air 84.16.",
    }
    entry_y = {
        "title": "First successful HADDOCK3 docking run scoring notes",
        "tags": ["haddock3", "w_air"],
        "body": vec + " Example run: score -101.6, air 88.4.",
    }
    _write_entry(project_dir, "entries", "entry_x", entry_x)
    _write_entry(project_dir, "entries", "entry_y", entry_y)
    loaded = load_entries(project_dir / ".magnolia" / "entries")
    assert find_conflict_candidates(loaded) == []


def test_max_pairs_caps_report(project_dir):
    names = ["alpha", "bravo", "charlie", "delta", "echo"]
    markers = " ".join(f"param_{s} value 0.1{i} marker m_{s}." for i, s in enumerate(names))
    base = {
        "title": "Reference parameter read from weights_params.json",
        "tags": ["weights_params.json", "w_air"],
        "body": (
            "w_air parameter baseline: 0.10 from weights_params.json of the "
            "caprieval stage. " + markers
        ),
    }
    _write_entry(project_dir, "entries", "base", base)
    for i, s in enumerate(names):
        _write_entry(project_dir, "entries", f"variant_{i}", {
            "title": f"Variant parameter w_air claim {i}",
            "tags": ["weights_params.json", "w_air"],
            "body": (
                f"w_air parameter variant {i}: 0.2{i} from weights_params.json "
                f"of the caprieval stage. param_{s} value 0.3{i} marker m_{s}."
            ),
        })
    loaded = load_entries(project_dir / ".magnolia" / "entries")
    assert len(find_conflict_candidates(loaded, max_pairs=2)) == 2


def test_candidates_against_entry_only_returns_source_pairs(project_dir):
    conflicting = {
        "title": "AIR-free score computation with w_air=0.1 verified for 3 runs",
        "tags": ["air-free_scoring", "w_air"],
        "body": "AIR-free = score - w_air x air, w_air = 0.1 across all runs (weights_params.json). Example: score -94.31, air 81.05.",
    }
    unrelated_a = {
        "title": "Restraint generation from actpass files",
        "tags": ["actpass", "restraints"],
        "body": "Generate ambig.tbl from actpass files; active residues 1.5 and passive 12.5.",
    }
    unrelated_b = {
        "title": "Restraint distance cutoffs for ambig.tbl",
        "tags": ["ambig.tbl", "restraints"],
        "body": "Distance cutoff 2.5 for active restraints, 0.5 margin for passive.",
    }
    pd = project_dir
    _write_entry(pd, "entries", "unrelated_a", unrelated_a)
    _write_entry(pd, "entries", "unrelated_b", unrelated_b)
    targets = load_entries(pd / ".magnolia" / "entries")
    src = {"filename": "incoming.md", "tier": "staging", "id": "incoming",
           "title": conflicting["title"], "tags": conflicting["tags"],
           "text": conflicting["title"] + "\n" + conflicting["body"]}
    pairs = candidates_against_entry(src, targets)
    assert len(pairs) == 0  # none of the unrelated targets conflict

    _write_entry(pd, "entries", "conflicting_project", W_AIR_CORRECT)
    targets = load_entries(pd / ".magnolia" / "entries")
    pairs = candidates_against_entry(src, targets)
    assert len(pairs) == 1
    assert {pairs[0]["a"]["filename"], pairs[0]["b"]["filename"]} == {
        "incoming.md", "conflicting_project.md"
    }


def test_file_format_threshold_pair_is_flagged(project_dir):
    """Real-world mechanism shape (synthetic content): two entries citing the
    same analysis file with different distance cutoffs — the same parameter
    with different values, one of them scope-limited."""
    thr_a = {
        "title": "Distance threshold for residue contact classification from contact_analysis.tsv",
        "tags": ["contact_analysis.tsv", "contact_threshold"],
        "body": (
            "Use the shortest-dist column of contact_analysis.tsv. The "
            "contact_threshold for a classified contact is 4.0 A."
        ),
    }
    thr_b = {
        "title": "Contact counting window from contact_analysis.tsv",
        "tags": ["contact_analysis.tsv", "contact_threshold"],
        "body": (
            "Count residue contacts from contact_analysis.tsv shortest-dist; "
            "the applied contact_threshold window was 3.6 to 4.5 A."
        ),
    }
    _write_entry(project_dir, "entries", "thr_a", thr_a)
    _write_entry(project_dir, "entries", "thr_b", thr_b)
    loaded = load_entries(project_dir / ".magnolia" / "entries")
    pairs = find_conflict_candidates(loaded)
    assert len(pairs) == 1
    params = {pc["param"] for pc in pairs[0]["param_conflicts"]}
    assert "contact_threshold" in params


def test_synthetic_corpus_scale_precision(project_dir):
    """Corpus-scale check: 10 unrelated entries sharing filler vocabulary and
    distinct values produce zero candidates; a planted conflicting pair is
    found. Guards the generality the live sweep demanded."""
    filler = "the run summary notes the model and the table for this batch."
    for i in range(10):
        _write_entry(project_dir, "entries", f"topic_{i}", {
            "title": f"Topic {i} run summary notes",
            "tags": [f"topic_label_{i}"],
            "body": filler * 3 + f" Observed value {1.0 + i * 0.1} for topic_label_{i}.",
        })
    _write_entry(project_dir, "entries", "plant_a", {
        "title": "Calibration parameter from the shared config",
        "tags": ["calibration"],
        "body": "calibration param_shared reads 0.50 from the config_shared section.",
    })
    _write_entry(project_dir, "entries", "plant_b", {
        "title": "Calibration parameter re-measured",
        "tags": ["calibration"],
        "body": "calibration param_shared reads 0.75 from the config_shared section.",
    })
    loaded = load_entries(project_dir / ".magnolia" / "entries")
    pairs = find_conflict_candidates(loaded)
    assert len(pairs) == 1
    pair_names = {pairs[0]["a"]["filename"], pairs[0]["b"]["filename"]}
    assert pair_names == {"plant_a.md", "plant_b.md"}


# --- promotion gate ----------------------------------------------------------

def _make_conflicting_staging(project_dir):
    _write_entry(project_dir, "entries", "correct", W_AIR_CORRECT)
    return _write_entry(
        project_dir, "staging", "overgen_staged", W_AIR_OVERGENERALIZED,
        extra_fm={
            "observation_count": 2,
            "confidence": 0.9,
            "observed_in_sessions": ["ses_a", "ses_b"],
        },
    )


def test_confirm_staging_holds_on_conflict(project_dir):
    staged = _make_conflicting_staging(project_dir)
    with pytest.raises(ConflictCandidatesError) as excinfo:
        _pm().confirm_staging(str(project_dir), "overgen_staged")
    assert excinfo.value.candidates
    assert staged.exists()  # held, not promoted
    assert not (project_dir / ".magnolia" / "entries" / staged.name).exists()


def test_confirm_staging_acknowledge_promotes(project_dir):
    staged = _make_conflicting_staging(project_dir)
    dest = _pm().confirm_staging(str(project_dir), "overgen_staged", acknowledge=True)
    assert Path(dest).exists()
    assert not staged.exists()


def test_confirm_staging_unrelated_entry_unblocked(project_dir):
    _write_entry(project_dir, "entries", "correct", W_AIR_CORRECT)
    _write_entry(project_dir, "staging", "actpass_note", {
        "title": "Generating restraints from actpass files",
        "tags": ["actpass", "restraints"],
        "body": "ambig.tbl comes from actpass; active residues 2.5, passive chosen around them.",
    })
    dest = _pm().confirm_staging(str(project_dir), "actpass_note")
    assert Path(dest).exists()


def test_auto_promote_holds_conflicting_entry_and_pushes_notice(project_dir):
    staged = _make_conflicting_staging(project_dir)
    # Second qualifying entry that does NOT conflict: must promote normally.
    _write_entry(project_dir, "staging", "actpass_note", {
        "title": "Generating restraints from actpass files",
        "tags": ["actpass", "restraints"],
        "body": "ambig.tbl comes from actpass; active residues 2.5, passive chosen around them.",
        }, extra_fm={
            "observation_count": 2,
            "confidence": 0.9,
            "observed_in_sessions": ["ses_c", "ses_d"],
        })

    promoted = _pm().auto_promote_staging(str(project_dir))

    assert "actpass_note.md" in promoted
    assert staged.exists()  # conflicting entry held in staging
    notices_file = project_dir / ".magnolia" / ".distill-notices"
    assert notices_file.exists()
    rows = [json.loads(l) for l in notices_file.read_text().splitlines() if l.strip()]
    assert any("overgen_staged" in r.get("quote", "") for r in rows)


# --- MCP tool surface --------------------------------------------------------

def test_memory_confirm_tool_returns_hold_payload(project_dir, monkeypatch):
    from compchem_memory import server

    _make_conflicting_staging(project_dir)
    monkeypatch.setattr(server, "PROJECT_DIR", str(project_dir))
    confirm = getattr(server.memory_confirm, "fn", server.memory_confirm)

    out = json.loads(confirm("overgen_staged", project_dir=str(project_dir)))
    assert out["status"] == "conflict_hold"
    assert out["candidates"]

    out2 = confirm("overgen_staged", project_dir=str(project_dir), acknowledge=True)
    assert (project_dir / ".magnolia" / "entries" / "overgen_staged.md").exists()
    assert "entries" in out2
