"""Ingest real opencode conversation transcripts into distillation.

Pipeline: read .magnolia/opencode-sessions.jsonl (written by the capture plugin)
-> for each ses_id not yet distilled: `opencode export <id>` -> reconstruct an
ordered transcript -> scrub secrets -> distill (conversation-oriented) -> save to
staging -> mark the ses_id done. The marker gives once-only processing.
"""

import json
from pathlib import Path

import yaml
import pytest

from compchem_memory import opencode_ingest as oi
from compchem_memory.storage import ensure_project_store


# ---- reconstruct_transcript -------------------------------------------------

def _export(messages):
    return {"info": {"id": "ses_x"}, "messages": messages}


def test_reconstruct_transcript_orders_roles_text_and_reasoning():
    export = _export([
        {"info": {"role": "user"}, "parts": [{"type": "text", "text": "dock KILDQ"}]},
        {"info": {"role": "assistant"}, "parts": [
            {"type": "reasoning", "text": "F2 sits near R272"},
            {"type": "text", "text": "Cluster 2 is best"},
        ]},
    ])
    t = oi.reconstruct_transcript(export)
    assert "USER: dock KILDQ" in t
    assert "F2 sits near R272" in t          # reasoning preserved
    assert "ASSISTANT: Cluster 2 is best" in t
    # order: user before assistant
    assert t.index("dock KILDQ") < t.index("Cluster 2 is best")


# ---- tool-result inclusion (MAGNOLIA_TRANSCRIPT_TOOL_CHARS) -----------------

def _tool_part(name, output=None, error=None, status="completed"):
    st = {"status": status}
    if output is not None:
        st["output"] = output
    if error is not None:
        st["error"] = error
    return {"type": "tool", "tool": name, "state": st}


def test_tool_results_excluded_by_default():
    export = _export([
        {"info": {"role": "assistant"}, "parts": [
            _tool_part("run_shell", output="Kd ≈ 57 nM secret finding"),
        ]},
    ])
    t = oi.reconstruct_transcript(export)
    assert "(tool:run_shell)" in t
    assert "Kd ≈ 57 nM" not in t


def test_tool_results_included_when_tool_chars_set(monkeypatch):
    monkeypatch.setenv("MAGNOLIA_TRANSCRIPT_TOOL_CHARS", "400")
    export = _export([
        {"info": {"role": "assistant"}, "parts": [
            _tool_part("webfetch", output="amyloid binding\nSBD contact\nshort result"),
        ]},
    ])
    t = oi.reconstruct_transcript(export)
    assert "(tool:webfetch): amyloid binding SBD contact short result" in t
    assert "...[truncated]" not in t         # short output included whole, unmarked


def test_tool_results_truncated_with_explicit_marker(monkeypatch):
    monkeypatch.setenv("MAGNOLIA_TRANSCRIPT_TOOL_CHARS", "50")
    long_out = "x" * 500 + " tail-token-that-must-not-appear"
    export = _export([
        {"info": {"role": "assistant"}, "parts": [_tool_part("read", output=long_out)]},
    ])
    t = oi.reconstruct_transcript(export)
    assert "x" * 50 + " ...[truncated]" in t
    assert "tail-token-that-must-not-appear" not in t


def test_tool_error_used_when_no_output(monkeypatch):
    monkeypatch.setenv("MAGNOLIA_TRANSCRIPT_TOOL_CHARS", "200")
    export = _export([
        {"info": {"role": "assistant"}, "parts": [
            _tool_part("gnina_dock", status="error", error="receptor file missing"),
        ]},
    ])
    t = oi.reconstruct_transcript(export)
    assert "(tool:gnina_dock): receptor file missing" in t


def test_tool_part_without_output_stays_stub(monkeypatch):
    monkeypatch.setenv("MAGNOLIA_TRANSCRIPT_TOOL_CHARS", "200")
    export = _export([
        {"info": {"role": "assistant"}, "parts": [_tool_part("think")]},
    ])
    assert "(tool:think)" in oi.reconstruct_transcript(export)


def test_tool_chars_param_overrides_env(monkeypatch):
    monkeypatch.setenv("MAGNOLIA_TRANSCRIPT_TOOL_CHARS", "0")   # env says off
    export = _export([
        {"info": {"role": "assistant"}, "parts": [_tool_part("read", output="content")]},
    ])
    assert "(tool:read): content" in oi.reconstruct_transcript(export, tool_chars=100)


def test_tool_chars_env_garbage_is_off(monkeypatch):
    monkeypatch.setenv("MAGNOLIA_TRANSCRIPT_TOOL_CHARS", "not-a-number")
    export = _export([
        {"info": {"role": "assistant"}, "parts": [_tool_part("read", output="content")]},
    ])
    assert "(tool:read)" in oi.reconstruct_transcript(export)
    assert "content" not in oi.reconstruct_transcript(export)


def test_memory_plumbing_outputs_never_included(monkeypatch):
    monkeypatch.setenv("MAGNOLIA_TRANSCRIPT_TOOL_CHARS", "4000")
    export = _export([
        {"info": {"role": "assistant"}, "parts": [
            _tool_part("compchem-memory_memory_get_context", output='{"content": "[PROJECT GOAL] dump"}'),
            _tool_part("memory_search", output="plumbing results"),
            _tool_part("run_shell", output="science payload"),
        ]},
    ])
    t = oi.reconstruct_transcript(export)
    assert "(tool:compchem-memory_memory_get_context)" in t      # stub, no output
    assert "[PROJECT GOAL] dump" not in t
    assert "(tool:memory_search)" in t
    assert "plumbing results" not in t
    assert "(tool:run_shell): science payload" in t              # science tool included


