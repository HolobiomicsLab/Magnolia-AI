"""Tests for the injection-log baseline summarizer (plan D2)."""

from compchem_memory.analyze_injections import load_events, summarize


def _events():
    return [
        {
            "ts": "2026-09-18T09:00:00Z", "sessionID": "s1", "tool": "edit",
            "callID": "c1", "outcome": "ok", "injected": 1, "hits": 2,
            "titles": ["Alpha learning"],
            "entries": [{"path": "/x/.magnolia/staging/a.md", "tier": "staging", "score": 3}],
        },
        {
            "ts": "2026-09-18T09:01:00Z", "sessionID": "s1", "tool": "edit",
            "callID": "c2", "outcome": "error?", "injected": 1, "hits": 1,
            "titles": ["Beta doctrine"],
            "entries": [{"path": "/x/.magnolia/entries/b.md", "tier": "project", "score": 4}],
        },
        {
            "ts": "2026-09-18T09:02:00Z", "sessionID": "s1", "tool": "edit",
            "callID": "c3", "outcome": "ok", "injected": 0, "skipped": "budget",
        },
    ]


def test_summarize_counts_injections_and_outcomes():
    r = summarize(_events())
    assert r["events"] == 3
    assert r["injected_events"] == 2
    assert r["outcomes"] == {"ok": 2, "error?": 1}
    assert r["skips"] == {"budget": 1}
    assert r["distinct_paths_surfaced"] == 2
    assert r["surfaced_by_path"]["/x/.magnolia/staging/a.md"] == 1


def test_summarize_resolves_tier_coverage_with_store(tmp_path):
    store = tmp_path
    staging = store / ".magnolia" / "staging"
    staging.mkdir(parents=True)
    (staging / "a.md").write_text(
        "---\ntitle: Alpha learning\ntype: note\n---\n\nbody\n"
    )
    entries = store / ".magnolia" / "entries"
    entries.mkdir(parents=True)
    (entries / "b.md").write_text(
        "---\ntitle: Beta doctrine\ntype: note\n---\n\nbody\n"
    )
    (entries / "c.md").write_text(
        "---\ntitle: Never surfaced\ntype: note\n---\n\nbody\n"
    )
    r = summarize(_events(), store_dir=str(store))
    assert r["tier_totals"] == {"entries": 2, "staging": 1}
    assert r["coverage_by_tier"] == {"entries": "1/2", "staging": "1/1"}


def test_load_events_tolerates_bad_lines(tmp_path):
    p = tmp_path / "log.jsonl"
    p.write_text('{"a": 1}\nnot json\n{"a": 2}\n')
    events = load_events(p)
    assert len(events) == 2
    assert sum(e.get("a", 0) for e in events) == 3
