"""Phase 1 of the learning-recall robustness work: expose staging entries to
`memory_get_context` retrieval, safely.

Today recall reads only `entries/` (promoted), so a freshly-recorded learning in
`staging/` is invisible until promoted (obs>=2 across 2 sessions) — i.e. it can
only be recalled after the mistake it warns about has already recurred.

Phase 1 (dependency-free):
- recall also reads `staging/`, flagged `provisional`
- asymmetric gating: only warning-type staging entries (error_resolution,
  failure_pattern) are eligible; prescriptions (success_pattern,
  parameter_guidance) stay promotion-gated
- provisional entries are down-weighted (a promoted entry of equal match wins)
  and capped, so they never crowd out trusted knowledge
"""

from pathlib import Path

import yaml
import pytest

from compchem_memory import retrieval


@pytest.fixture(autouse=True)
def _no_llm(monkeypatch):
    # Keep retrieval deterministic: exercise the heuristic path, not LLM rerank.
    monkeypatch.setattr(retrieval, "is_llm_available", lambda: False)


def _write(d: Path, name: str, frontmatter: dict, body: str = "Body text."):
    d.mkdir(parents=True, exist_ok=True)
    fm = yaml.safe_dump(frontmatter, default_flow_style=False)
    (d / f"{name}.md").write_text(f"---\n{fm}---\n\n{body}\n")


def _fm(title, etype, tags=None):
    return {
        "title": title,
        "type": etype,
        "tags": tags or ["contact"],
        "tools": [],
        "confidence": 0.7,
        "observation_count": 1,
        "last_verified": "2026-06-02",
        "description": title,
    }


TASK = "interpret the contact analysis labels for this docking pose"


def test_staging_warning_surfaces_as_provisional(tmp_path):
    _write(tmp_path / "staging", "w",
           _fm("Contact analysis labels are not interaction mechanisms", "error_resolution"),
           "Measure atom-level distances; do not infer mechanism from labels.")
    results = retrieval.select_relevant_entries(TASK, str(tmp_path))
    hits = [r for r in results if "not interaction mechanisms" in r["title"]]
    assert hits, "staging warning matching the task should be retrieved"
    assert hits[0].get("provisional") is True, "staging hit must be flagged provisional"


def test_staging_prescription_is_not_surfaced(tmp_path):
    # success_pattern / parameter_guidance must stay promotion-gated
    _write(tmp_path / "staging", "p",
           _fm("Contact analysis recommended distance cutoff is 5 angstrom", "parameter_guidance"))
    results = retrieval.select_relevant_entries(TASK, str(tmp_path))
    assert not any("recommended distance cutoff" in r["title"] for r in results), \
        "prescriptive staging entries must not be exposed until promoted"


def test_promoted_outranks_equal_provisional(tmp_path):
    _write(tmp_path / "entries", "promoted",
           _fm("Contact analysis interpretation note", "error_resolution"))
    _write(tmp_path / "staging", "prov",
           _fm("Contact analysis interpretation tip", "error_resolution"))
    results = retrieval.select_relevant_entries(TASK, str(tmp_path))
    assert len(results) >= 2
    # The promoted (non-provisional) entry must rank above the provisional one.
    assert results[0].get("provisional") is False


def test_provisional_count_is_capped(tmp_path):
    for i in range(5):
        _write(tmp_path / "staging", f"w{i}",
               _fm(f"Contact analysis warning variant {i}", "error_resolution"))
    results = retrieval.select_relevant_entries(TASK, str(tmp_path), max_selections=10)
    provisional = [r for r in results if r.get("provisional")]
    assert len(provisional) <= 2, f"provisional entries must be capped, got {len(provisional)}"
