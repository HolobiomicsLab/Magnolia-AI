"""Single staging writer shared by the dialogue and tool-event paths."""

from pathlib import Path

import yaml
import pytest

from compchem_memory.storage import ensure_project_store


@pytest.fixture
def store(tmp_path):
    return ensure_project_store(str(tmp_path))


def test_save_candidate_writes_frontmatter_and_body(store):
    from compchem_memory.staging_io import save_candidate

    path = save_candidate(
        store,
        {"title": "Contact finding", "content": "F2 near R272", "type": "scientific_finding",
         "tools": ["haddock3"], "tags": ["hsc70"], "confidence": 0.7},
        source="opencode_distill",
    )
    p = Path(path)
    assert p.exists()
    text = p.read_text()
    meta = yaml.safe_load(text.split("---")[1])
    assert meta["source"] == "opencode_distill"
    assert meta["type"] == "scientific_finding"
    assert meta["title"] == "Contact finding"
    assert meta["tools"] == ["haddock3"]
    assert meta["confidence"] == 0.7
    assert meta["observation_count"] == 1
    assert "F2 near R272" in text


def test_save_candidate_includes_provenance_when_given(store):
    from compchem_memory.staging_io import save_candidate

    path = save_candidate(
        store, {"title": "t", "content": "c"},
        source="opencode_distill", opencode_session_id="ses_prov",
    )
    meta = yaml.safe_load(Path(path).read_text().split("---")[1])
    assert meta["opencode_session_id"] == "ses_prov"


def test_save_candidate_omits_provenance_key_when_absent(store):
    from compchem_memory.staging_io import save_candidate

    path = save_candidate(store, {"title": "t", "content": "c"}, source="auto_extraction")
    meta = yaml.safe_load(Path(path).read_text().split("---")[1])
    assert "opencode_session_id" not in meta
    assert meta["source"] == "auto_extraction"
