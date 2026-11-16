"""Issue #22: memory_confirm / bump_observation_count must never resolve a
staging entry by taking the first substring hit.

Old behavior: `entry_name` was matched with `entry_name in f.name`, and the
first directory-listing hit won. A shared fragment ("2026", or the empty
string) silently promoted the wrong learning into the durable project tier.
"""

import json
from pathlib import Path

import pytest
import yaml

from compchem_memory.storage import ensure_project_store
from compchem_memory.tiers.project import AmbiguousEntryError, ProjectManager

WATER = "20261022_012422_061718_Water_model_choice"
BOX = "20261022_012422_099722_Box_size"


@pytest.fixture
def project_dir(tmp_path):
    pd = tmp_path / "proj"
    ensure_project_store(str(pd))
    return pd


def _pm() -> ProjectManager:
    return ProjectManager(Path.home() / ".magnolia")


def _staging(pd: Path) -> Path:
    return pd / ".magnolia" / "staging"


def _entries(pd: Path) -> Path:
    return pd / ".magnolia" / "entries"


def _make_entry(pd: Path, name: str, title: str, body: str, entry_id: str) -> Path:
    p = _staging(pd) / f"{name}.md"
    fm = yaml.safe_dump(
        {
            "id": entry_id,
            "title": title,
            "type": "parameter_guidance",
            "tags": [],
            "observation_count": 1,
        },
        default_flow_style=False,
    )
    p.write_text(f"---\n{fm}---\n\n{body}\n")
    return p


@pytest.fixture
def two_entries(project_dir):
    water = _make_entry(
        project_dir, WATER, "Water model choice", "Use TIP3P.", "20261022_012422_061718"
    )
    box = _make_entry(
        project_dir, BOX, "Box size", "Use a 1.0 nm margin.", "20261022_012422_099722"
    )
    return water, box


def test_shared_fragment_is_ambiguous_and_promotes_nothing(project_dir, two_entries):
    water, box = two_entries
    with pytest.raises(AmbiguousEntryError) as exc:
        _pm().confirm_staging(str(project_dir), "2026")
    assert len(exc.value.candidates) == 2
    assert water.exists() and box.exists()
    assert not list(_entries(project_dir).glob("*.md"))


def test_empty_entry_name_is_rejected(project_dir, two_entries):
    with pytest.raises(ValueError, match="must not be empty"):
        _pm().confirm_staging(str(project_dir), "")
    assert not list(_entries(project_dir).glob("*.md"))


def test_exact_stem_promotes_only_that_entry(project_dir, two_entries):
    dest = _pm().confirm_staging(str(project_dir), BOX)
    assert Path(dest).name == f"{BOX}.md"
    assert (project_dir / ".magnolia" / "entries" / f"{BOX}.md").exists()
    assert (_staging(project_dir) / f"{WATER}.md").exists()


def test_exact_file_name_promotes_only_that_entry(project_dir, two_entries):
    dest = _pm().confirm_staging(str(project_dir), f"{WATER}.md")
    assert Path(dest).name == f"{WATER}.md"
    assert (_staging(project_dir) / f"{BOX}.md").exists()


def test_unique_case_insensitive_substring_still_works(project_dir, two_entries):
    """The reported 'water' vs 'Water_model' case must resolve when unique."""
    dest = _pm().confirm_staging(str(project_dir), "water")
    assert Path(dest).name == f"{WATER}.md"


def test_frontmatter_id_resolves_exactly(project_dir, two_entries):
    dest = _pm().confirm_staging(str(project_dir), "20261022_012422_099722")
    assert Path(dest).name == f"{BOX}.md"


def test_unknown_name_still_raises_file_not_found(project_dir, two_entries):
    with pytest.raises(FileNotFoundError, match="Staging entry not found"):
        _pm().confirm_staging(str(project_dir), "no-such-entry")


def test_bump_refuses_ambiguous_match(project_dir, two_entries):
    water, box = two_entries
    with pytest.raises(AmbiguousEntryError):
        _pm().bump_observation_count(str(project_dir), "2026")
    for p in (water, box):
        meta = yaml.safe_load(p.read_text().split("---")[1])
        assert meta["observation_count"] == 1


def test_bump_by_exact_name_increments_only_that_entry(project_dir, two_entries):
    water, box = two_entries
    assert _pm().bump_observation_count(str(project_dir), BOX) is True
    box_meta = yaml.safe_load(box.read_text().split("---")[1])
    water_meta = yaml.safe_load(water.read_text().split("---")[1])
    assert box_meta["observation_count"] == 2
    assert water_meta["observation_count"] == 1


def test_memory_confirm_tool_returns_ambiguous_payload(project_dir, two_entries, monkeypatch):
    from compchem_memory import server

    monkeypatch.setattr(server, "PROJECT_DIR", str(project_dir))
    confirm = getattr(server.memory_confirm, "fn", server.memory_confirm)

    out = json.loads(confirm("2026", project_dir=str(project_dir)))
    assert out["status"] == "ambiguous_entry"
    assert len(out["candidates"]) == 2
    assert not list(_entries(project_dir).glob("*.md"))
