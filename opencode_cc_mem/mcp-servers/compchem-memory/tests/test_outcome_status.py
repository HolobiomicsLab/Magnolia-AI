"""Outcome-status support in the memory server (assessment §11.5).

- memory_record_learning stamps outcome_status / work_item_id / attempt
  into entry frontmatter, and rejects invalid statuses.
- memory_get_context(final_only=True) excludes ONLY explicitly-intermediate
  entries; unstamped entries remain retrievable (backward compatible).

Run from mcp-servers/compchem-memory/:
    python3 -m pytest tests/test_outcome_status.py -q
"""

from __future__ import annotations

import uuid
from pathlib import Path

import pytest
import yaml

from compchem_memory import server
from compchem_memory.context_assembly import assemble_context


def _frontmatter(path: str) -> dict:
    text = Path(path).read_text()
    block = text.split("---", 2)[1]
    return yaml.safe_load(block)


def test_record_learning_stamps_outcome_status_and_provenance():
    result = server.memory_record_learning(
        title=f"zzyzx intermediate observation {uuid.uuid4().hex[:8]}",
        content="one attempt looked good, not yet a lesson",
        outcome_status="intermediate",
        work_item_id="wi_test123",
        attempt=2,
        project_dir=None,
    )
    import json
    payload = json.loads(result)
    assert payload["status"] in {"created", "bumped"}
    if payload["status"] == "bumped":
        pytest.skip("title collided with an existing entry")
    meta = _frontmatter(payload["path"])
    assert meta["outcome_status"] == "intermediate"
    assert meta["work_item_id"] == "wi_test123"
    assert meta["attempt"] == 2


def test_record_learning_rejects_invalid_status():
    with pytest.raises(ValueError, match="outcome_status"):
        server.memory_record_learning(
            title=f"zzyzx invalid {uuid.uuid4().hex[:8]}",
            content="bad status",
            outcome_status="final",
            project_dir=None,
        )


def test_record_learning_without_status_is_unstamped():
    import json
    result = server.memory_record_learning(
        title=f"zzyzx legacy entry {uuid.uuid4().hex[:8]}",
        content="no outcome status supplied",
        project_dir=None,
    )
    payload = json.loads(result)
    if payload["status"] == "bumped":
        pytest.skip("title collided with an existing entry")
    meta = _frontmatter(payload["path"])
    assert "outcome_status" not in meta


def _write_entry(directory: Path, name: str, title: str, body: str,
                 status: str | None, entry_type: str = "note") -> None:
    directory.mkdir(parents=True, exist_ok=True)
    fm = f"title: {title}\ntype: {entry_type}\nconfidence: 0.9\n"
    if status:
        fm += f"outcome_status: {status}\n"
    (directory / name).write_text(f"---\n{fm}---\n{body}\n")


def test_final_only_filters_only_explicit_intermediates(tmp_path):
    store = tmp_path / ".magnolia"
    _write_entry(store / "entries", "a.md", "zzyzx intermediate",
                 "zzyzx intermediate observation body", "intermediate")
    _write_entry(store / "entries", "b.md", "zzyzx completed",
                 "zzyzx completed campaign body", "completed_campaign")
    _write_entry(store / "entries", "c.md", "zzyzx legacy",
                 "zzyzx legacy body without a status stamp", None)
    _write_entry(store / "staging", "d.md", "zzyzx staging intermediate",
                 "zzyzx staging intermediate body", "intermediate",
                 entry_type="error_resolution")

    full = assemble_context("zzyzx", str(tmp_path), final_only=False)
    assert "zzyzx intermediate observation body" in full.content
    assert "zzyzx completed campaign body" in full.content
    assert "zzyzx legacy body" in full.content

    final = assemble_context("zzyzx", str(tmp_path), final_only=True)
    assert "zzyzx intermediate observation body" not in final.content
    assert "zzyzx staging intermediate body" not in final.content
    assert "zzyzx completed campaign body" in final.content
    assert "zzyzx legacy body" in final.content  # unstamped stays available


def test_get_context_exposes_final_only_flag(tmp_path):
    store = Path(tmp_path / ".magnolia")
    _write_entry(store / "entries", "a.md", "zzyzx intermediate",
                 "zzyzx intermediate observation body", "intermediate")
    _write_entry(store / "entries", "b.md", "zzyzx validated",
                 "zzyzx human validated body", "human_validated")
    import json
    result = json.loads(server.memory_get_context("zzyzx", project_dir=str(tmp_path), final_only=True))
    assert "zzyzx human validated body" in result["content"]
    assert "zzyzx intermediate observation body" not in result["content"]
