"""R9 parked flag: a staging entry marked `parked: true` stays in staging but is
excluded everywhere entries are discovered:

- consolidation pool (_load_findings) — phase 1
- search_staging — phase 1
- auto_promote_staging (auto-confirm freezer) — phase 1
- retrieval staging-warnings exposure — phase 2
- scan_memory_headers reports the flag — phase 2
- ProjectManager.set_parked + memory_park/memory_unpark tools — phase 2

Design driver: literature-derived staging entries parked by user decision
(2026-09-30) pending the literature-xiulian interaction design; only a new
inbox request lifts the park.
"""

import json
from pathlib import Path

import yaml
import pytest

from compchem_memory import retrieval
from compchem_memory.consolidation import _load_findings
from compchem_memory.tiers.project import ProjectManager


@pytest.fixture(autouse=True)
def _no_llm(monkeypatch):
    # Keep retrieval deterministic: exercise the heuristic path, not LLM rerank.
    monkeypatch.setattr(retrieval, "is_llm_available", lambda: False)


def _write_staging(project_dir: Path, name: str, frontmatter: dict, body: str = "Body text."):
    staging = project_dir / ".magnolia" / "staging"
    staging.mkdir(parents=True, exist_ok=True)
    fm = yaml.safe_dump(frontmatter, default_flow_style=False)
    (staging / f"{name}.md").write_text(f"---\n{fm}---\n\n{body}\n")


def _fm(title, etype="error_resolution", parked=None, **extra):
    fm = {
        "title": title,
        "type": etype,
        "tags": ["contact"],
        "tools": [],
        "confidence": 0.7,
        "observation_count": 1,
        "last_verified": "2026-06-02",
        "description": title,
    }
    if parked is not None:
        fm["parked"] = parked
    fm.update(extra)
    return fm


TASK = "interpret the contact analysis labels for this docking pose"


# ── scan_memory_headers reports the flag ─────────────────────────────────────


def test_scan_headers_parked_field(tmp_path):
    _write_staging(tmp_path, "a", _fm("Contact analysis labels are not mechanisms", parked=True))
    _write_staging(tmp_path, "b", _fm("Contact analysis label caveat"))
    headers = retrieval.scan_memory_headers(tmp_path / ".magnolia" / "staging")
    by_name = {h["filename"]: h for h in headers}
    assert by_name["a.md"]["parked"] is True
    assert by_name["b.md"]["parked"] is False


def test_scan_headers_parked_defaults_false(tmp_path):
    _write_staging(tmp_path, "plain", _fm("Contact analysis note"))
    headers = retrieval.scan_memory_headers(tmp_path / ".magnolia" / "staging")
    assert headers[0]["parked"] is False


# ── set_parked ───────────────────────────────────────────────────────────────


def test_set_parked_sets_and_clears(tmp_path):
    _write_staging(tmp_path, "w", _fm("Contact analysis labels are not mechanisms"))
    mgr = ProjectManager(global_base=tmp_path / "global")

    name = mgr.set_parked(str(tmp_path), "w", True)
    assert name == "w.md"
    text = (tmp_path / ".magnolia" / "staging" / "w.md").read_text()
    assert "parked: true" in text

    name = mgr.set_parked(str(tmp_path), "w", False)
    assert name == "w.md"
    text = (tmp_path / ".magnolia" / "staging" / "w.md").read_text()
    assert "parked: false" in text


def test_set_parked_matches_partial_name(tmp_path):
    _write_staging(tmp_path, "20260904_180705_734200_long_title", _fm("Binder design prompt notes"))
    mgr = ProjectManager(global_base=tmp_path / "global")
    name = mgr.set_parked(str(tmp_path), "20260904_180705_734200", True)
    assert name == "20260904_180705_734200_long_title.md"


def test_set_parked_returns_none_when_missing(tmp_path):
    mgr = ProjectManager(global_base=tmp_path / "global")
    assert mgr.set_parked(str(tmp_path), "no-such-entry", True) is None


# ── phase 1 exclusions (regression cover) ────────────────────────────────────


