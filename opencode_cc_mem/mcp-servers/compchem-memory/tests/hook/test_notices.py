"""Job and critic notices are read into one hook response and consumed after delivery."""

from __future__ import annotations

import threading
import time
from types import SimpleNamespace

from compchem_memory.hook.notices import commit_notices, peek_notices, release_notices
from compchem_memory.hook.project import ProjectPaths
from compchem_memory.hook.state import SessionState
from compchem_memory.job_notices import push_job_notice


def _paths(tmp_path):
    project = tmp_path / "project"
    store = project / ".magnolia"
    store.mkdir(parents=True)
    return ProjectPaths(tmp_path, project, store, "project", False)


def _deliver(paths, harness="claude", session="s1"):
    """Peek from the stored state, then commit as the CLI does after emission."""
    with SessionState.open(paths.store, harness, session) as state:
        notices = peek_notices(paths, state)
    if notices is None:
        return None
    commit_notices(paths, harness, session, notices)
    return notices.text


def _queue_critic(paths, notices, harness="claude", session="s1"):
    with SessionState.open(paths.store, harness, session) as state:
        state.data["unshown_notices"] = list(notices)


def test_notices_drain_ten_after_delivery(tmp_path):
    paths = _paths(tmp_path)
    for index in range(11):
        push_job_notice(
            str(paths.project_dir), run_id=f"run-{'x' * 24}-{index}", tool=f"tool-{index}",
            state="completed", category="success",
        )
    _queue_critic(paths, ["critic one", "critic two"])

    first = _deliver(paths)
    second = _deliver(paths)
    assert len(first.splitlines()) == 12
    assert first.splitlines()[0].startswith("✅ tool-0 finished (completed) — run run-")
    assert first.splitlines()[-2:] == ["critic one", "critic two"]
    expected_run = f"run-{'x' * 24}-10"
    assert second == f"✅ tool-10 finished (completed) — run {expected_run}"
    assert _deliver(paths) is None
    with SessionState.open(paths.store, "claude", "s1") as state:
        assert state.data["unshown_notices"] == []


def test_peek_consumes_nothing(tmp_path):
    paths = _paths(tmp_path)
    push_job_notice(str(paths.project_dir), run_id="r1", tool="dock", state="done", category="success")
    state = SimpleNamespace(data={"unshown_notices": ["critic warning"]})

    first = peek_notices(paths, state)
    release_notices(first)
    second = peek_notices(paths, state)

    assert first is not None and second is not None and first.text == second.text
    assert first.text.splitlines() == ["✅ dock finished (done) — run r1", "critic warning"]
    assert state.data == {"unshown_notices": ["critic warning"]}
    assert (paths.store / ".job-notices.jsonl").exists()
    release_notices(second)


def test_job_notice_keeps_run_ids_through_64_chars_and_shortens_longer_ids(tmp_path):
    paths = _paths(tmp_path)
    exact = "r" * 64
    long = "start-" + "x" * 70 + "-end"
    push_job_notice(
        str(paths.project_dir), run_id=exact, tool="dock", state="done", category="success",
    )
    push_job_notice(
        str(paths.project_dir), run_id=long, tool="dock", state="done", category="success",
    )

    lines = _deliver(paths).splitlines()

    assert lines[0].endswith(f"run {exact}")
    shown = lines[1].split(" — run ", 1)[1]
    assert shown.startswith("start-") and shown.endswith("-end")
    assert shown.count("…") == 1 and len(shown) == 64
    assert all(len(line) <= 160 for line in lines)


def test_commit_keeps_notices_added_after_peek(tmp_path):
    paths = _paths(tmp_path)
    push_job_notice(str(paths.project_dir), run_id="r1", tool="first", state="done", category="success")
    _queue_critic(paths, ["critic one"])
    with SessionState.open(paths.store, "claude", "s1") as state:
        notices = peek_notices(paths, state)

    writer = threading.Thread(
        target=push_job_notice,
        args=(str(paths.project_dir),),
        kwargs={"run_id": "r2", "tool": "later", "state": "done", "category": "success"},
    )
    writer.start()
    time.sleep(0.05)
    assert writer.is_alive()  # the append waits for the peek-to-commit lock
    with SessionState.open(paths.store, "claude", "s1") as state:
        state.data["unshown_notices"].append("critic later")
    commit_notices(paths, "claude", "s1", notices)
    writer.join(timeout=1)

    assert not writer.is_alive()
    assert _deliver(paths).splitlines() == ["✅ later finished (done) — run r2", "critic later"]


def test_second_consumer_cannot_peek_same_notice(tmp_path):
    paths = _paths(tmp_path)
    push_job_notice(str(paths.project_dir), run_id="r1", tool="dock", state="done", category="success")
    state = SimpleNamespace(data={})

    first = peek_notices(paths, state)
    second = peek_notices(paths, state)

    assert first is not None
    assert second is None  # first still owns the peek-to-commit queue lock
    commit_notices(paths, "claude", "s1", first)
    assert peek_notices(paths, state) is None
    assert not (paths.store / ".job-notices.jsonl").exists()


def test_notices_malformed_rows_and_switch(tmp_path, monkeypatch):
    paths = _paths(tmp_path)
    queue = paths.store / ".job-notices.jsonl"
    queue.write_text(
        "not json\n"
        '{"category":"science_failure","tool":"dock","state":"failed","run_id":"abcdef"}\n'
        "[]\n",
        encoding="utf-8",
    )
    _queue_critic(paths, ["critic warning"])
    monkeypatch.setenv("MAGNOLIA_JOB_NOTIFY", "0")

    assert _deliver(paths) == "critic warning"
    assert queue.exists()
    with SessionState.open(paths.store, "claude", "s1") as state:
        assert state.data["unshown_notices"] == []

    monkeypatch.delenv("MAGNOLIA_JOB_NOTIFY")
    assert _deliver(paths) == "❌ dock finished (failed) — run abcdef"
    assert not queue.exists()


def test_notices_are_defensive_and_cap_lines(tmp_path):
    paths = _paths(tmp_path)
    push_job_notice(
        str(paths.project_dir), run_id="r" * 200, tool="t" * 200,
        state="infra_failure", category="infra_failure",
    )
    state = SimpleNamespace(data={"unshown_notices": "not-a-list"})
    notices = peek_notices(paths, state)
    assert len(notices.text) <= 160
    assert notices.text.startswith("⚠️ ")
    assert notices.critic == []
    release_notices(notices)


def test_critic_notice_flood_is_bounded_and_drains_in_batches(tmp_path):
    paths = _paths(tmp_path)
    queued = [f"critic-{index}-" + "x" * 500 for index in range(23)]
    _queue_critic(paths, queued)

    first = _deliver(paths)

    assert first is not None
    assert len(first.splitlines()) == 10
    assert len(first) <= 1_609
    assert all(len(line) <= 160 for line in first.splitlines())
    with SessionState.open(paths.store, "claude", "s1") as state:
        assert state.data["unshown_notices"] == queued[10:]

    assert len(_deliver(paths).splitlines()) == 10
    assert len(_deliver(paths).splitlines()) == 3
    assert _deliver(paths) is None


def test_critic_notice_newlines_are_flattened(tmp_path):
    paths = _paths(tmp_path)
    _queue_critic(paths, ["first line\n# Injected: second line\rthird"])

    text = _deliver(paths)

    assert text is not None
    assert text.splitlines() == ["first line # Injected: second line third"]
    with SessionState.open(paths.store, "claude", "s1") as state:
        assert state.data["unshown_notices"] == []
