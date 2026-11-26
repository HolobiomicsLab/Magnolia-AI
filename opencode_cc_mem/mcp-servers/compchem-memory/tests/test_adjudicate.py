"""Policy tests for the optional LLM adjudicator.

The model is always stubbed. What is tested is the GATE: what may clear a
pair, what must hold, and what happens when the model is unavailable.
"""


import pytest

from compchem_memory import adjudicate as A
from compchem_memory.contradictions import candidates_against_entry, load_entry_file


def _pair(param, a_vals, b_vals, binding="proximity", name="p1"):
    return {
        "a": {"filename": "incoming.md", "tier": "staging", "id": "incoming", "title": ""},
        "b": {"filename": f"{name}.md", "tier": "project", "id": name, "title": ""},
        "param_conflicts": [{"param": param, "a_values": a_vals, "b_values": b_vals,
                             "binding": binding}],
        "signal_terms": [param], "overlap": 3, "signal_count": 1,
    }


def _texts(pairs):
    params = sorted({"w_air"} | {pc["param"] for p in pairs
                                 for pc in p.get("param_conflicts", [])})
    body = " ".join(f"{param} = 0.1" for param in params)
    out = {}
    for p in pairs:
        for side in (p["a"], p["b"]):
            out[(side["tier"], side["filename"])] = f"body of {side['filename']} {body}"
    return out


def _stub(monkeypatch, verdicts, calls):
    def fake(system, prompt, **kw):
        calls.append(1)
        return ([{"id": i + 1, "verdict": v, "quoted_a": "w_air = 0.1",
                  "quoted_b": "w_air = 0.1", "reason": "r"}
                 for i, v in enumerate(verdicts)], "stop")
    monkeypatch.setattr("compchem_memory.llm.call_llm_json", fake)


def test_disabled_returns_pairs_unchanged(tmp_path, monkeypatch):
    monkeypatch.delenv("MAGNOLIA_CONFLICT_LLM", raising=False)
    pairs = [_pair("w_air", ["0.1"], ["0.01"])]
    out = A.filter_blocking(pairs, _texts(pairs), tmp_path)
    assert out == pairs
    assert not (tmp_path / "reflex").exists()


def test_enabled_clears_agreement_and_scope(tmp_path, monkeypatch):
    monkeypatch.setenv("MAGNOLIA_CONFLICT_LLM", "1")
    pairs = [_pair("w_air", ["0.1"], ["0.01", "0.1"], name="one"),
             _pair("w_air", ["0.1"], ["0.01"], name="two")]
    calls = []
    _stub(monkeypatch, ["scope_difference", "agreement"], calls)
    out = A.filter_blocking(pairs, _texts(pairs), tmp_path)
    assert out == []
    assert len(calls) == 1
    notes = (tmp_path / "reflex" / "conflict-notes.jsonl").read_text().strip().splitlines()
    assert len(notes) == 2


def test_explicit_disagreement_cannot_be_cleared(tmp_path, monkeypatch):
    monkeypatch.setenv("MAGNOLIA_CONFLICT_LLM", "1")
    pairs = [_pair("w_air", ["0.1"], ["0.01"], binding="explicit")]
    calls = []
    _stub(monkeypatch, ["agreement"], calls)
    out = A.filter_blocking(pairs, _texts(pairs), tmp_path)
    assert out == pairs


def test_llm_error_fails_closed_and_logs(tmp_path, monkeypatch):
    monkeypatch.setenv("MAGNOLIA_CONFLICT_LLM", "1")
    pairs = [_pair("w_air", ["0.1"], ["0.01", "0.1"])]
    monkeypatch.setattr("compchem_memory.llm.call_llm_json",
                        lambda *a, **k: (_ for _ in ()).throw(TimeoutError("down")))
    out = A.filter_blocking(pairs, _texts(pairs), tmp_path)
    assert out == pairs
    log = (tmp_path / "reflex" / "unadjudicated.jsonl").read_text()
    assert "llm_error:TimeoutError" in log


