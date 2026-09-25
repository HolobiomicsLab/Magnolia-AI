import json

from replay_eval import judge, runner
from replay_eval.loader import Slice


def test_blind_payload_strips_arm_names_and_shuffles():
    slc = Slice(name="s1", project="p", session="ses", capture_version="c1",
                chars=10, text="T")
    cands = [{"title": "alpha", "content": "a"}, {"title": "beta", "content": "b"}]
    user, items = judge._blind_payload(slc, cands)
    assert "alpha" in user and "beta" in user
    # ids are positional, content present, order shuffled deterministically
    assert [i["id"] for i in items] in ([0, 1], [1, 0])
    again = judge._blind_payload(slc, cands)[1]
    assert again == items  # same seed -> same shuffle


def test_parse_candidates_variants():
    assert runner._parse_candidates('[{"title": "t"}]') == ([{"title": "t"}], True)
    assert runner._parse_candidates('```json\n[1,2]\n```')[0] == [1, 2]
    assert runner._parse_candidates('noise before [ {"a": 1}] noise after')[0] == [{"a": 1}]
    assert runner._parse_candidates(None) == ([], False)
    assert runner._parse_candidates('no array here') == ([], False)
    assert runner._parse_candidates('{"x": 1}')[1] is False  # not a list


def test_load_arm_validation(tmp_path):
    fake_src = tmp_path / "src" / "compchem_memory"
    fake_src.mkdir(parents=True)
    good = tmp_path / "arm.yaml"
    good.write_text(
        "name: a\ncode_ref: " + str(tmp_path / "src") +
        "\nmodel: m\nprompt_version: v1\nflags: {max_tokens: 100}\n")
    arm = runner.load_arm(good)
    assert arm["name"] == "a"

    bad = tmp_path / "bad.yaml"
    bad.write_text("name: a\n")  # missing required keys
    try:
        runner.load_arm(bad)
        raise AssertionError("expected ValueError")
    except ValueError:
        pass

    wrong_pv = tmp_path / "pv.yaml"
    wrong_pv.write_text(
        "name: a\ncode_ref: " + str(tmp_path / "src") +
        "\nmodel: m\nprompt_version: v9\nflags: {}\n")
    try:
        runner.load_arm(wrong_pv)
        raise AssertionError("expected ValueError")
    except ValueError:
        pass


def test_report_renders_without_judge(tmp_path):
    from replay_eval import report

    run = tmp_path / "run"
    (run / "judge").mkdir(parents=True)
    (run / "summary.json").write_text(json.dumps({
        "arm": {"name": "baseline", "model": "m", "provider": "p",
                "prompt_version": "v1", "flags": {}, "code_ref": "/tmp"},
        "corpus_dir": "/tmp", "corpus_version": 1,
        "n_slices": 2, "ok": 2, "parse_fail": 0, "finish_length": 0,
        "total_candidates": 5, "total_ms": 1200,
        "records": [{"slice": "s1", "capture_version": "c1"}],
    }))
    md = report.render_report(run)
    assert "baseline" in md and "Not judged yet" in md
    out = report.write_report(run)
    assert out.exists()
