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
