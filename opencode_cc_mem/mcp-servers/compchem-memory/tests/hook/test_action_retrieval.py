"""Action-time retrieval, noise controls, telemetry, and exposure tests."""

from __future__ import annotations

import fcntl
import json
import threading
import time
from pathlib import Path

from compchem_memory import quick_search
from compchem_memory.hook.action_retrieval import (
    ActionNote,
    _cache_key,
    args_to_query,
    commit_exposure,
    discard_note,
    on_post_tool,
    probe_application,
)
from compchem_memory.hook.events import HookEvent, ToolCall
from compchem_memory.hook.project import ProjectPaths
from compchem_memory.hook.state import SessionState


def _paths(tmp_path: Path, name: str = "project") -> ProjectPaths:
    project_dir = tmp_path / name
    store = project_dir / ".magnolia"
    store.mkdir(parents=True)
    return ProjectPaths(tmp_path, project_dir, store, name, False)


def _event(
    paths: ProjectPaths,
    *,
    session_id: str = "s1",
    call_id: str = "c1",
    raw_name: str = "compchem-tools_run_shell",
    server: str | None = "compchem-tools",
    name: str = "run_shell",
    tool_input: dict | None = None,
    output: str = "completed successfully",
) -> HookEvent:
    return HookEvent(
        harness="claude",
        event="post-tool",
        session_id=session_id,
        cwd=str(paths.project_dir),
        tool=ToolCall(
            name=name,
            server=server,
            raw_name=raw_name,
            call_id=call_id,
            input=tool_input or {"cmd": "haddock3 run workflow"},
            output=output,
        ),
    )


def _hit(
    store: Path,
    filename: str,
    *,
    score: int = 4,
    tier: str = "project",
    title: str = "Haddock timeout recovery",
    entry_type: str = "error_resolution",
    provisional: bool = False,
) -> dict:
    directory = "staging" if tier == "staging" else "entries"
    path = store / directory / filename
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("entry\n", encoding="utf-8")
    return {
        "title": title,
        "type": entry_type,
        "tier": tier,
        "score": score,
        "provisional": provisional,
        "confidence": 0.9,
        "path": str(path),
        "gist": f"Gist for {title}",
    }


def _rows(store: Path) -> list[dict]:
    path = store / "action-retrieval.jsonl"
    if not path.exists():
        return []
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()]


def _call(
    event: HookEvent,
    paths: ProjectPaths | None,
    *,
    seconds: float = 10.0,
) -> ActionNote | None:
    return on_post_tool(event, paths, time.monotonic() + seconds)


def _deliver(event: HookEvent, paths: ProjectPaths) -> ActionNote | None:
    """Retrieve, then commit as the CLI does once the client emitted the note."""
    note = _call(event, paths)
    if note is not None:
        commit_exposure(note, event.session_id)
    return note


def test_args_to_query_depth_and_cut() -> None:
    args = {
        "top": "alpha",
        "listed": ["beta"],
        "nested": {"two": "gamma", "too_deep": {"three": "hidden"}},
        "long": "x" * 2000,
        "empty": "",
    }

    assert args_to_query(args) == "alpha beta gamma"
    assert args_to_query(["z" * 399, "tail"]) == "z" * 399 + " "


