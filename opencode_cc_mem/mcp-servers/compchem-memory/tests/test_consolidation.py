from compchem_memory.consolidation import merge_entries


def _e(path, title, body, sessions=None, ses=None, tags=None, conf=0.5):
    meta = {"title": title, "type": "scientific_finding", "confidence": conf,
            "tags": tags or []}
    if sessions is not None:
        meta["observed_in_sessions"] = sessions
    if ses is not None:
        meta["opencode_session_id"] = ses
    return {"id": path, "path": path, "meta": meta, "body": body}


def test_merge_unions_distinct_sessions_not_copy_count():
    entries = [
        _e("a.md", "N-term ALA extension wins", "long canonical body here .....", ses="ses_1"),
        _e("b.md", "ALA extension outperforms C-term", "body two", ses="ses_1"),
        _e("c.md", "N-terminal alanine beats C-terminal", "body three", ses="ses_2"),
    ]
    merged = merge_entries(entries)
    assert sorted(merged["meta"]["observed_in_sessions"]) == ["ses_1", "ses_2"]
    assert merged["meta"]["observation_count"] == 2


def test_merge_picks_longest_body_as_canonical_and_keeps_provenance():
    entries = [
        _e("a.md", "short", "tiny", ses="ses_1"),
        _e("b.md", "long one", "this is the much longer canonical body text", ses="ses_2"),
    ]
    merged = merge_entries(entries)
    assert "much longer canonical" in merged["body"]
    assert "tiny" in merged["body"]
    assert set(merged["sources"]) == {"a.md", "b.md"}
    assert merged["canonical"] == "b.md"


def test_merge_unions_tags_and_takes_max_confidence():
    entries = [
        _e("a.md", "x", "body a", ses="ses_1", tags=["haddock3"], conf=0.6),
        _e("b.md", "y", "body b", ses="ses_2", tags=["hsc70"], conf=0.8),
    ]
    merged = merge_entries(entries)
    assert set(merged["meta"]["tags"]) == {"haddock3", "hsc70"}
    assert merged["meta"]["confidence"] == 0.8
