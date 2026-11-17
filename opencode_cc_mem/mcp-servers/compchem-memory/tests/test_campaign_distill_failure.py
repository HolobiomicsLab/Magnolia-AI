import json

from campaign_runner.distill import drain_queue
from compchem_memory.opencode_ingest import distill_session_transcript


def test_failed_extraction_keeps_queue_and_cursor_retryable(tmp_path):
    store = tmp_path / ".magnolia"
    store.mkdir()
    export = {
        "messages": [
            {
                "info": {"id": "m1", "role": "user"},
                "parts": [{"type": "text", "text": "same dialogue"}],
            },
            {
                "info": {"id": "m2", "role": "assistant"},
                "parts": [{"type": "text", "text": "same answer"}],
            },
        ]
    }
    attempts = []

    def distiller(transcript):
        attempts.append(transcript)
        return None if len(attempts) == 1 else []

    first = distill_session_transcript(
        str(store), "session", commit=True, exporter=lambda sid: export, distiller=distiller
    )
    marker = store / "opencode-distilled" / "session.json"
    assert first is None
    assert not marker.exists()

    second = distill_session_transcript(
        str(store), "session", commit=True, exporter=lambda sid: export, distiller=distiller
    )
    assert second == {"status": "committed", "saved": []}
    assert attempts[0] == attempts[1]
    assert json.loads(marker.read_text())["cursor"] == "m2"


def test_retryable_skip_is_not_acknowledged_done(tmp_path, monkeypatch):
    from compchem_memory import opencode_ingest

    project = tmp_path / "project"
    control = project / "runs" / "campaign" / "control"
    control.mkdir(parents=True)
    (project / ".magnolia").mkdir()
    (control / "distill-queue.jsonl").write_text(
        json.dumps({"session_ids": ["retry-me"]}) + "\n"
    )
    monkeypatch.setattr(
        opencode_ingest,
        "distill_session_transcript",
        lambda *args, **kwargs: {
            "status": "skipped",
            "reason": "concurrent write",
            "retry": True,
            "saved": [],
        },
    )

    result = drain_queue(
        control.parent, project, tmp_path / "db", exporter=lambda session_id: {}
    )

    assert result["completed"] == []
    assert result["retryable"] == ["retry-me"]
    assert not (control / "distill-done.jsonl").exists()