def test_unquoted_verdict_is_ignored(tmp_path, monkeypatch):
    monkeypatch.setenv("MAGNOLIA_CONFLICT_LLM", "1")
    pairs = [_pair("w_air", ["0.1"], ["0.01", "0.1"])]
    monkeypatch.setattr("compchem_memory.llm.call_llm_json",
                        lambda *a, **k: ([{"id": 1, "verdict": "agreement", "reason": "no quotes"}], "stop"))
    out = A.filter_blocking(pairs, _texts(pairs), tmp_path)
    assert out == pairs
    assert "invalid_or_unquoted_verdict" in (tmp_path / "reflex" / "unadjudicated.jsonl").read_text()


def test_fabricated_quote_is_ignored(tmp_path, monkeypatch):
    monkeypatch.setenv("MAGNOLIA_CONFLICT_LLM", "1")
    pairs = [_pair("w_air", ["0.1"], ["0.01", "0.1"])]
    monkeypatch.setattr("compchem_memory.llm.call_llm_json",
                        lambda *a, **k: ([{"id": 1, "verdict": "agreement",
                                          "quoted_a": "not in the note",
                                          "quoted_b": "also not in the note",
                                          "reason": "fabricated"}], "stop"))
    out = A.filter_blocking(pairs, _texts(pairs), tmp_path)
    assert out == pairs
    assert "quote_not_in_note" in (tmp_path / "reflex" / "unadjudicated.jsonl").read_text()


def test_quote_from_the_other_note_is_ignored(tmp_path, monkeypatch):
    monkeypatch.setenv("MAGNOLIA_CONFLICT_LLM", "1")
    pairs = [_pair("w_air", ["0.1"], ["0.01", "0.1"])]
    texts = {("staging", "incoming.md"): "incoming says w_air = 0.1",
             ("project", "p1.md"): "existing says w_air = 0.01"}
    monkeypatch.setattr("compchem_memory.llm.call_llm_json",
                        lambda *a, **k: ([{"id": 1, "verdict": "agreement",
                                          "quoted_a": "existing says w_air = 0.01",
                                          "quoted_b": "existing says w_air = 0.01",
                                          "reason": "wrong note"}], "stop"))
    out = A.filter_blocking(pairs, texts, tmp_path)
    assert out == pairs
    assert "quote_not_in_note" in (tmp_path / "reflex" / "unadjudicated.jsonl").read_text()


def test_quote_with_data_block_prefix_is_accepted(tmp_path, monkeypatch):
    monkeypatch.setenv("MAGNOLIA_CONFLICT_LLM", "1")
    pairs = [_pair("w_air", ["0.1"], ["0.01", "0.1"])]
    monkeypatch.setattr("compchem_memory.llm.call_llm_json",
                        lambda *a, **k: ([{"id": 1, "verdict": "agreement",
                                          "quoted_a": "| w_air = 0.1",
                                          "quoted_b": "| w_air = 0.1",
                                          "reason": "prefixed"}], "stop"))
    out = A.filter_blocking(pairs, _texts(pairs), tmp_path)
    assert out == []


def test_verdicts_are_cached_by_content(tmp_path, monkeypatch):
    monkeypatch.setenv("MAGNOLIA_CONFLICT_LLM", "1")
    pairs = [_pair("w_air", ["0.1"], ["0.01", "0.1"])]
    calls = []
    _stub(monkeypatch, ["agreement"], calls)
    A.filter_blocking(pairs, _texts(pairs), tmp_path)
    A.filter_blocking(pairs, _texts(pairs), tmp_path)
    assert len(calls) == 1, "second identical pass must be served from cache"


def test_trivial_quote_is_ignored(tmp_path, monkeypatch):
    monkeypatch.setenv("MAGNOLIA_CONFLICT_LLM", "1")
    pairs = [_pair("w_air", ["0.1"], ["0.01", "0.1"])]
    monkeypatch.setattr("compchem_memory.llm.call_llm_json",
                        lambda *a, **k: ([{"id": 1, "verdict": "agreement",
                                          "quoted_a": "0", "quoted_b": "0",
                                          "reason": "trivial"}], "stop"))
    out = A.filter_blocking(pairs, _texts(pairs), tmp_path)
    assert out == pairs
    assert "quote_not_in_note" in (tmp_path / "reflex" / "unadjudicated.jsonl").read_text()


