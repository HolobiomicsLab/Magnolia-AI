"""Regression tests for parameter-value attribution in contradictions.py.

The fixtures are the real spans that produced 58 false conflict pairs on
2026-11-24, kept as text so the mechanism is pinned, not just today's files.
"""

from compchem_memory.contradictions import (
    _bound_values,
    _clause_values,
    _is_filename_only,
    _param_conflicts,
)

TABLE_NEIGHBOUR = (
    "run | score | irmsd | bsa\n"
    "repl2 | -125.61 | 1.83 | 1693 |\n\n"
    "AIR-free = score - 0.1 * air, with w_air read from output/12_caprieval/weights_params.json. "
    "The score decomposition vdw + 0.2*elec + desolv reproduces it."
)
AIR_RULE = (
    "AIR / CAPRI scores in this project's HADDOCK3 runs: score - w_air*AIR is the "
    "AIR-free score to compare (w_air from the config; 0.1 here), not raw score."
)


def test_table_row_does_not_leak_into_parameter():
    vals = _clause_values(TABLE_NEIGHBOUR, "w_air")
    assert vals == {"0.1"}
    assert "1.83" not in vals and "125.61" not in vals


def test_table_neighbour_pair_is_agreement_not_conflict():
    assert _param_conflicts(TABLE_NEIGHBOUR, AIR_RULE, {"w_air"}) == []


def test_three_weights_in_one_clause_bind_to_their_own_names():
    text = "standard caprieval config with w_elec=0.2, w_air=0.1, w_desolv=1.0"
    assert _bound_values(text, "w_elec") == {"0.2"}
    assert _bound_values(text, "w_air") == {"0.1"}
    assert _bound_values(text, "w_desolv") == {"1.0"}


def test_shared_weight_vector_is_not_a_conflict():
    a = "standard caprieval config with w_elec=0.2, w_air=0.1, w_desolv=1.0"
    b = "All other weights remain w_vdw=1.0, w_elec=0.2, w_desolv=1.0, w_bsa=0.0 across all stages."
    assert _param_conflicts(a, b, {"w_elec", "w_desolv"}) == []


def test_per_stage_vs_blanket_stays_a_candidate():
    a = "AIR-free = score - 0.1 * air, with w_air read from weights_params.json."
    b = "Only w_air differs: rigidbody (it0) = 0.01, flexref/emref/mdref (it1/it2) = 0.1."
    conflicts = _param_conflicts(a, b, {"w_air"})
    assert len(conflicts) == 1
    assert conflicts[0]["a_values"] == ["0.1"]
    assert conflicts[0]["b_values"] == ["0.01", "0.1"]
    # No syntax binding on at least one side, so this is adjudicable, not hard.
    assert conflicts[0]["binding"] == "proximity"


def test_disjoint_bound_values_are_explicit():
    conflicts = _param_conflicts("w_air = 0.1", "w_air = 0.01", {"w_air"})
    assert len(conflicts) == 1
    assert conflicts[0]["binding"] == "explicit"


def test_shared_bound_value_is_not_explicit():
    conflicts = _param_conflicts("w_air = 0.1", "w_air = 0.01 and later w_air = 0.1", {"w_air"})
    assert conflicts[0]["binding"] == "proximity"


def test_filename_is_not_a_parameter():
    text = "Read the weights from output/12_caprieval/weights_params.json (0.1) or weights_params.json (0.01)."
    assert _is_filename_only(text, "weights_params") is True
    other = "The weights are in weights_params.json of the same stage (0.1)."
    assert _param_conflicts(text, other, {"weights_params"}) == []


def test_prose_parameter_without_equals_still_found():
    a = "The contact_threshold for a classified contact is 4.0 A."
    b = "the applied contact_threshold window was 3.6 to 4.5 A."
    conflicts = _param_conflicts(a, b, {"contact_threshold"})
    assert len(conflicts) == 1
    assert conflicts[0]["a_values"] == ["4.0"]
    assert conflicts[0]["b_values"] == ["3.6", "4.5"]


def test_decimal_point_is_not_a_clause_boundary():
    # "0.1." ends a sentence; the clause must not swallow the next one.
    text = "w_air was 0.1. The cluster score was -125.61."
    assert _clause_values(text, "w_air") == {"0.1"}


def test_trailing_zero_is_not_a_conflict():
    # 0.10 and 0.1 are the same number; a string comparison would invent an
    # explicit conflict that no adjudicator can clear.
    assert _param_conflicts("w_air = 0.10", "w_air = 0.1", {"w_air"}) == []


def test_one_vs_one_oh_is_not_a_conflict():
    assert _param_conflicts("w_air = 1.0", "w_air = 1.00", {"w_air"}) == []


def test_normalized_shared_value_is_not_explicit():
    conflicts = _param_conflicts(
        "w_air = 0.10", "w_air = 0.1 and w_air = 0.2", {"w_air"})
    assert len(conflicts) == 1
    assert conflicts[0]["binding"] == "proximity"


def test_longer_identifier_does_not_bind():
    # my_w_air is a different parameter; its value must not bind to w_air.
    assert _bound_values("my_w_air = 0.1", "w_air") == set()
    assert _clause_values("my_w_air = 0.1", "w_air") == set()
    assert _param_conflicts("my_w_air = 0.1", "w_air = 0.2", {"w_air"}) == []