def test_load_findings_skips_parked(tmp_path):
    _write_staging(tmp_path, "a", _fm("Contact analysis labels are not mechanisms",
                                     etype="scientific_finding"))
    _write_staging(tmp_path, "b", _fm("Contact analysis label pitfall", parked=True,
                                      etype="scientific_finding"))
    findings = _load_findings(tmp_path / ".magnolia" / "staging")
    ids = [Path(f["id"]).name for f in findings]
    assert "a.md" in ids
    assert "b.md" not in ids


def test_search_staging_skips_parked(tmp_path):
    _write_staging(tmp_path, "a", _fm("Contact analysis labels are not mechanisms"))
    _write_staging(tmp_path, "b", _fm("Contact analysis label pitfall", parked=True))
    mgr = ProjectManager(global_base=tmp_path / "global")
    hits = mgr.search_staging(str(tmp_path), keyword="contact analysis")
    names = [h["name"] for h in hits]
    assert "a.md" in names
    assert "b.md" not in names


def test_auto_promote_skips_parked(tmp_path):
    _write_staging(
        tmp_path, "w",
        _fm("Contact analysis labels are not mechanisms", parked=True,
            confidence=0.9, observation_count=3,
            observed_in_sessions=["s1", "s2"]),
    )
    mgr = ProjectManager(global_base=tmp_path / "global")
    promoted = mgr.auto_promote_staging(str(tmp_path))
    assert promoted == []
    # Still in staging — parked freezes, never deletes.
    assert (tmp_path / ".magnolia" / "staging" / "w.md").exists()


# ── phase 2: retrieval exposure ──────────────────────────────────────────────


def test_select_relevant_excludes_parked_staging_warning(tmp_path):
    # tmp_path doubles as the memory-store root here: entries/ + staging/.
    (tmp_path / "staging").mkdir(parents=True)
    fm = yaml.safe_dump(_fm("Contact analysis labels are not mechanisms", parked=True))
    (tmp_path / "staging" / "parked.md").write_text(f"---\n{fm}---\n\nBody.\n")
    fm = yaml.safe_dump(_fm("Contact analysis labels caveat for docking poses"))
    (tmp_path / "staging" / "live.md").write_text(f"---\n{fm}---\n\nBody.\n")

    results = retrieval.select_relevant_entries(TASK, str(tmp_path))
    titles = [r["title"] for r in results]
    assert any("labels caveat" in t for t in titles), "unparked warning must surface"
    assert not any("not mechanisms" in t for t in titles), "parked warning must not surface"


def test_parked_warning_surfaces_again_after_unpark(tmp_path):
    (tmp_path / "staging").mkdir(parents=True)
    fm = yaml.safe_dump(_fm("Contact analysis labels are not mechanisms", parked=False))
    (tmp_path / "staging" / "w.md").write_text(f"---\n{fm}---\n\nBody.\n")
    results = retrieval.select_relevant_entries(TASK, str(tmp_path))
    assert any("not mechanisms" in r["title"] for r in results)


# ── phase 2: MCP tools ───────────────────────────────────────────────────────


def _tool(fn):
    return getattr(fn, "fn", fn)


def test_memory_park_unpark_tools(tmp_path, monkeypatch):
    from compchem_memory import server

    _write_staging(tmp_path, "w", _fm("Contact analysis labels are not mechanisms"))
    monkeypatch.setattr(server, "PROJECT_DIR", str(tmp_path))

    park = _tool(server.memory_park)
    unpark = _tool(server.memory_unpark)

    out = json.loads(park(entry_name="w", project_dir=str(tmp_path)))
    assert out == {"parked": True, "entry": "w.md"}
    text = (tmp_path / ".magnolia" / "staging" / "w.md").read_text()
    assert "parked: true" in text

    out = json.loads(unpark(entry_name="w", project_dir=str(tmp_path)))
    assert out == {"parked": False, "entry": "w.md"}
    text = (tmp_path / ".magnolia" / "staging" / "w.md").read_text()
    assert "parked: false" in text


def test_memory_park_missing_entry_reports_error(tmp_path, monkeypatch):
    from compchem_memory import server

    monkeypatch.setattr(server, "PROJECT_DIR", str(tmp_path))
    park = _tool(server.memory_park)
    out = json.loads(park(entry_name="ghost", project_dir=str(tmp_path)))
    assert "error" in out
