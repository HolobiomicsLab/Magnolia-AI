# tests/test_consolidation_labels.py
# Label store (2026-10-06): every handled proposal lands one row in
# reflex/labels.jsonl — verdict, who decided (human/auto band), content key,
# confidence, sources. Ground truth for bake-off arms and retirement tuning.
import json

import pytest
import yaml

from compchem_memory.consolidation import (
    apply_proposals,
    auto_apply_band,
    backfill_labels,
    consolidate_project_findings,
)


def _write(staging, name, title, body, ses):
    fm = {"title": title, "type": "scientific_finding",
          "opencode_session_id": ses, "observed_in_sessions": [ses],
          "tags": [], "tools": [], "confidence": 0.6}
    (staging / name).write_text("---\n" + yaml.dump(fm) + "---\n\n" + body + "\n")
    return str(staging / name)


def _store(tmp_path, conf=0.95):
    store = tmp_path / ".magnolia"
    staging = store / "staging"
    staging.mkdir(parents=True)
    a = _write(staging, "a.md", "A finding", "longer canonical body", "s1")
    b = _write(staging, "b.md", "B finding", "short", "s2")
    consolidate_project_findings(
        str(store),
        clusterer=lambda p: [{"ids": ["a.md", "b.md"], "confidence": conf,
                              "rationale": "same claim"}])
    return store, a, b


def _rows(store):
    return [json.loads(l) for l in
            (store / "reflex" / "labels.jsonl").read_text().splitlines()]


def test_accept_writes_human_label(tmp_path):
    store, a, b = _store(tmp_path)
    (store / ".current-session-id").write_text("ses_test123\n")

    res = apply_proposals(str(store), [0])

    assert res["applied"] == 1
    rows = _rows(store)
    assert len(rows) == 1
    r = rows[0]
    assert r["verdict"] == "accept" and r["via"] == "human"
    assert r["session"] == "ses_test123"
    assert r["cluster_key"] == ["a.md", "b.md"]
    assert r["confidence"] == pytest.approx(0.95)
    assert r["sources"] == ["a.md", "b.md"]
    assert r["merged"] == "a.md"


def test_reject_writes_label(tmp_path):
    store, a, b = _store(tmp_path)

    apply_proposals(str(store), [], reject=[0])

    (r,) = _rows(store)
    assert r["verdict"] == "reject" and r["via"] == "human"
    assert r["cluster_key"] == ["a.md", "b.md"]
    assert r["rationale"] == "same claim"


def test_auto_band_labels_via_auto(tmp_path, monkeypatch):
    store, a, b = _store(tmp_path)
    monkeypatch.setenv("MAGNOLIA_CONSOLIDATION_AUTO", "1")  # after fixture: still pending

    res = auto_apply_band(str(store))

    assert res["applied"] == 1
    (r,) = _rows(store)
    assert r["verdict"] == "accept" and r["via"] == "auto"


def test_stale_dismissal_writes_label(tmp_path):
    store, a, b = _store(tmp_path)
    import pathlib
    pathlib.Path(a).unlink()
    pathlib.Path(b).unlink()

    res = apply_proposals(str(store), [0])

    assert res["dismissed"] == [0]
    (r,) = _rows(store)
    assert r["verdict"] == "dismiss_stale"


def test_backfill_imports_and_is_idempotent(tmp_path):
    store, a, b = _store(tmp_path)
    apply_proposals(str(store), [], reject=[0])     # labeled normally
    # Simulate a pre-label-store artifact state: wipe labels, restore pending.
    (store / "reflex" / "labels.jsonl").unlink()
    data = json.loads((store / "reflex" / "consolidation-proposal.json").read_text())
    data["rejected"] = []
    data["rejected_keys"] = []
    data["proposals"].append({
        "confidence": 0.7, "rationale": "old queue reject",
        "sources": [str(store / "staging" / "x.md"),
                    str(store / "staging" / "y.md")],
        "canonical": str(store / "staging" / "x.md"),
        "merged_preview": {}})
    data["rejected"] = [1]
    (store / "reflex" / "consolidation-proposal.json").write_text(json.dumps(data))

    res = backfill_labels(str(store))
    assert res["written"] == 1
    (r,) = _rows(store)
    assert r["verdict"] == "reject" and r["via"] == "human"
    assert r["cluster_key"] == ["x.md", "y.md"]
    assert r["backfilled"] is True

    res2 = backfill_labels(str(store))
    assert res2["written"] == 0                     # content-key idempotent
    assert len(_rows(store)) == 1


def test_backfill_marks_auto_from_receipts(tmp_path, monkeypatch):
    monkeypatch.setenv("MAGNOLIA_CONSOLIDATION_AUTO", "1")
    store, a, b = _store(tmp_path, conf=0.9)
    auto_apply_band(str(store))
    (store / "reflex" / "labels.jsonl").unlink()    # pretend it never existed
    data = json.loads((store / "reflex" / "consolidation-proposal.json").read_text())
    data["applied"] = [0]

    backfill_labels(str(store))

    (r,) = _rows(store)
    assert r["via"] == "auto" and r["backfilled"] is True
