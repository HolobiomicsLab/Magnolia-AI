"""Regression guard (2026-09-11 hotfix): every memory call that expects
structured output must pass disable_thinking=True.

DeepSeek thinking models can spend the entire completion budget on hidden
reasoning and return empty content (or a false '[]'), so call sites that
consume content must disable thinking. Handover and consolidation already
did; extraction, retrieval, promotion and compaction did not, which blanked
the reranker and leaked extraction slices.
"""


def test_llm_select_memories_disables_thinking(monkeypatch):
    from compchem_memory import retrieval
    captured = {}

    def fake(system_prompt, user_content, **kwargs):
        captured.update(kwargs)
        return {"selected": ["a.md"]}

    monkeypatch.setattr(retrieval, "call_llm_json", fake)
    out = retrieval.llm_select_memories("task", [{"filename": "a.md", "title": "A"}])
    assert out == ["a.md"]
    assert captured.get("disable_thinking") is True


def test_llm_distill_disables_thinking(monkeypatch):
    from compchem_memory import extraction
    captured = {}

    def fake(system_prompt, user_content, **kwargs):
        captured.update(kwargs)
        return []

    monkeypatch.setattr(extraction, "call_llm_json", fake)
    assert extraction.AutomaticMemoryExtractor()._llm_distill([{"event_type": "x"}]) == []
    assert captured.get("disable_thinking") is True


def test_distill_transcript_disables_thinking(monkeypatch):
    from compchem_memory import extraction
    captured = {}

    def fake(system_prompt, user_content, **kwargs):
        captured.update(kwargs)
        return []

    monkeypatch.setattr(extraction, "call_llm_json", fake)
    assert extraction.AutomaticMemoryExtractor().distill_transcript("transcript") == []
    assert captured.get("disable_thinking") is True


def test_promotion_defaults_disable_thinking(monkeypatch):
    from compchem_memory import llm, promotion
    calls = []

    def fake(system_prompt, user_content, **kwargs):
        calls.append(kwargs)
        return {"approve": True}

    monkeypatch.setattr(llm, "call_llm_json", fake)
    entry = {"meta": {"title": "T", "tags": []}, "body": "B"}
    promotion._default_judge(entry, 0)
    promotion._default_drafter(entry)
    promotion._default_checker({"name": "n", "description": "d"}, [])
    assert len(calls) == 3
    assert all(c.get("disable_thinking") is True for c in calls)


def test_compaction_disables_thinking(monkeypatch):
    from compchem_memory import compaction
    captured = {}

    def fake(system_prompt, user_content, **kwargs):
        captured.update(kwargs)
        return "summary text"

    monkeypatch.setattr(compaction, "call_llm", fake)
    res = compaction.compact_with_agent([{"timestamp": "t", "content": "x"}])
    assert res is not None
    assert captured.get("disable_thinking") is True
