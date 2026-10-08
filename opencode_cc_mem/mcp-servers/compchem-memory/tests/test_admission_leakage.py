"""Stage 1.5 session-leakage screen (ACT-MEM-LEAKAGE-SCREEN; idea ticket from
the RRSI redigest, arXiv 2609.24972). Contract under test:
- flags titles whose claim rests on session-local referents (session ids,
  absolute paths, bare run ids, unanchored numeric results)
- never screens content (evidence may cite paths/ids; the claim must not
  rest on them)
- fails open per candidate (a dead screen must not zero a session)
- is OFF by default; only the arm flag turns it on
"""

import compchem_memory.admission as adm
from compchem_memory.admission import AdmissionGate, leakage_screen


def _no_llm(prompt, payload, **kw):
    raise RuntimeError("judge dead — fail-open expected")


def _admit_all(prompt, payload, **kw):
    import json
    cands = json.loads(payload)
    return [{"title": c["title"], "decision": "admit", "reason": "ok",
             "class": "finding"} for c in cands]


def test_passes_durable_titles_with_numbers_and_paths_in_content():
    # numbers WITH a domain noun survive; paths in CONTENT are never screened
    assert leakage_screen({"title": "v6 bake-off recall 72% vs >=95% gate"}) is None
    assert leakage_screen({"title": "Arm3 volume 43.6% vs <=40% gate"}) is None
    c = {"title": "KFERQ docking pose 2 binds pocket",
         "content": "see /home/tjiang/repos/project_magnolia-exp/runs/x/out.pdb"}
    assert leakage_screen(c) is None


def test_flags_session_ids_and_absolute_path_titles():
    assert leakage_screen({"title": "ses_f612d4af5ffe4SAG showed k3 judge drift"}) is not None
    assert leakage_screen({"title": "/home/tjiang/repos/ad_verum/start-adverum.sh fixed"}) is not None


def test_flags_bare_run_id_only_without_domain_noun():
    assert leakage_screen({"title": "2026-10-02_080245_hsc70-arm2-admission completed"}) is not None
    # same id WITH a durable referent survives
    assert leakage_screen(
        {"title": "2026-10-02_080245_hsc70-arm2-admission: recall 72% measured"}) is None


def test_flags_bare_numeric_result_without_anchor():
    assert leakage_screen({"title": "Up 14.1 points on the evolve split"}) is not None
    assert leakage_screen({"title": "score improved 3.2"}) is None  # no pts/% unit -> not flagged


def test_fail_open_on_broken_candidate():
    assert leakage_screen({"title": 12345}) is None          # non-string -> pass
    assert leakage_screen({}) is None                        # no title -> pass
    assert leakage_screen(None) is None                      # garbage -> pass


def test_gate_screen_off_by_default(tmp_path):
    g = AdmissionGate(tmp_path, llm_json=_no_llm)
    assert g.leakage_screen is False
    res = g.admit([{"title": "ses_f612d4af5ffe4SAG showed k3 judge drift",
                    "type": "note", "content": "x"}])
    assert len(res.admitted) == 1                            # judge dead -> fail-open
    assert not any(r["stage"] == "leakage" for r in res.rejected)


def test_gate_screen_rejects_before_judge(tmp_path):
    calls = {"n": 0}

    def spy(prompt, payload, **kw):
        calls["n"] += 1
        return _admit_all(prompt, payload, **kw)

    g = AdmissionGate(tmp_path, llm_json=spy, leakage_screen=True)
    res = g.admit([
        {"title": "ses_f612d4af5ffe4SAG showed k3 judge drift", "type": "note", "content": "x"},
        {"title": "v6 bake-off recall 72% vs >=95% gate", "type": "scientific_finding", "content": "y"},
    ])
    leaky = [r for r in res.rejected if r["stage"] == "leakage"]
    assert len(leaky) == 1 and "session id" in leaky[0]["reason"]
    assert [c["title"] for c in res.admitted] == ["v6 bake-off recall 72% vs >=95% gate"]
    assert calls["n"] == 1                                   # judge still batched, once
    # rejection is logged with the stage reason
    log = (tmp_path / "admission-log.jsonl").read_text()
    assert "session_leakage: session id in title" in log


def test_gate_screen_fail_open_keeps_batch_alive(tmp_path):
    # screen works even with a DEAD judge: durable candidate admitted
    # fail-open, leaky candidate still rejected by the screen
    g = AdmissionGate(tmp_path, llm_json=_no_llm, leakage_screen=True)
    res = g.admit([
        {"title": "v6 bake-off recall 72% vs >=95% gate", "type": "scientific_finding", "content": "x"},
        {"title": "ses_f612d4af5ffe4SAG showed k3 judge drift", "type": "note", "content": "y"},
    ])
    assert res.judge_available is False
    assert [c["title"] for c in res.admitted] == ["v6 bake-off recall 72% vs >=95% gate"]
    assert [r["stage"] for r in res.rejected] == ["leakage"]
