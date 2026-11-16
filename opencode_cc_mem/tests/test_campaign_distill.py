import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from campaign_runner.distill import drain_queue  # noqa: E402


def test_drain_queue_commits_once(tmp_path, monkeypatch):
    from compchem_memory import opencode_ingest

    project = tmp_path / "project"
    run = project / "runs" / "campaign"
    control = run / "control"
    control.mkdir(parents=True)
    (project / ".magnolia").mkdir(parents=True)
    (control / "distill-queue.jsonl").write_text(
        json.dumps({"session_ids": ["ses_one", "ses_two"]}) + "\n",
        encoding="utf-8",
    )

    calls = []

    def fake_distill(store_dir, session_id, *, commit, exporter, distiller):
        calls.append((store_dir, session_id, commit))
        return {"status": "committed", "saved": [session_id]}

    monkeypatch.setattr(
        opencode_ingest,
        "distill_session_transcript",
        fake_distill,
    )
    first = drain_queue(
        run,
        project,
        tmp_path / "db.sqlite",
        exporter=lambda session_id: {},
    )
    assert first["completed"] == ["ses_one", "ses_two"]
    assert [call[1] for call in calls] == ["ses_one", "ses_two"]
    done = (control / "distill-done.jsonl").read_text().splitlines()
    assert len(done) == 2

    second = drain_queue(
        run,
        project,
        tmp_path / "db.sqlite",
        exporter=lambda session_id: {},
    )
    assert second["sessions"] == 0
    assert len(calls) == 2


def test_mixed_client_worker_reviewer_distilled_once(tmp_path, monkeypatch):
    from compchem_memory import opencode_ingest

    project = tmp_path / "project"
    control = project / "runs" / "campaign" / "control"
    control.mkdir(parents=True)
    (project / ".magnolia").mkdir()
    sessions = [
        {"harness": "claude", "session_id": "worker", "key": "claude-worker"},
        {"harness": "vibe", "session_id": "review", "key": "vibe-review"},
    ]
    (control / "distill-queue.jsonl").write_text(
        json.dumps({"sessions": sessions}) + "\n"
    )
    seen = []

    def fake_distill(store_dir, ref, **kwargs):
        seen.append((ref.harness, ref.session_id))
        return {"status": "committed", "saved": []}

    monkeypatch.setattr(opencode_ingest, "distill_session_transcript", fake_distill)
    result = drain_queue(control.parent, project, tmp_path / "db", exporter=lambda sid: {})
    assert result["completed"] == sessions
    assert seen == [("claude", "worker"), ("vibe", "review")]
    assert drain_queue(
        control.parent, project, tmp_path / "db", exporter=lambda sid: {}
    )["sessions"] == 0


def test_legacy_queue_and_done_markers_retained(tmp_path, monkeypatch):
    from compchem_memory import opencode_ingest

    project = tmp_path / "project"
    control = project / "runs" / "campaign" / "control"
    control.mkdir(parents=True)
    (project / ".magnolia").mkdir()
    (control / "distill-queue.jsonl").write_text(
        json.dumps({"session_ids": ["old", "new"]}) + "\n"
    )
    (control / "distill-done.jsonl").write_text(json.dumps({"session_id": "old"}) + "\n")
    seen = []

    def fake_distill(store_dir, ref, **kwargs):
        seen.append(("opencode", ref))
        return {"status": "committed", "saved": []}

    monkeypatch.setattr(opencode_ingest, "distill_session_transcript", fake_distill)
    drain_queue(control.parent, project, tmp_path / "db", exporter=lambda sid: {})
    assert seen == [("opencode", "new")]


def test_same_session_id_different_harness_not_collapsed(tmp_path, monkeypatch):
    from compchem_memory import opencode_ingest

    project = tmp_path / "project"
    control = project / "runs" / "campaign" / "control"
    control.mkdir(parents=True)
    (project / ".magnolia").mkdir()
    sessions = [
        {"harness": "claude", "session_id": "same", "key": "claude-same"},
        {"harness": "vibe", "session_id": "same", "key": "vibe-same"},
    ]
    (control / "distill-queue.jsonl").write_text(json.dumps({"sessions": sessions}) + "\n")
    seen = []
    monkeypatch.setattr(
        opencode_ingest,
        "distill_session_transcript",
        lambda store, ref, **kwargs: seen.append(ref.key) or {"status": "committed", "saved": []},
    )
    drain_queue(control.parent, project, tmp_path / "db", exporter=lambda sid: {})
    assert seen == ["claude-same", "vibe-same"]
