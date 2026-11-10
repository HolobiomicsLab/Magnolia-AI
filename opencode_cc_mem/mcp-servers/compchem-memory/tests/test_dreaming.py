"""Tests for the dreaming pass (transcript-grounded memory maintenance).

Run from mcp-servers/compchem-memory/:
    uv run pytest tests/test_dreaming.py -q
"""

from __future__ import annotations

import json
from pathlib import Path

import yaml

from compchem_memory import dreaming
from compchem_memory.dreaming import DreamingConfig


def make_store(tmp_path: Path) -> Path:
    store = tmp_path / ".magnolia"
    (store / "staging").mkdir(parents=True)
    (store / "entries").mkdir()
    (store / "sessions").mkdir()
    (store / "opencode-transcripts").mkdir()
    return store


def write_entry(store: Path, name: str, body: str, tier: str = "staging", **meta) -> Path:
    path = store / tier / name
    front = yaml.safe_dump(meta, sort_keys=False)
    path.write_text(f"---\n{front}---\n\n{body}\n", encoding="utf-8")
    return path


def write_session(store: Path, sid: str, rows: list[dict]) -> None:
    (store / "sessions" / f"{sid}.jsonl").write_text(
        "\n".join(json.dumps(r) for r in rows) + "\n", encoding="utf-8"
    )


def judge_returning(verdict, corrected_text="", evidence=None, rationale="test rationale"):
    def _judge(entry, transcript):
        return {
            "verdict": verdict,
            "corrected_text": corrected_text,
            "evidence": evidence if evidence is not None else [],
            "rationale": rationale,
        }
    return _judge


def anchored(store, name="20261101_000000_000001_entry.md", sid="2026-11-01_000000", body="The run completed."):
    write_session(store, sid, [{
        "timestamp": "2026-11-01T00:00:00+00:00",
        "event_type": "tool_call",
        "tool": "demo",
        "result_summary": "run completed",
    }])
    return write_entry(
        store, name, body,
        id="20261101_000000_000001",
        title="Demo entry",
        type="workflow_note",
        opencode_session_id=sid,
    )


def test_selection_skips_entries_without_transcript_anchor(tmp_path):
    store = make_store(tmp_path)
    write_entry(store, "no_anchor.md", "A note with no session.", title="No anchor")
    path = anchored(store)
    selected = dreaming.select_dirty_entries(store, DreamingConfig())
    assert [s["entry"] for s in selected] == [path.name]


def test_selection_respects_max_entries(tmp_path):
    store = make_store(tmp_path)
    for i in range(3):
        anchored(store, name=f"2026110{i}_000000_00000{i}_e.md", sid=f"2026-11-0{i}_000000")
    selected = dreaming.select_dirty_entries(store, DreamingConfig(max_entries=2))
    assert len(selected) == 2


def test_confirmed_verdict_is_receipt_only(tmp_path):
    store = make_store(tmp_path)
    path = anchored(store)
    before = path.read_bytes()
    summary = dreaming.run_dreaming(tmp_path, judge=judge_returning("confirmed"))
    assert summary["verdicts"]["confirmed"] == 1
    assert summary["applied"] == 0
    assert path.read_bytes() == before
    log = (store / "reflex" / "dreaming-log.jsonl").read_text().strip().splitlines()
    assert len(log) == 1
    assert json.loads(log[0])["applied"] is False
    assert all(summary["acceptance"].values())


def test_wrong_verdict_appends_correction(tmp_path):
    store = make_store(tmp_path)
    path = anchored(store)
    evidence = [{"source": "session 2026-11-01_000000", "quote": "run completed"}]
    summary = dreaming.run_dreaming(
        tmp_path,
        judge=judge_returning(
            "wrong",
            corrected_text="The run did NOT complete; it stopped at the gate.",
            evidence=evidence,
        ),
    )
    assert summary["applied"] == 1
    text = path.read_text()
    assert "## Dreaming correction" in text
    assert "did NOT complete" in text
    assert "run completed" in text  # evidence quote is preserved
    meta = yaml.safe_load(text.split("---", 2)[1])
    assert meta["dreaming_status"] == "corrected"
    assert meta["dreaming_checked_at"]
    assert all(summary["acceptance"].values())
    # Original body is still present (append-only).
    assert "The run completed." in text


