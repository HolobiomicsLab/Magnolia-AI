# tests/test_consolidation_apply.py
from pathlib import Path
import yaml
from compchem_memory.consolidation import apply_merge


def _write(staging, name, title, body, ses):
    fm = {"title": title, "type": "scientific_finding", "opencode_session_id": ses,
          "observed_in_sessions": [ses], "tags": [], "tools": [], "confidence": 0.6}
    p = staging / name
    p.write_text("---\n" + yaml.dump(fm) + "---\n\n" + body + "\n")
    return str(p)


def test_apply_merge_overwrites_canonical_and_removes_others(tmp_path):
    staging = tmp_path / "staging"; staging.mkdir()
    a = _write(staging, "a.md", "short", "tiny", "ses_1")
    b = _write(staging, "b.md", "long", "the much longer canonical body text here", "ses_2")
    c = _write(staging, "c.md", "mid", "middle length body", "ses_2")

    res = apply_merge([a, b, c])

    assert res["skipped"] is False
    assert res["merged"] == b
    assert Path(b).exists()
    merged_text = Path(b).read_text()
    meta = yaml.safe_load(merged_text.split("---")[1])
    assert meta["observation_count"] == 2
    assert "tiny" in merged_text
    assert not Path(a).exists()
    assert not Path(c).exists()
    assert set(res["removed"]) == {a, c}


def test_apply_merge_skips_when_fewer_than_two_sources_survive(tmp_path):
    staging = tmp_path / "staging"; staging.mkdir()
    a = _write(staging, "a.md", "x", "body", "ses_1")
    res = apply_merge([a, str(staging / "b.md")])
    assert res["skipped"] is True
    assert res["merged"] is None
    assert Path(a).exists()
