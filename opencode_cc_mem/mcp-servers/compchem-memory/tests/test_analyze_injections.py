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


# --- P3 memory-quality telemetry: application probe join ---------------------

def _probe(session_id, call_id, applied):
    return {"ts": "2026-09-24T10:00:00Z", "event": "application",
            "sessionID": session_id, "tool": "edit", "callID": call_id,
            "applied": applied, "matched": ["alpha"], "distinct": 3}


def test_application_report_full_join():
    events = _events() + [
        _probe("s1", "c1", True),    # staging injection, applied
        _probe("s1", "c2", False),   # project injection, not applied
        _probe("s1", "c9", True),    # orphan: no matching injection
    ]
    r = summarize(events)["application"]
    assert r["probes"] == 3
    assert r["applied"] == 1
    assert r["not_applied"] == 1
    assert r["orphans"] == 1
    assert r["probes_missing"] == 0
    assert r["application_rate"] == 0.5
    assert r["by_tier"]["staging"]["applied"] == 1
    assert r["by_tier"]["project"]["not_applied"] == 1


def test_application_report_missing_probes_not_counted_as_not_applied():
    events = [e for e in _events()]  # c1, c2 injected; no probes at all
    r = summarize(events)["application"]
    assert r["probes"] == 0
    assert r["probes_missing"] == 2
    assert r["application_rate"] is None


def test_application_report_empty_log():
    r = summarize([])["application"]
    assert r == {"probes": 0, "applied": 0, "not_applied": 0,
                 "application_rate": None, "probes_missing": 0,
                 "orphans": 0, "by_tier": {}}


def test_application_report_ignores_non_injected_rows_as_keys():
    # c3 is a skip row (injected=0): a probe for it is an orphan, and it
    # never enters probes_missing.
    events = _events() + [_probe("s1", "c3", True)]
    r = summarize(events)["application"]
    assert r["orphans"] == 1
    assert r["probes_missing"] == 2