def test_action_retrieval_injects_block(tmp_path, monkeypatch) -> None:
    paths = _paths(tmp_path)
    hits = [
        _hit(paths.store, "first.md", score=5, title="First learning"),
        _hit(paths.store, "second.md", score=4, title="Second learning"),
        _hit(
            paths.store,
            "third.md",
            score=3,
            tier="staging",
            title="Third learning",
            provisional=True,
        ),
    ]
    monkeypatch.setattr(quick_search, "search", lambda query, project_dir, k=5: hits)

    note = _deliver(_event(paths), paths)

    assert note is not None
    assert note.text == (
        "\n[Magnolia · action-memory] 3 past learning(s) matched this action "
        "(tool: run_shell). Scan these BEFORE trusting defaults — one may hold the exact "
        "pitfall or parameter for this step:\n"
        f"- [project/error_resolution] First learning — Gist for First learning\n  {hits[0]['path']}\n"
        f"- [project/error_resolution] Second learning — Gist for Second learning\n  {hits[1]['path']}\n"
        f"- [staging/error_resolution [unconfirmed]] Third learning — Gist for Third learning\n  {hits[2]['path']}\n"
        "(read-only auto-injection; opt out MAGNOLIA_ACTION_RETRIEVE=0)\n\n"
    )
    rows = _rows(paths.store)
    assert len(rows) == 2
    assert rows[0] | {"ts": "ignored"} == {
        "ts": "ignored",
        "sessionID": "s1",
        "tool": "compchem-tools_run_shell",
        "callID": "c1",
        "outcome": "ok",
        "injected": 3,
    }
    assert rows[1]["hits"] == 3
    assert {row["tool"] for row in rows} == {"compchem-tools_run_shell"}
    assert rows[1]["titles"] == ["First learning", "Second learning", "Third learning"]
    assert rows[1]["entries"] == [
        {
            "path": hit["path"],
            "tier": hit["tier"],
            "score": hit["score"],
            "confidence": hit["confidence"],
            "provisional": hit["provisional"],
        }
        for hit in hits[:3]
    ]


def test_action_retrieval_latch_and_budget(tmp_path, monkeypatch) -> None:
    paths = _paths(tmp_path)
    fixed = _hit(paths.store, "fixed.md")
    current = [fixed]
    monkeypatch.setattr(quick_search, "search", lambda query, project_dir, k=5: current)

    assert _deliver(_event(paths, call_id="l1"), paths) is not None
    assert _deliver(_event(paths, call_id="l2"), paths) is not None
    assert _deliver(_event(paths, call_id="l3"), paths) is None
    assert _deliver(_event(paths, call_id="l4"), paths) is None

    for index in range(6):
        current[:] = [_hit(paths.store, f"fresh-{index}.md", title=f"Fresh {index}")]
        assert _deliver(
            _event(paths, call_id=f"f{index}", tool_input={"cmd": f"command {index}"}), paths
        ) is not None
    current[:] = [_hit(paths.store, "ninth.md", title="Ninth")]
    assert _deliver(_event(paths, call_id="b1", tool_input={"cmd": "budget one"}), paths) is None
    assert _deliver(_event(paths, call_id="b2", tool_input={"cmd": "budget two"}), paths) is None

    rows = _rows(paths.store)
    assert sum(row.get("skipped") == "latch" for row in rows) == 1
    assert sum(row.get("skipped") == "budget" for row in rows) == 1
    assert sum(row.get("injected", 0) > 0 for row in rows) == 8


def test_dropped_note_spends_nothing_and_is_logged_as_not_injected(tmp_path, monkeypatch) -> None:
    paths = _paths(tmp_path)
    hit = _hit(paths.store, "dropped.md")
    monkeypatch.setattr(quick_search, "search", lambda query, project_dir, k=5: [hit])

    dropped = _call(_event(paths, call_id="d1"), paths)
    assert dropped is not None
    discard_note(dropped)

    with SessionState.open(paths.store, "claude", "s1") as state:
        assert state.data.get("injections", 0) == 0
        assert state.data.get("entry_counts", {}) == {}
        assert state.data.get("pending_application", []) == []
    assert [row["injected"] for row in _rows(paths.store)] == [0]
    assert not (paths.store / "surfacing.jsonl").exists()

    for call_id in ("d2", "d3"):
        assert _deliver(_event(paths, call_id=call_id), paths) is not None
    assert _call(_event(paths, call_id="d4"), paths) is None
    rows = _rows(paths.store)
    assert [row.get("injected") for row in rows if "injected" in row] == [0, 1, 1, 0]
    assert sum("hits" in row for row in rows) == 2


def test_action_retrieval_zero_hits(tmp_path, monkeypatch) -> None:
    paths = _paths(tmp_path)
    monkeypatch.setattr(quick_search, "search", lambda query, project_dir, k=5: [])

    assert _call(_event(paths), paths) is None
    assert [row["injected"] for row in _rows(paths.store)] == [0]


