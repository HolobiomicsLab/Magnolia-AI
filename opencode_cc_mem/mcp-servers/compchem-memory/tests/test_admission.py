"""Admission gate tests (R1–R4) — all with scripted fake judges, no real LLM."""

from __future__ import annotations

import json
from pathlib import Path

from compchem_memory.admission import (
    AdmissionGate,
    is_same_claim,
    load_profile,
    recent_admitted_titles,
    session_has_signal,
)


def _no_llm(prompt, payload, **kw):
    raise AssertionError("LLM must not be called for this case")


def _judge(verdicts):
    def f(prompt, payload, **kw):
        return verdicts
    return f


def test_idle_gate_blocks_quiet_session(tmp_path):
    g = AdmissionGate(tmp_path, llm_json=_no_llm)
    res = g.admit([{"title": "anything", "content": "x"}],
                  transcript="Hello. How are you? Fine weather today.",
                  session="s1")
    assert res.idle_skipped and not res.admitted
    row = json.loads((tmp_path / "admission-log.jsonl").read_text().splitlines()[0])
    assert row["reason"] == "idle_no_signal"


def test_idle_gate_passes_session_with_signal(tmp_path):
    g = AdmissionGate(tmp_path, llm_json=_judge([]))
    res = g.admit([{"title": "c", "content": "x"}],
                  transcript="The job failed with exit code 1 after the parameter change",
                  session="s1")
    assert not res.idle_skipped


def test_signal_kinds(tmp_path):
    worth, kinds = session_has_signal("set --exhaustiveness 16, see arXiv:2609.27334")
    assert worth and "parameter" in kinds and "reference" in kinds
    worth2, kinds2 = session_has_signal("traceback: Fatal error in gmx mdrun")
    assert worth2 and "error" in kinds2


def test_r4_duplicate_kill(tmp_path):
    g = AdmissionGate(tmp_path, llm_json=_judge([{"title": "fresh", "decision": "admit"}]))
    # prime the ledger with an admitted title
    g._ledger("now", "s0", "HADDOCK3 OOM kill on Azzurra 32 concurrent CNS jobs", "admit", "class=error: x")
    res = g.admit([{"title": "HADDOCK3 OOM kills on Azzurra with 32 CNS jobs", "content": "x"}],
                  session="s1")
    assert not res.admitted
    assert res.rejected[0]["stage"] == "r4_dup"


def test_r4_in_batch_twin_kill(tmp_path):
    # One distiller call emitting two same-claim candidates: the first is
    # judged, the second must die at Stage 1 against the batch itself — the
    # ledger snapshot predates the batch and knows nothing of the first twin.
    keep = "Replay-eval harness approved as acceptance-test substrate"
    twin = "Replay-eval harness approved as the acceptance-test substrate"
    g = AdmissionGate(tmp_path, llm_json=_judge(
        [{"title": keep, "decision": "admit", "reason": "design decision",
          "class": "reference"}]))
    res = g.admit([{"title": keep, "content": "x"}, {"title": twin, "content": "y"}],
                  session="s1")
    assert [c["title"] for c in res.admitted] == [keep]
    assert res.rejected[0]["stage"] == "r4_dup_batch"
    assert "same batch" in res.rejected[0]["reason"]
    rows = [json.loads(l) for l in (tmp_path / "admission-log.jsonl").read_text().splitlines()]
    assert any(r["reason"] == "r4_duplicate_in_batch" for r in rows)
    # the killed twin never enters the admitted-title ledger
    assert recent_admitted_titles(tmp_path) == [keep]


def test_r4_in_batch_twin_never_reaches_judge(tmp_path):
    calls = []

    def spy(prompt, payload, **kw):
        calls.append(json.loads(payload))
        return [{"title": "alpha beta gamma delta", "decision": "admit",
                 "reason": "ok", "class": "reference"}]

    g = AdmissionGate(tmp_path, llm_json=spy)
    res = g.admit([{"title": "alpha beta gamma delta", "content": "x"},
                   {"title": "alpha beta gamma delta epsilon", "content": "y"}],
                  session="s1")
    assert len(calls) == 1 and len(calls[0]) == 1  # judge scored only the survivor
    assert [c["title"] for c in res.admitted] == ["alpha beta gamma delta"]
    assert res.rejected[0]["stage"] == "r4_dup_batch"


def test_judge_admits_and_rejects(tmp_path):
    verdicts = [
        {"title": "keep", "decision": "admit", "reason": "error fix", "class": "error"},
        {"title": "drop", "decision": "reject", "reason": "narration", "class": "narration"},
    ]
    g = AdmissionGate(tmp_path, llm_json=_judge(verdicts))
    res = g.admit([{"title": "keep", "content": "fix"}, {"title": "drop", "content": "we ran"}],
                  session="s1")
    assert [c["title"] for c in res.admitted] == ["keep"]
    assert res.rejected[0]["stage"] == "judge"
    titles = recent_admitted_titles(tmp_path)
    assert titles == ["keep"]


def test_judge_failure_fails_open(tmp_path):
    def dead(prompt, payload, **kw):
        return None
    g = AdmissionGate(tmp_path, llm_json=dead)
    res = g.admit([{"title": "a", "content": "x"}, {"title": "b", "content": "y"}],
                  session="s1")
    assert res.judge_available is False
    assert len(res.admitted) == 2  # fail-open: never silently drop everything


def test_profile_file(tmp_path):
    assert load_profile(tmp_path) == "operations"
    (tmp_path / ".admission-profile").write_text("findings_keep")
    assert load_profile(tmp_path) == "findings_keep"
    (tmp_path / ".admission-profile").write_text("garbage")
    assert load_profile(tmp_path) == "operations"


def test_is_same_claim():
    assert is_same_claim("HADDOCK3 OOM kill 32 CNS jobs", "HADDOCK3 OOM kills with 32 CNS jobs")
    assert not is_same_claim("HADDOCK3 OOM kill 32 CNS jobs", "GROMACS minimization converged")
    assert not is_same_claim("short", "short")
