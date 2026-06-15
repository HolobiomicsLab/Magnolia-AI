# tests/test_promotion.py
from pathlib import Path
import yaml
from compchem_memory.promotion import eligible_entries, _PROMOTION_MIN_SESSIONS


def _entry(entries_dir, name, title, body, sessions):
    fm = {"title": title, "type": "success_pattern",
          "observed_in_sessions": sessions, "confidence": 0.9}
    p = entries_dir / name
    p.write_text("---\n" + yaml.dump(fm) + "---\n\n" + body + "\n")
    return str(p)


def test_eligible_requires_min_distinct_sessions(tmp_path):
    entries = tmp_path / ".magnolia" / "entries"; entries.mkdir(parents=True)
    _entry(entries, "two.md", "two", "b", ["s1", "s2"])              # below floor (N=3)
    ok = _entry(entries, "three.md", "three", "b", ["s1", "s2", "s3"])
    (entries / "INDEX.md").write_text("index")                       # must be skipped

    got = eligible_entries(str(tmp_path / ".magnolia"))
    assert [e["path"] for e in got] == [ok]
    assert _PROMOTION_MIN_SESSIONS == 3


def test_eligible_dedups_sessions_within_entry(tmp_path):
    entries = tmp_path / ".magnolia" / "entries"; entries.mkdir(parents=True)
    _entry(entries, "dup.md", "dup", "b", ["s1", "s1", "s2"])        # only 2 distinct
    assert eligible_entries(str(tmp_path / ".magnolia")) == []
