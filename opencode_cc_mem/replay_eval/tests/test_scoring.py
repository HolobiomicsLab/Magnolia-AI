import json

from replay_eval import judge
from replay_eval.judge import _merge_verdicts


def test_merge_verdicts_first_wins_and_fills_missing():
    a = [{"id": 1, "grounded": True}, {"id": 2, "grounded": False}]
    b = [{"id": 1, "grounded": False}, {"id": 3, "grounded": True}]  # id1 dup, id3 fills
    merged = _merge_verdicts([a, b])
    by_id = {v["id"]: v for v in merged}
    assert by_id[1]["grounded"] is True        # first verdict wins
    assert by_id[2]["grounded"] is False
    assert by_id[3]["grounded"] is True        # missing id filled by retry
    assert [v["id"] for v in merged] == [1, 2, 3]


def test_rescore_scores_against_answered(tmp_path):
    run = tmp_path / "run"
    (run / "judge").mkdir(parents=True)
    (run / "outputs").mkdir()
    # slice with 3 candidates but only 2 verdicts returned
    (run / "outputs" / "s1.json").write_text(json.dumps({"n_candidates": 3}))
    (run / "judge" / "s1.json").write_text(json.dumps({"verdicts": [
        {"id": 0, "grounded": True, "durable": True, "specific": True},
        {"id": 1, "grounded": True, "durable": False, "specific": False},
    ]}))
    (run / "summary.json").write_text(json.dumps({
        "arm": {"name": "a", "model": "m", "provider": "p", "prompt_version": "v1",
                "flags": {}, "code_ref": "/tmp"},
        "corpus_dir": "/tmp", "corpus_version": 1, "n_slices": 1, "ok": 1,
        "parse_fail": 0, "finish_length": 0, "total_candidates": 3,
        "total_ms": 1, "records": []}))
    (run / "judge" / "judge_summary.json").write_text(json.dumps(
        {"judge_model": "glm-5.3", "totals": {}}))

    t = judge.rescore(run)["totals"]
    assert t["candidates"] == 3
    assert t["answered"] == 2
    assert t["missing"] == 1          # missing counted as missing...
    assert t["grounded"] == 2         # ...not as a negative verdict
    assert t["grounded"] / t["answered"] == 1.0

    from replay_eval import report
    md = report.render_report(run)
    assert "completeness" in md and "missing, not negative" in md
