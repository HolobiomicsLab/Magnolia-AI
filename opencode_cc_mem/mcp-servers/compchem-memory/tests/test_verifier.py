"""Deterministic verifier: known-good passes, seeded-bad trips loudly,
missing evidence fails closed — per the P1 verification-regime DoD."""
import json

from compchem_memory.tiers.project import ProjectManager
from compchem_memory.verifier import verify_claim


EVIDENCE = {
    "metrics": {"w_air": -0.42, "best_score": -108.2345, "cluster_count": 4},
    "stages": {"flexref": {"w_air": -0.42}},
}


def test_known_good_claim_verifies():
    out = verify_claim(
        "w_air = -0.42 in the flexref stage; best_score: -108.23, "
        "cluster_count = 4", EVIDENCE,
    )
    assert out["status"] == "verified"
    assert all(c["status"] == "verified" for c in out["checks"])
    # path-suffix matching found the stage-scoped value
    w_air = next(c for c in out["checks"] if c["label"] == "w_air")
    assert w_air["evidence_key"].endswith("w_air")


def test_planted_wrong_number_trips_loudly():
    """The DoD's w_air-by-stage class: one digit planted wrong."""
    out = verify_claim("w_air = -0.50 in the flexref stage", EVIDENCE)
    assert out["status"] == "contradicted"
    check = out["checks"][0]
    assert check["claimed"] == -0.50
    assert check["observed"] == -0.42
    assert "w_air" in check["label"]


def test_missing_evidence_fails_closed():
    out = verify_claim("w_air = -0.42", {})
    assert out["status"] == "unverified"
    assert out["checks"][0]["reason"]


def test_unknown_label_fails_closed():
    out = verify_claim("w_desolv = -0.42", EVIDENCE)
    assert out["status"] == "unverified"


def test_tolerance_covers_honest_rounding():
    out = verify_claim("best_score = -108.2", EVIDENCE)
    assert out["status"] == "verified"


def test_no_quantitative_claims_is_not_a_pass():
    out = verify_claim("the docking worked well", EVIDENCE)
    assert out["status"] == "no_quantitative_claims"


def test_unlabeled_numbers_are_ignored_not_checked():
    out = verify_claim("run gave 4 clusters and took 12 minutes", EVIDENCE)
    assert out["status"] == "no_quantitative_claims"
    assert out["unlabeled_numbers_ignored"] == 2


def test_contradiction_beats_verification_in_overall_status():
    out = verify_claim("w_air = -0.50 and cluster_count = 4", EVIDENCE)
    assert out["status"] == "contradicted"


def test_planted_error_against_a_recorded_run(tmp_path):
    """End-to-end through the run record: a scratch run records w_air=-0.42;
    a claim planted with -0.50 must trip, the honest claim must pass."""
    pd = tmp_path / "proj"
    pd.mkdir()
    pm = ProjectManager(global_base=tmp_path / ".magnolia")
    pm.record_run(str(pd), "haddock3_X", "haddock3", "pass",
                  metrics={"w_air": -0.42, "cluster_count": 4})

    rec = pm.get_run(str(pd), "haddock3_X")
    planted = verify_claim(
        "w_air = -0.50 in the flexref stage, cluster_count = 4",
        {"run_record": rec},
    )
    assert planted["status"] == "contradicted"
    honest = verify_claim(
        "w_air = -0.42, cluster_count = 4", {"run_record": rec}
    )
    assert honest["status"] == "verified"


def test_mcp_tool_trips_on_planted_number_in_scratch_session(
    tmp_path, monkeypatch
):
    """DoD, through the actual MCP tool: scratch project + recorded run +
    planted wrong number -> the tool returns 'contradicted'."""
    pd = tmp_path / "proj"
    pd.mkdir()
    pm = ProjectManager(global_base=tmp_path / ".magnolia")
    pm.record_run(str(pd), "haddock3_X", "haddock3", "pass",
                  metrics={"w_air": -0.42})

    monkeypatch.setenv("MAGNOLIA_PROJECT_DIR", str(pd))
    import importlib, sys
    saved = {k: v for k, v in sys.modules.items()
             if k.startswith("compchem_memory")}
    for mod in list(saved):
        del sys.modules[mod]
    try:
        server = importlib.import_module("compchem_memory.server")
        out = json.loads(server.memory_verify_claim(
            "w_air = -0.50 in the flexref stage",
            project_dir=str(pd), run_id="haddock3_X",
        ))
        assert out["status"] == "contradicted", out
        assert out["checks"][0]["observed"] == -0.42
    finally:
        for mod in [m for m in list(sys.modules)
                    if m.startswith("compchem_memory")]:
            del sys.modules[mod]
        sys.modules.update(saved)
