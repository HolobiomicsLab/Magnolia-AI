"""Tests for staging dedup matching (Bug A) and lossless bump enrichment (Bug B).

Bug A: find_similar_staging matched a generic project-init note as a magnet for
every learning, because shared project-identity tags alone cleared the threshold
and entry_type was ignored.

Bug B: bump_observation_count incremented the counter but discarded the new
content, so re-observed learnings were silently lost.
"""

from pathlib import Path

import yaml
import pytest

from compchem_memory.tiers.project import ProjectManager
from compchem_memory.storage import ensure_project_store


@pytest.fixture
def project_dir(tmp_path):
    pd = tmp_path / "proj"
    ensure_project_store(str(pd))
    return pd


def _make_staging_entry(pd, name, content, frontmatter):
    p = pd / ".magnolia" / "staging" / f"{name}.md"
    fm = yaml.safe_dump(frontmatter, default_flow_style=False)
    p.write_text(f"---\n{fm}---\n\n{content}\n")
    return p


def _pm():
    return ProjectManager(Path.home() / ".magnolia")


# --- Bug A: matching ---------------------------------------------------------

def test_project_init_note_is_not_a_magnet_for_learnings(project_dir):
    """A new error_resolution that shares only project-identity tags with a
    generic project-init note must NOT match it."""
    pm = _pm()
    _make_staging_entry(
        project_dir,
        "20260601_072242_project_init",
        "Project hsc70_new created. No prior runs.",
        {
            "title": "Project hsc70_new initialized — greenfield peptide docking project using HADDOCK3",
            "type": "note",
            "tags": ["hsc70_new", "haddock3", "peptide-docking", "greenfield-project"],
            "observation_count": 1,
        },
    )
    match = pm.find_similar_staging(
        str(project_dir),
        "submit_job does verbatim rsync of working_dir — reuse carefully",
        ["hsc70_new", "haddock3"],
        entry_type="error_resolution",
    )
    assert match is None, "project-init note should not absorb an unrelated learning"


def test_genuine_duplicate_learning_matches(project_dir):
    """Two error_resolution entries with near-identical titles and same type
    should match so the second bumps the first."""
    pm = _pm()
    _make_staging_entry(
        project_dir,
        "20260601_100000_rsync",
        "submit_job rsyncs the whole working_dir.",
        {
            "title": "submit_job does verbatim rsync of working_dir",
            "type": "error_resolution",
            "tags": ["haddock3", "submit_job"],
            "observation_count": 1,
        },
    )
    match = pm.find_similar_staging(
        str(project_dir),
        "submit_job does a verbatim rsync of the working_dir directory",
        ["submit_job"],
        entry_type="error_resolution",
    )
    assert match is not None, "near-identical same-type learnings should match"


def test_shared_stopwords_alone_do_not_match(project_dir):
    """Two same-type entries that share only common stopwords ('to', 'before')
    in their titles are different learnings and must not match."""
    pm = _pm()
    _make_staging_entry(
        project_dir,
        "20260601_092242_p2rank_cleanup",
        "Kill stray p2rank processes before re-running.",
        {
            "title": "P2rank process cleanup required before re-run to avoid resource contention",
            "type": "error_resolution",
            "tags": ["p2rank", "process-management"],
            "observation_count": 1,
        },
    )
    match = pm.find_similar_staging(
        str(project_dir),
        "submit_job output collision — always clean output/ before resubmitting to same dir",
        ["submit_job", "haddock3"],
        entry_type="error_resolution",
    )
    assert match is None, "shared stopwords ('to', 'before') must not trigger a match"


def test_same_title_different_entry_type_does_not_match(project_dir):
    """A note and an error_resolution with the same title are different kinds of
    knowledge and must not be merged."""
    pm = _pm()
    _make_staging_entry(
        project_dir,
        "20260601_110000_note",
        "Body.",
        {
            "title": "haddock3 docking workflow",
            "type": "note",
            "tags": ["haddock3"],
            "observation_count": 1,
        },
    )
    match = pm.find_similar_staging(
        str(project_dir),
        "haddock3 docking workflow",
        ["haddock3"],
        entry_type="error_resolution",
    )
    assert match is None, "must not match across entry_type"


# --- Parser robustness: '---' inside a frontmatter value --------------------

def test_parse_frontmatter_tolerates_triple_dash_in_value(project_dir):
    """A '---' inside a frontmatter value (e.g. a markdown rule captured into
    description) must not be mistaken for the closing delimiter."""
    pm = _pm()
    text = (
        "---\n"
        'description: "intro\\n\\n---\\nmore text"\n'
        "title: My Real Title\n"
        "type: error_resolution\n"
        "---\n\n"
        "Body.\n"
    )
    meta = pm._parse_frontmatter(text)
    assert meta.get("title") == "My Real Title"
    assert meta.get("type") == "error_resolution"


def test_created_entry_with_dash_rule_is_matchable(project_dir):
    """End-to-end: an entry whose content contains a '---' rule must still be
    found by find_similar_staging (i.e. its frontmatter stays parseable)."""
    pm = _pm()
    body = "## Heading\n\nsome text\n\n---\n\nmore"
    pm.create_entry(
        str(project_dir), "submit_job rsync behavior", body,
        tags=["submit_job"], staging=True, entry_type="error_resolution",
    )
    match = pm.find_similar_staging(
        str(project_dir), "submit_job rsync behavior", ["submit_job"],
        entry_type="error_resolution",
    )
    assert match is not None, "entry with a '---' rule must remain matchable"


# --- Bug B: lossless bump ----------------------------------------------------

def test_bump_appends_new_content(project_dir):
    """Bumping an entry must preserve the original body AND append the new
    observation's content, not just increment the counter."""
    pm = _pm()
    p = _make_staging_entry(
        project_dir,
        "20260601_120000_rsync",
        "Original observation: submit_job rsyncs working_dir.",
        {
            "title": "submit_job rsync semantics",
            "type": "error_resolution",
            "tags": ["submit_job"],
            "observation_count": 1,
        },
    )
    pm.bump_observation_count(
        str(project_dir),
        p.name,
        session_id="2026-06-01_120500",
        content="New observation: reusing a dirty directory copies stale output/.",
    )
    body = p.read_text()
    assert "Original observation: submit_job rsyncs working_dir." in body
    assert "reusing a dirty directory copies stale output/" in body
    meta = pm._parse_frontmatter(body)
    assert meta.get("observation_count") == 2
