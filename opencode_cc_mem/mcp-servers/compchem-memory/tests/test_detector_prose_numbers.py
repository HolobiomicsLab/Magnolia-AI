# tests/test_detector_prose_numbers.py
# Prose-number fix (2026-10-06), behind MAGNOLIA_PROSE_NUMBERS: titles that
# differ in their numbers state DIFFERENT claims and must not bump each other
# ("use s10000" vs "use s20000", "volume 42" vs "volume 95"). Dates are
# stripped before the veto so they never count. Flag off = legacy behavior.
import yaml

from compchem_memory.tiers.project import ProjectManager


def _mgr(tmp_path):
    store = tmp_path / ".magnolia"
    (store / "staging").mkdir(parents=True)
    mgr = ProjectManager(tmp_path)
    fm = {"title": "Use s10000 not s20000 for bake-off sampling",
          "type": "parameter_guidance", "opencode_session_id": "s1",
          "observed_in_sessions": ["s1"], "tags": [], "tools": [],
          "confidence": 0.6}
    (store / "staging" / "a.md").write_text(
        "---\n" + yaml.dump(fm) + "---\n\nbody\n")
    return mgr, tmp_path, store


def test_flag_on_numbers_differ_no_match(tmp_path, monkeypatch):
    mgr, pd, store = _mgr(tmp_path)
    monkeypatch.setenv("MAGNOLIA_PROSE_NUMBERS", "1")

    hit = mgr.find_similar_staging(
        str(pd), "Use s20000 not s10000 for bake-off sampling",
        [], entry_type="parameter_guidance")

    assert hit is None                       # different numbers -> different claim


def test_flag_off_legacy_still_matches(tmp_path, monkeypatch):
    mgr, pd, store = _mgr(tmp_path)
    monkeypatch.delenv("MAGNOLIA_PROSE_NUMBERS", raising=False)

    hit = mgr.find_similar_staging(
        str(pd), "Use s20000 not s10000 for bake-off sampling",
        [], entry_type="parameter_guidance")

    assert hit == "a.md"                     # legacy: digits invisible, matches


def test_flag_on_identical_numbers_still_match(tmp_path, monkeypatch):
    mgr, pd, store = _mgr(tmp_path)
    monkeypatch.setenv("MAGNOLIA_PROSE_NUMBERS", "1")

    hit = mgr.find_similar_staging(
        str(pd), "Use s10000 not s20000 for bake-off sampling",
        [], entry_type="parameter_guidance")

    assert hit == "a.md"                     # same numbers -> same claim


def test_dates_never_veto(tmp_path, monkeypatch):
    mgr, pd, store = _mgr(tmp_path)
    monkeypatch.setenv("MAGNOLIA_PROSE_NUMBERS", "1")
    fm = {"title": "bake-off verdict recall 72 percent still fail",
          "type": "scientific_finding", "opencode_session_id": "s2",
          "observed_in_sessions": ["s2"], "tags": [], "tools": [],
          "confidence": 0.6}
    (store / "staging" / "b.md").write_text(
        "---\n" + yaml.dump(fm) + "---\n\nbody\n")

    hit = mgr.find_similar_staging(
        str(pd), "bake-off verdict recall 72 percent still fail (run 2026-09-30)",
        [], entry_type="scientific_finding")

    assert hit == "b.md"                     # a trailing date must not veto