def test_action_cache_evicts_oldest_after_50(tmp_path, monkeypatch) -> None:
    paths = _paths(tmp_path)
    calls = []
    monkeypatch.setattr(
        quick_search,
        "search",
        lambda query, project_dir, k=5: calls.append(query) or [],
    )

    for index in range(51):
        _call(_event(paths, call_id=str(index), tool_input={"cmd": f"unique {index}"}), paths)

    with SessionState.open(paths.store, "claude", "s1") as state:
        cache = state.data["query_cache"]
    assert len(cache) == 50
    assert _cache_key("run_shell", {"cmd": "unique 0"}) not in cache
    assert _cache_key("run_shell", {"cmd": "unique 1"}) in cache
    assert len(calls) == 51


def test_action_state_holds_no_argument_text(tmp_path, monkeypatch) -> None:
    paths = _paths(tmp_path)
    queries = []
    monkeypatch.setattr(
        quick_search, "search", lambda query, project_dir, k=5: queries.append(query) or [],
    )
    secret = "hunter2-fabricated-pw"
    tool_input = {"cmd": "haddock3 run workflow", "env": {"password": secret}}

    _call(_event(paths, tool_input=tool_input), paths)
    _call(_event(paths, call_id="c2", tool_input=tool_input), paths)

    assert len(queries) == 1
    state_text = "".join(p.read_text(encoding="utf-8") for p in (paths.store / "hook-state").glob("*.json"))
    assert state_text and secret not in state_text and "haddock3" not in state_text
    assert all(secret not in p.read_text(encoding="utf-8")
               for p in paths.store.rglob("*") if p.is_file())


def test_action_retrieval_ignores_non_action_tools(tmp_path, monkeypatch) -> None:
    paths = _paths(tmp_path)
    called = False

    def search(*args, **kwargs):
        nonlocal called
        called = True
        return []

    monkeypatch.setattr(quick_search, "search", search)
    event = _event(paths, raw_name="Read", server=None, name="read")

    assert _call(event, paths) is None
    assert not called
    assert not (paths.store / "action-retrieval.jsonl").exists()


def test_action_retrieval_without_project_is_silent(tmp_path, monkeypatch) -> None:
    paths = _paths(tmp_path)
    monkeypatch.setattr(quick_search, "search", lambda *args, **kwargs: (_ for _ in ()).throw(AssertionError))

    assert _call(_event(paths), None) is None
    assert not (paths.store / "action-retrieval.jsonl").exists()


def test_action_retrieval_kill_switch(tmp_path, monkeypatch) -> None:
    paths = _paths(tmp_path)
    monkeypatch.setenv("MAGNOLIA_ACTION_RETRIEVE", "0")
    monkeypatch.setattr(quick_search, "search", lambda *args, **kwargs: (_ for _ in ()).throw(AssertionError))

    assert _call(_event(paths), paths) is None
    assert not (paths.store / "action-retrieval.jsonl").exists()


def test_action_retrieval_timeout_skips(tmp_path, monkeypatch) -> None:
    paths = _paths(tmp_path)

    def slow_search(query, project_dir, k=5):
        time.sleep(3)
        return []

    monkeypatch.setattr(quick_search, "search", slow_search)
    started = time.monotonic()

    assert _call(_event(paths), paths) is None

    assert time.monotonic() - started < 2.7
    rows = _rows(paths.store)
    assert any(row.get("skipped") == "timeout" for row in rows)
    assert any(row.get("injected") == 0 for row in rows)


def test_action_retrieval_waits_only_for_short_state_sections(tmp_path, monkeypatch) -> None:
    paths = _paths(tmp_path)
    hit = _hit(paths.store, "short-lock.md")
    monkeypatch.setattr(quick_search, "search", lambda query, project_dir, k=5: [hit])
    locked = threading.Event()

    def hold_state() -> None:
        with SessionState.open(paths.store, "claude", "s1"):
            locked.set()
            time.sleep(0.3)

    holder = threading.Thread(target=hold_state)
    holder.start()
    assert locked.wait(timeout=1)
    started = time.monotonic()

    note = _call(_event(paths), paths)

    elapsed = time.monotonic() - started
    holder.join(timeout=1)
    assert note is not None
    assert elapsed < 2
    assert not holder.is_alive()