def test_wrong_without_evidence_is_not_applied(tmp_path):
    store = make_store(tmp_path)
    path = anchored(store)
    before = path.read_bytes()
    summary = dreaming.run_dreaming(
        tmp_path,
        judge=judge_returning("wrong", corrected_text="A correction with no evidence."),
    )
    assert summary["applied"] == 0
    assert path.read_bytes() == before
    result = summary["results"][0]
    assert "evidence" in result["reason"]


def test_missing_transcript_is_unverifiable_and_judge_not_called(tmp_path):
    store = make_store(tmp_path)
    write_entry(
        store, "20261101_000000_000002_orphan.md", "Anchored to a missing session.",
        opencode_session_id="2026-11-01_999999",
    )
    calls = []

    def counting_judge(entry, transcript):
        calls.append(entry["entry"])
        return {"verdict": "confirmed", "corrected_text": "", "evidence": [], "rationale": ""}

    summary = dreaming.run_dreaming(tmp_path, judge=counting_judge)
    assert calls == []
    assert summary["verdicts"]["unverifiable"] == 1
    assert summary["applied"] == 0


def test_stale_verdict_stamps_frontmatter(tmp_path):
    store = make_store(tmp_path)
    path = anchored(store)
    summary = dreaming.run_dreaming(tmp_path, judge=judge_returning("stale"))
    assert summary["applied"] == 1
    meta = yaml.safe_load(path.read_text().split("---", 2)[1])
    assert meta["dreaming_status"] == "stale"
    assert all(summary["acceptance"].values())


def test_recheck_days_skips_recently_checked_entries(tmp_path):
    store = make_store(tmp_path)
    anchored(store)
    dreaming.run_dreaming(tmp_path, judge=judge_returning("stale"))
    selected = dreaming.select_dirty_entries(store, DreamingConfig(recheck_days=7))
    assert selected == []
    selected = dreaming.select_dirty_entries(store, DreamingConfig(recheck_days=0))
    assert len(selected) == 1


def test_hash_mismatch_dismisses_instead_of_mutating(tmp_path):
    store = make_store(tmp_path)
    path = anchored(store)
    artifact = dreaming.propose_dreaming(tmp_path, judge=judge_returning("stale"))
    # Human edits the entry after the proposal was written.
    path.write_text(path.read_text() + "\nHuman edit after the proposal.\n")
    summary = dreaming.apply_dreaming(tmp_path, artifact=artifact)
    assert summary["applied"] == 0
    assert summary["dismissed"] == 1
    text = path.read_text()
    assert "Human edit after the proposal." in text
    assert "dreaming_status" not in text.split("---", 2)[1]


def test_max_apply_caps_mutations(tmp_path):
    store = make_store(tmp_path)
    paths = [
        anchored(store, name=f"2026110{i}_000000_00000{i}_e.md", sid=f"2026-11-0{i}_000000")
        for i in range(3)
    ]
    summary = dreaming.run_dreaming(
        tmp_path, config=DreamingConfig(max_apply=1), judge=judge_returning("stale")
    )
    assert summary["applied"] == 1
    stamped = [p for p in paths if "dreaming_status" in p.read_text().split("---", 2)[1]]
    assert len(stamped) == 1
    artifact = json.loads((store / "reflex" / "dreaming-proposal.json").read_text())
    assert len(artifact["applied"]) == 1


def test_opencode_transcript_is_preferred_over_jsonl(tmp_path):
    store = make_store(tmp_path)
    sid = "ses_test_123"
    (store / "opencode-transcripts" / f"{sid}.json").write_text(json.dumps([
        {"info": {"role": "user"}, "parts": [{"type": "text", "text": "dock KILDQ"}]},
        {"info": {"role": "assistant"}, "parts": [{"type": "text", "text": "starting docking"}]},
    ]))
    write_session(store, sid, [{"event_type": "tool_call", "tool": "should-not-be-used"}])
    resolved = dreaming.resolve_transcripts(tmp_path, [sid], DreamingConfig())
    assert "dock KILDQ" in resolved["text"]
    assert "starting docking" in resolved["text"]
    assert "should-not-be-used" not in resolved["text"]


def test_report_lists_entries_and_acceptance(tmp_path):
    store = make_store(tmp_path)
    anchored(store)
    summary = dreaming.run_dreaming(tmp_path, judge=judge_returning("confirmed"))
    report = dreaming.render_dreaming_report(summary)
    assert "20261101_000000_000001_entry.md" in report
    assert "confirmed" in report
    assert "bounded_scope: PASS" in report
