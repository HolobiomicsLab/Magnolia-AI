# tests/test_v2_export_reconstruct.py
# opencode v2 session-export compatibility (gate-1, 2026-10-06): v2 exports
# flat messages {id, text, time, type} with no parts and no tool outputs.
# reconstruct_transcript detects the shape automatically and renders
# role-labeled text; v1 parsing is untouched (regression-covered by the
# existing test_opencode_ingest suite).
import json
from pathlib import Path

from compchem_memory.opencode_ingest import reconstruct_transcript

FIXTURE = Path(__file__).parent / "fixtures" / "v2_session_export.json"


def _v2_export(messages):
    return {"info": {"id": "ses_test"}, "messages": messages}


def test_real_v2_fixture_reconstructs():
    export = json.loads(FIXTURE.read_text())
    t = reconstruct_transcript(export)
    assert t, "v2 export produced an empty transcript"
    # Message text may itself contain blank lines, so assert on role markers,
    # not on \n\n block structure. NOTE: in both real v2.0.6 exports captured
    # (gate-1, 2026-10-06) every non-empty message was type=user — assistant
    # turns exported with empty text. Assistant rendering is covered by the
    # synthetic tests below; the fixture pins the real structure.
    assert t.startswith("USER: ")
    n_user = sum(1 for i in range(len(t)) if t.startswith("USER: ", i))
    n_asst = sum(1 for i in range(len(t)) if t.startswith("ASSISTANT: ", i))
    with_text = sum(1 for m in export["messages"] if (m.get("text") or "").strip())
    assert n_user + n_asst == with_text      # every non-empty message rendered


def test_v2_flat_messages_rendered_and_empties_skipped():
    export = _v2_export([
        {"id": "m1", "type": "user", "time": {"created": 1}, "text": "run the docking"},
        {"id": "m2", "type": "assistant", "time": {"created": 2}, "text": ""},
        {"id": "m3", "type": "assistant", "time": {"created": 3},
         "text": "Submitting the haddock3 run."},
    ])
    t = reconstruct_transcript(export)
    assert t == "USER: run the docking\n\nASSISTANT: Submitting the haddock3 run."


def test_v1_shape_still_parsed_parts():
    v1 = {"info": {}, "messages": [{
        "info": {"role": "user"},
        "parts": [{"type": "text", "text": "v1 message"}],
    }]}
    assert reconstruct_transcript(v1) == "USER: v1 message"


def test_tool_chars_irrelevant_for_v2():
    # v2 exports carry no tool parts; a set tool_chars must not break parsing.
    export = _v2_export([{"id": "m", "type": "user", "text": "hello"}])
    assert reconstruct_transcript(export, tool_chars=4000) == "USER: hello"