def test_action_retrieval_does_not_hold_state_lock_while_searching(
    tmp_path, monkeypatch
) -> None:
    paths = _paths(tmp_path)
    hit = _hit(paths.store, "search-unlocked.md")
    searched = threading.Event()

    def search_without_state_lock(query, project_dir, k=5):
        lock_path = paths.store / "hook-state" / "claude-s1.json.lock"
        with open(lock_path, "a+", encoding="utf-8") as lock_file:
            fcntl.flock(lock_file.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            fcntl.flock(lock_file.fileno(), fcntl.LOCK_UN)
        searched.set()
        return [hit]

    monkeypatch.setattr(quick_search, "search", search_without_state_lock)

    assert _call(_event(paths), paths) is not None
    assert searched.is_set()


def test_probe_application_positive_and_negative(tmp_path, monkeypatch) -> None:
    paths = _paths(tmp_path)
    first = _hit(paths.store, "alpha_beta.md", title="Alpha catalyst protocol")
    second = _hit(paths.store, "gamma_delta.md", title="Gamma solvent warning")
    current = [first]
    monkeypatch.setattr(quick_search, "search", lambda query, project_dir, k=5: current)
    assert _deliver(_event(paths, call_id="positive"), paths) is not None
    current = [second]
    assert _deliver(
        _event(paths, call_id="negative", tool_input={"cmd": "different query"}), paths
    ) is not None

    probe_application(
        paths,
        "claude",
        "s1",
        "I followed the alpha catalyst recommendation.",
    )

    applications = [row for row in _rows(paths.store) if row.get("event") == "application"]
    assert [row["applied"] for row in applications] == [True, False]
    assert applications[0]["matched"] == ["alpha", "catalyst"]
    with SessionState.open(paths.store, "claude", "s1") as state:
        assert state.data["pending_application"] == []


# Copied from collegium@e64e411:src/collegium/harness/magnolia/exposure.py:99-141.
def _collegium_recognized(row: dict | None) -> bool:
    if row is None:
        return False
    return (
        isinstance(row.get("ts"), str)
        and row.get("channel") in {"get_context", "boot_context", "action_memory"}
        and isinstance(row.get("store"), str)
        and (isinstance(row.get("session_id"), str) or row.get("session_id") is None)
        and row.get("tier") in {"project", "staging"}
        and isinstance(row.get("id"), str)
        and isinstance(row.get("counts_for_exposure"), bool)
        and isinstance(row.get("task_len"), int)
    )


def test_action_surfacing_rows_keep_collegium_shape(tmp_path, monkeypatch) -> None:
    paths = _paths(tmp_path, "caller")
    serving_project = _paths(tmp_path, "serving-project")
    serving_staging = _paths(tmp_path, "serving-staging")
    # Fixture follows collegium@e64e411:tests/test_harness_magnolia_exposure.py:35-89.
    hits = [
        _hit(
            serving_staging.store,
            "collegium_card_1.md",
            tier="staging",
            title="Collegium card",
            provisional=True,
        ),
        _hit(serving_project.store, "project_note.md", title="Project note"),
    ]
    monkeypatch.setattr(quick_search, "search", lambda query, project_dir, k=5: hits)

    note = _call(_event(paths), paths)
    assert note is not None
    commit_exposure(note, "s1")

    staging_row = json.loads(
        (serving_staging.store / "surfacing.jsonl").read_text(encoding="utf-8").strip()
    )
    project_row = json.loads(
        (serving_project.store / "surfacing.jsonl").read_text(encoding="utf-8").strip()
    )
    assert staging_row["id"] == "collegium_card_1.md"
    assert project_row["id"] == "project_note.md"
    assert staging_row["store"] == str(serving_staging.store)
    assert project_row["store"] == str(serving_project.store)
    assert _collegium_recognized(staging_row)
    assert _collegium_recognized(project_row)


def test_no_exposure_before_commit(tmp_path, monkeypatch) -> None:
    paths = _paths(tmp_path)
    hit = _hit(paths.store, "not-yet-exposed.md")
    monkeypatch.setattr(quick_search, "search", lambda query, project_dir, k=5: [hit])

    note = _call(_event(paths), paths)

    assert note is not None
    assert not (paths.store / "surfacing.jsonl").exists()


def test_overlapping_hooks_respect_the_per_entry_cap(tmp_path, monkeypatch) -> None:
    """Parallel post-tool hooks of one session reserve slots at selection (N1)."""
    paths = _paths(tmp_path)
    hit = _hit(paths.store, "shared.md")
    monkeypatch.setattr(quick_search, "search", lambda query, project_dir, k=5: [hit])

    notes = [
        _call(_event(paths, call_id=f"p{index}", tool_input={"cmd": f"run {index}"}), paths)
        for index in range(4)
    ]

    assert [note is not None for note in notes] == [True, True, False, False]
    with SessionState.open(paths.store, "claude", "s1") as state:
        assert state.data["entry_counts"] == {hit["path"]: 2}
        assert state.data["injections"] == 2


def test_overlapping_hooks_respect_the_session_budget(tmp_path, monkeypatch) -> None:
    paths = _paths(tmp_path)
    hits = [_hit(paths.store, f"e{index}.md", title=f"Entry {index}") for index in range(12)]
    current: list[dict] = []
    monkeypatch.setattr(quick_search, "search", lambda query, project_dir, k=5: list(current))

    notes = []
    for index in range(10):
        current[:] = [hits[index]]
        notes.append(
            _call(_event(paths, call_id=f"b{index}", tool_input={"cmd": f"step {index}"}), paths)
        )

    assert sum(note is not None for note in notes) == 8


def test_commit_after_reservation_counts_once(tmp_path, monkeypatch) -> None:
    paths = _paths(tmp_path)
    hit = _hit(paths.store, "once.md")
    monkeypatch.setattr(quick_search, "search", lambda query, project_dir, k=5: [hit])

    note = _deliver(_event(paths), paths)

    assert note is not None
    with SessionState.open(paths.store, "claude", "s1") as state:
        assert state.data["injections"] == 1
        assert state.data["entry_counts"] == {hit["path"]: 1}
        assert len(state.data["pending_application"]) == 1


def test_discard_releases_the_reservation_for_another_hook(tmp_path, monkeypatch) -> None:
    paths = _paths(tmp_path)
    hit = _hit(paths.store, "released.md")
    monkeypatch.setattr(quick_search, "search", lambda query, project_dir, k=5: [hit])

    first = _call(_event(paths, call_id="r1", tool_input={"cmd": "one"}), paths)
    second = _call(_event(paths, call_id="r2", tool_input={"cmd": "two"}), paths)
    assert first is not None and second is not None
    assert _call(_event(paths, call_id="r3", tool_input={"cmd": "three"}), paths) is None

    discard_note(first)

    assert _call(_event(paths, call_id="r4", tool_input={"cmd": "four"}), paths) is not None


def test_a_note_is_settled_once(tmp_path, monkeypatch) -> None:
    paths = _paths(tmp_path)
    hit = _hit(paths.store, "settled.md")
    monkeypatch.setattr(quick_search, "search", lambda query, project_dir, k=5: [hit])

    delivered = _call(_event(paths, call_id="s1", tool_input={"cmd": "one"}), paths)
    assert delivered is not None
    commit_exposure(delivered, "s1")
    discard_note(delivered)
    commit_exposure(delivered, "s1")

    with SessionState.open(paths.store, "claude", "s1") as state:
        assert state.data["injections"] == 1
        assert state.data["entry_counts"] == {hit["path"]: 1}
        assert len(state.data["pending_application"]) == 1
    assert sum(row.get("injected", 0) > 0 for row in _rows(paths.store)) == 1
    assert sum(row.get("injected") == 0 for row in _rows(paths.store)) == 0


def test_failed_note_build_releases_the_reservation(tmp_path, monkeypatch) -> None:
    import pytest

    from compchem_memory.hook import action_retrieval

    paths = _paths(tmp_path)
    hit = _hit(paths.store, "broken.md")
    monkeypatch.setattr(quick_search, "search", lambda query, project_dir, k=5: [hit])

    def broken_block(tool_name: str, hits: list[dict]) -> str:
        raise RuntimeError("block")

    monkeypatch.setattr(action_retrieval, "_block", broken_block)
    with pytest.raises(RuntimeError):
        _call(_event(paths), paths)

    with SessionState.open(paths.store, "claude", "s1") as state:
        assert state.data.get("injections", 0) == 0
        assert state.data.get("entry_counts", {}) == {}
