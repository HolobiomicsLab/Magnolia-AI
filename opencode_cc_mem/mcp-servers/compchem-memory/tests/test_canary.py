"""D3 canary tests: probe distillation, freeze flag, and the promotion halt."""

import yaml
import pytest

from compchem_memory import canary
from compchem_memory.tiers.project import ProjectManager
from compchem_memory.storage import ensure_project_store


@pytest.fixture
def project_dir(tmp_path):
    pd = tmp_path / "proj"
    ensure_project_store(str(pd))
    return pd


def _make_staging_entry(pd, name, frontmatter):
    p = pd / ".magnolia" / "staging" / f"{name}.md"
    fm = yaml.safe_dump(frontmatter, default_flow_style=False)
    p.write_text(f"---\n{fm}---\n\nBody.\n")


def test_canary_pass_clears_flag(project_dir):
    def good_distiller(transcript):
        return [{"title": "haddock3 docking run summary", "type": "success_pattern"}]

    report = canary.run_canary(str(project_dir), distiller=good_distiller)
    assert report["status"] == "pass"
    assert not canary.is_frozen(str(project_dir))


def test_canary_empty_result_writes_freeze_flag(project_dir):
    report = canary.run_canary(str(project_dir), distiller=lambda t: [])
    assert report["status"] == "fail"
    assert canary.flag_path(str(project_dir)).exists()
    assert canary.is_frozen(str(project_dir))


def test_canary_llm_failure_writes_freeze_flag(project_dir):
    report = canary.run_canary(str(project_dir), distiller=lambda t: None)
    assert report["status"] == "fail"
    assert "LLM failure" in report.get("reason", "")
    assert canary.is_frozen(str(project_dir))


def test_canary_missing_expected_topic_fails(project_dir):
    report = canary.run_canary(
        str(project_dir),
        distiller=lambda t: [{"title": "unrelated note"}],
        expect="haddock3",
    )
    assert report["status"] == "fail"
    assert "haddock3" in report.get("reason", "")


def test_clear_removes_flag(project_dir):
    canary.run_canary(str(project_dir), distiller=lambda t: [])
    assert canary.is_frozen(str(project_dir))
    canary.clear(str(project_dir))
    assert not canary.is_frozen(str(project_dir))


def test_auto_promotion_halted_while_frozen(project_dir):
    pm = ProjectManager(project_dir / ".magnolia")
    p = project_dir / ".magnolia" / "staging" / "20260512_120000_qualifying.md"
    fm = yaml.safe_dump(
        {
            "title": "Qualifying entry",
            "confidence": 0.9,
            "observation_count": 2,
            "observed_in_sessions": ["s1", "s2"],
            "tags": ["x"],
            "type": "note",
        },
        default_flow_style=False,
    )
    p.write_text(f"---\n{fm}---\n\nBody.\n")

    canary.run_canary(str(project_dir), distiller=lambda t: [])
    promoted = pm.auto_promote_staging(str(project_dir))
    assert promoted == [], "frozen canary must halt auto-promotion"
    assert p.exists(), "entry must stay in staging while frozen"

    canary.clear(str(project_dir))
    promoted = pm.auto_promote_staging(str(project_dir))
    assert len(promoted) == 1, "clearing the flag resumes auto-promotion"
