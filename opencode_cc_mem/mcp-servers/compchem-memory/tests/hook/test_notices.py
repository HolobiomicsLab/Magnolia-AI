"""Job and critic notices are drained into one hook response."""

from __future__ import annotations

from types import SimpleNamespace

from compchem_memory.hook.notices import pending_notices
from compchem_memory.hook.project import ProjectPaths
from compchem_memory.job_notices import push_job_notice


def _paths(tmp_path):
    project = tmp_path / "project"
    store = project / ".magnolia"
    store.mkdir(parents=True)
    return ProjectPaths(tmp_path, project, store, "project", False)


def test_pending_notices_drains_ten(tmp_path):
    paths = _paths(tmp_path)
    for index in range(11):
        push_job_notice(
            str(paths.project_dir), run_id=f"run-{'x' * 24}-{index}", tool=f"tool-{index}",
            state="completed", category="success",
        )
    state = SimpleNamespace(data={"unshown_notices": ["critic one", "critic two"]})

    first = pending_notices(paths, state)
    second = pending_notices(paths, state)
    assert len(first.splitlines()) == 12
    assert first.splitlines()[0].startswith("✅ tool-0 finished (completed) — run …")
    assert first.splitlines()[-2:] == ["critic one", "critic two"]
    expected_tail = f"run-{'x' * 24}-10"[-24:]
    assert second == f"✅ tool-10 finished (completed) — run …{expected_tail}"
    assert state.data["unshown_notices"] == []


def test_pending_notices_malformed_rows_and_switch(tmp_path, monkeypatch):
    paths = _paths(tmp_path)
    queue = paths.store / ".job-notices.jsonl"
    queue.write_text(
        "not json\n"
        '{"category":"science_failure","tool":"dock","state":"failed","run_id":"abcdef"}\n'
        "[]\n",
        encoding="utf-8",
    )
    state = SimpleNamespace(data={"unshown_notices": ["critic warning"], "other": 1})
    monkeypatch.setenv("MAGNOLIA_JOB_NOTIFY", "0")

    assert pending_notices(paths, state) == "critic warning"
    assert queue.exists()
    assert state.data == {"unshown_notices": [], "other": 1}

    monkeypatch.delenv("MAGNOLIA_JOB_NOTIFY")
    assert pending_notices(paths, state) == "❌ dock finished (failed) — run …abcdef"
    assert not queue.exists()


def test_pending_notices_is_defensive_and_caps_lines(tmp_path):
    paths = _paths(tmp_path)
    push_job_notice(
        str(paths.project_dir), run_id="r" * 200, tool="t" * 200,
        state="infra_failure", category="infra_failure",
    )
    state = SimpleNamespace(data={"unshown_notices": "not-a-list"})
    notice = pending_notices(paths, state)
    assert len(notice) == 160
    assert notice.startswith("⚠️ ")
    assert state.data["unshown_notices"] == []