# ---- scrub_secrets ----------------------------------------------------------

def test_scrub_secrets_redacts_keys_keeps_prose():
    text = "use key sk-abcdEFGH1234ijklMNOP5678 and token ghp_ABCdef0123456789ABCdef0123456789ABCD"
    out = oi.scrub_secrets(text)
    assert "sk-abcdEFGH1234" not in out
    assert "ghp_ABCdef" not in out
    assert "REDACTED" in out
    assert oi.scrub_secrets("the contact map shows F2 near R272") == "the contact map shows F2 near R272"


# ---- ingest_opencode_sessions ----------------------------------------------

@pytest.fixture
def store(tmp_path):
    ensure_project_store(str(tmp_path))
    return Path(tmp_path) / ".magnolia"


def _write_mapping(store, ids):
    p = store / "opencode-sessions.jsonl"
    p.write_text("".join(json.dumps({"opencode_session_id": i, "ts": "t"}) + "\n" for i in ids))
    return p


def test_ingest_processes_each_new_session_once(store):
    _write_mapping(store, ["ses_a", "ses_b"])
    exported = {"ses_a": _export([{"info": {"id": "a1", "role": "user"}, "parts": [{"type": "text", "text": "A"}]}]),
                "ses_b": _export([{"info": {"id": "b1", "role": "user"}, "parts": [{"type": "text", "text": "B"}]}])}
    seen = []

    def fake_export(sid): return exported[sid]
    def fake_distill(transcript): seen.append(transcript); return [{"title": f"finding {transcript[-1]}", "content": transcript, "type": "scientific_finding"}]

    saved = oi.ingest_opencode_sessions(str(store), exporter=fake_export, distiller=fake_distill)
    assert len(saved) == 2
    assert (store / "staging").glob("*.md")
    # markers written
    assert (store / "opencode-distilled" / "ses_a.json").exists()
    assert (store / "opencode-distilled" / "ses_b.json").exists()

    # re-run: nothing new (dedup via markers)
    saved2 = oi.ingest_opencode_sessions(str(store), exporter=fake_export, distiller=fake_distill)
    assert saved2 == []


def test_ingest_marks_even_with_zero_candidates(store):
    _write_mapping(store, ["ses_empty"])
    oi.ingest_opencode_sessions(str(store),
                                exporter=lambda s: _export([{"info": {"role": "user"}, "parts": [{"type": "text", "text": "hi"}]}]),
                                distiller=lambda t: [])
    assert (store / "opencode-distilled" / "ses_empty.json").exists()  # don't re-export forever


def test_ingest_does_not_mark_on_export_failure(store):
    _write_mapping(store, ["ses_fail"])
    oi.ingest_opencode_sessions(str(store), exporter=lambda s: None, distiller=lambda t: [{"title": "x", "content": "c"}])
    assert not (store / "opencode-distilled" / "ses_fail.json").exists()  # retry next time


def test_export_session_reads_full_output_via_file_not_truncated_pipe(monkeypatch):
    """Regression: `opencode export` truncates piped stdout at one 64 KB pipe
    buffer, so export_session must redirect to a FILE (full output), not capture
    a pipe. A >64 KB session piped -> invalid JSON -> None -> silently skipped."""
    import subprocess as sp
    big = {"info": {"id": "ses_big"},
           "messages": [{"info": {"role": "user"},
                         "parts": [{"type": "text", "text": "x" * 100000}]}],
           "end_marker": "TAIL_OK"}
    payload = json.dumps(big)
    assert len(payload) > 65536  # must exceed one pipe buffer to be a real test

    captured = {}

    def fake_run(cmd, **kwargs):
        captured.update(kwargs)
        # Model opencode's bug: a pipe is truncated; a file gets the full write.
        if kwargs.get("capture_output") or kwargs.get("stdout") in (sp.PIPE, -1):
            return sp.CompletedProcess(cmd, 0, stdout=payload[:65536], stderr="")
        out = kwargs.get("stdout")
        out.write(payload)
        out.flush()
        return sp.CompletedProcess(cmd, 0, stdout="", stderr="")

    monkeypatch.setattr(oi.subprocess, "run", fake_run)
    result = oi.export_session("ses_big")
    assert result is not None
    assert result.get("end_marker") == "TAIL_OK"   # full JSON, not cut off
    assert not captured.get("capture_output")        # must not pipe-capture


def test_ingest_does_not_mark_on_distill_failure(store):
    """A distiller that returns None (LLM error / context overflow) must NOT mark
    the session done — otherwise a transient failure loses the session forever.
    Distinct from a distiller returning [] (genuine empty), which DOES mark."""
    _write_mapping(store, ["ses_llmfail"])
    oi.ingest_opencode_sessions(
        str(store),
        exporter=lambda s: _export([{"info": {"role": "user"},
                                     "parts": [{"type": "text", "text": "hi"}]}]),
        distiller=lambda t: None,  # None == distillation FAILED
    )
    assert not (store / "opencode-distilled" / "ses_llmfail.json").exists()  # retry


def test_saved_entry_carries_provenance(store):
    _write_mapping(store, ["ses_prov"])
    oi.ingest_opencode_sessions(str(store),
                                exporter=lambda s: _export([{"info": {"role": "user"}, "parts": [{"type": "text", "text": "hi"}]}]),
                                distiller=lambda t: [{"title": "Contact finding", "content": "F2 near R272", "type": "scientific_finding"}])
    md = next((store / "staging").glob("*Contact_finding*.md"))
    meta = yaml.safe_load(md.read_text().split("---")[1])
    assert meta["source"] == "opencode_distill"
    assert meta["opencode_session_id"] == "ses_prov"
    assert meta["type"] == "scientific_finding"
