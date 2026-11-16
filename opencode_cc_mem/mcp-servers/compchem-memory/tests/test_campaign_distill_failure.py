import json

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