def test_read_only_store_fails_closed(tmp_path, monkeypatch):
    monkeypatch.setenv("MAGNOLIA_CONFLICT_LLM", "1")
    pairs = [_pair("w_air", ["0.1"], ["0.01", "0.1"])]
    _stub(monkeypatch, ["agreement"], [])
    store = tmp_path / "store"
    store.mkdir()
    store.chmod(0o555)
    try:
        out = A.filter_blocking(pairs, _texts(pairs), store)
    finally:
        store.chmod(0o755)
    assert out == pairs


def test_cache_key_includes_prompt_version():
    assert A.PROMPT_VERSION in A._cache_key("a", "b", "w_air")


def test_string_id_is_normalized(tmp_path, monkeypatch):
    monkeypatch.setenv("MAGNOLIA_CONFLICT_LLM", "1")
    pairs = [_pair("w_air", ["0.1"], ["0.01", "0.1"])]
    monkeypatch.setattr("compchem_memory.llm.call_llm_json",
                        lambda *a, **k: ([{"id": "1", "verdict": "agreement",
                                          "quoted_a": "w_air = 0.1",
                                          "quoted_b": "w_air = 0.1",
                                          "reason": "r"}], "stop"))
    out = A.filter_blocking(pairs, _texts(pairs), tmp_path)
    assert out == []


def test_truncated_response_fails_closed(tmp_path, monkeypatch):
    monkeypatch.setenv("MAGNOLIA_CONFLICT_LLM", "1")
    pairs = [_pair("w_air", ["0.1"], ["0.01", "0.1"])]
    monkeypatch.setattr("compchem_memory.llm.call_llm_json",
                        lambda *a, **k: (None, "length"))
    out = A.filter_blocking(pairs, _texts(pairs), tmp_path)
    assert out == pairs
    assert "truncated_response" in (tmp_path / "reflex" / "unadjudicated.jsonl").read_text()


def test_same_filename_in_both_tiers_uses_the_right_text(tmp_path, monkeypatch):
    monkeypatch.setenv("MAGNOLIA_CONFLICT_LLM", "1")
    pairs = [{
        "a": {"filename": "x.md", "tier": "staging", "id": "a", "title": ""},
        "b": {"filename": "x.md", "tier": "project", "id": "b", "title": ""},
        "param_conflicts": [{"param": "w_air", "a_values": ["0.1"],
                             "b_values": ["0.01"], "binding": "proximity"}],
        "signal_terms": ["w_air"], "overlap": 3, "signal_count": 1,
    }]
    texts = {("staging", "x.md"): "staging w_air = 0.1",
             ("project", "x.md"): "project w_air = 0.01"}
    # A quote from the project text must not be accepted for NOTE A (staging).
    monkeypatch.setattr("compchem_memory.llm.call_llm_json",
                        lambda *a, **k: ([{"id": 1, "verdict": "agreement",
                                          "quoted_a": "project w_air = 0.01",
                                          "quoted_b": "project w_air = 0.01",
                                          "reason": "wrong side"}], "stop"))
    out = A.filter_blocking(pairs, texts, tmp_path)
    assert out == pairs
    assert "quote_not_in_note" in (tmp_path / "reflex" / "unadjudicated.jsonl").read_text()


def _write_entry(path, title, body):
    path.write_text(f"---\ntitle: {title}\n---\n{body}\n", encoding="utf-8")


