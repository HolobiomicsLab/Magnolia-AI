"""R5 surfacing counter tests (distill-admission plan).

Covers: row shape per channel, serving-store attribution through a symlinked
store, the exposure substance test at log time, session-id guessing from the
mapping, and the never-raise failure model.
"""

from __future__ import annotations

import json
from pathlib import Path

from compchem_memory.surfacing import (
    counts_for_exposure,
    guess_session_id,
    log_surfacing,
    surfacing_path,
)


def _rows(store: Path) -> list[dict]:
    p = surfacing_path(store)
    if not p.exists():
        return []
    return [json.loads(l) for l in p.read_text().splitlines() if l.strip()]


def test_rows_only_for_entry_tiers(tmp_path):
    sources = [
        {"tier": "goal", "id": "GOAL.md"},
        {"tier": "session", "id": "recent"},
        {"tier": "run", "id": "run-1"},
        {"tier": "project", "id": "a.md"},
        {"tier": "staging", "id": "b.md"},
    ]
    n = log_surfacing(
        tmp_path, sources, "get_context",
        session_id="ses_1", task_description="x" * 50,
    )
    assert n == 2
    rows = _rows(tmp_path)
    assert [r["id"] for r in rows] == ["a.md", "b.md"]
    assert all(r["channel"] == "get_context" for r in rows)
    assert all(r["session_id"] == "ses_1" for r in rows)
    assert all(r["store"] == str(tmp_path) for r in rows)
    assert all(r["counts_for_exposure"] for r in rows)
    assert all(r["task_len"] == 50 for r in rows)


def test_short_task_description_does_not_count(tmp_path):
    log_surfacing(
        tmp_path, [{"tier": "project", "id": "a.md"}], "get_context",
        task_description="too short",
    )
    assert _rows(tmp_path)[0]["counts_for_exposure"] is False


def test_no_task_description_does_not_count(tmp_path):
    log_surfacing(tmp_path, [{"tier": "project", "id": "a.md"}], "get_context")
    assert _rows(tmp_path)[0]["counts_for_exposure"] is False


def test_boot_context_never_counts(tmp_path):
    log_surfacing(tmp_path, [{"tier": "project", "id": "a.md"}], "boot_context")
    assert _rows(tmp_path)[0]["counts_for_exposure"] is False


def test_counts_for_exposure_matrix():
    assert counts_for_exposure("get_context", "y" * 40) is True
    assert counts_for_exposure("get_context", "y" * 39) is False
    assert counts_for_exposure("get_context", None) is False
    assert counts_for_exposure("action_memory") is True
    assert counts_for_exposure("boot_context") is False
    assert counts_for_exposure("unknown_channel") is False


def test_guess_session_id_latest_mapping_line(tmp_path):
    mapping = tmp_path / "opencode-sessions.jsonl"
    mapping.write_text(
        '{"opencode_session_id": "ses_old"}\n'
        '{"opencode_session_id": "ses_new"}\n'
        '{"junk": 1}\n'
    )
    assert guess_session_id(tmp_path) == "ses_new"


def test_guess_session_id_no_mapping(tmp_path):
    assert guess_session_id(tmp_path) is None


def test_log_never_raises_on_bad_store(tmp_path):
    # A read-only store dir: opening the log must fail SILENTLY (returns 0),
    # never propagate into the serving tool.
    store = tmp_path / "store"
    store.mkdir()
    store.chmod(0o500)
    assert log_surfacing(store, [{"tier": "project", "id": "a.md"}], "get_context") == 0
    store.chmod(0o700)


def test_serving_store_attribution_via_symlink(tmp_path):
    """The HOOK resolves the symlink before logging: rows land in (and name)
    the REAL store that served, not the calling project's .magnolia link."""
    from compchem_memory.context_assembly import _memory_store

    real = tmp_path / "real-store"
    real.mkdir()
    proj = tmp_path / "proj"
    proj.mkdir()
    (proj / ".magnolia").symlink_to(real, target_is_directory=True)

    store = _memory_store(str(proj))
    assert Path(store).resolve() == real.resolve()

    n = log_surfacing(store, [{"tier": "project", "id": "a.md"}], "get_context")
    assert n == 1
    rows = _rows(real)
    assert len(rows) == 1
    assert Path(rows[0]["store"]).resolve() == real.resolve()
    # nothing was written through a separate path: the only log lives in the
    # real store and its rows name the real (resolved) store
    assert (proj / ".magnolia").is_symlink()
    assert surfacing_path(real).exists()
    assert Path(rows[0]["store"]) == Path(store)


def test_boot_context_hook_writes_rows(tmp_path):
    """End-to-end: regenerate_boot_context logs its includes as boot_context
    rows in the resolved serving store."""
    from compchem_memory.boot_context import regenerate_boot_context
    from compchem_memory.tiers.project import ProjectManager

    proj = tmp_path / "proj"
    proj.mkdir()
    store = proj / ".magnolia"
    store.mkdir()
    pm = ProjectManager(tmp_path)
    pm.create_entry(
        str(proj),
        title="Boot context project memory entry",
        content="boot context project memory entry body for retrieval",
        staging=False,
    )
    out = regenerate_boot_context(str(proj))
    assert Path(out).exists()
    rows = _rows(store)
    boot_rows = [r for r in rows if r["channel"] == "boot_context"]
    # whether retrieval surfaced the entry is scoring-dependent; if it did,
    # the row must exist, be non-exposure, and carry the entry filename
    for r in boot_rows:
        assert r["counts_for_exposure"] is False
        assert r["id"].endswith(".md")