def test_end_to_end_explicit_pair_stays_blocking(tmp_path, monkeypatch):
    monkeypatch.setenv("MAGNOLIA_CONFLICT_LLM", "1")
    src_file = tmp_path / "incoming.md"
    tgt_file = tmp_path / "existing.md"
    _write_entry(src_file, "weights", "w_air = 0.1 and w_elec = 0.2")
    _write_entry(tgt_file, "weights", "w_air = 0.01 and w_elec = 0.2")
    src = load_entry_file(src_file, "staging")
    tgt = load_entry_file(tgt_file, "project")
    pairs = candidates_against_entry(src, [tgt])
    assert len(pairs) == 1
    assert pairs[0]["param_conflicts"][0]["binding"] == "explicit"
    texts = {(src["tier"], src["filename"]): src["text"],
             (tgt["tier"], tgt["filename"]): tgt["text"]}
    _stub(monkeypatch, ["agreement"], [])
    assert A.filter_blocking(pairs, texts, tmp_path) == pairs


def test_end_to_end_proximity_pair_is_cleared(tmp_path, monkeypatch):
    monkeypatch.setenv("MAGNOLIA_CONFLICT_LLM", "1")
    src_file = tmp_path / "incoming.md"
    tgt_file = tmp_path / "existing.md"
    _write_entry(src_file, "weights", "w_air = 0.1 and w_elec = 0.2")
    _write_entry(tgt_file, "weights", "w_air = 0.01 and w_elec = 0.2 and w_air = 0.1")
    src = load_entry_file(src_file, "staging")
    tgt = load_entry_file(tgt_file, "project")
    pairs = candidates_against_entry(src, [tgt])
    assert len(pairs) == 1
    assert pairs[0]["param_conflicts"][0]["binding"] == "proximity"
    texts = {(src["tier"], src["filename"]): src["text"],
             (tgt["tier"], tgt["filename"]): tgt["text"]}
    _stub(monkeypatch, ["agreement"], [])
    assert A.filter_blocking(pairs, texts, tmp_path) == []


# --- the labelled regression set (10 items, known truth) --------------------
# The dangerous direction is "true contradiction labelled non-blocking".

LABELLED = [
    ("contradiction", "w_air", ["0.1"], ["0.01"], "explicit"),
    ("contradiction", "w_elec", ["0.2"], ["0.3"], "explicit"),
    ("contradiction", "bsa", ["1420.2"], ["1676.8"], "explicit"),
    ("contradiction", "kd", ["12.0"], ["40.0"], "explicit"),
    ("agreement", "ic50", ["3.0"], ["3000.0"], "proximity"),
    ("extraction_artifact", "w_air", ["0.1"], ["0.1", "1.83"], "proximity"),
    ("agreement", "w_elec", ["0.2"], ["0.2", "1.0"], "proximity"),
    ("scope_difference", "w_air", ["0.1"], ["0.01", "0.1"], "proximity"),
    ("scope_difference", "n_models", ["88.0"], ["82.0"], "proximity"),
    ("agreement", "w_air", ["0.1"], ["0.1"], "proximity"),
]


@pytest.mark.parametrize("truth,param,av,bv,binding", LABELLED)
def test_labelled_set_blocks_only_true_contradictions(tmp_path, monkeypatch,
                                                      truth, param, av, bv, binding):
    monkeypatch.setenv("MAGNOLIA_CONFLICT_LLM", "1")
    pairs = [_pair(param, av, bv, binding=binding)]
    _stub(monkeypatch, [truth], [])
    out = A.filter_blocking(pairs, _texts(pairs), tmp_path)
    if truth == "contradiction":
        assert out == pairs, f"{param}: a real contradiction was cleared"
    else:
        assert out == [], f"{param}: {truth} should not block"


def test_labelled_set_as_one_batch(tmp_path, monkeypatch):
    """All ten items in a single batched call, as in production."""
    monkeypatch.setenv("MAGNOLIA_CONFLICT_LLM", "1")
    pairs = [_pair(p, av, bv, binding="proximity", name=f"n{i}")
             for i, (_, p, av, bv, b) in enumerate(LABELLED)]
    _stub(monkeypatch, [t for t, *_ in LABELLED], [])
    out = A.filter_blocking(pairs, _texts(pairs), tmp_path)
    expected = [pairs[i] for i, (t, *_rest) in enumerate(LABELLED) if t == "contradiction"]
    assert out == expected
