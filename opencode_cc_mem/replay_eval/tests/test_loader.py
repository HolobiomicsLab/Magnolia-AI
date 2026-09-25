import json
from pathlib import Path

from replay_eval import loader


def _make_corpus(tmp_path, n=2):
    gold = tmp_path / "corpus" / "extraction_gold"
    (gold / "slices").mkdir(parents=True)
    entries = []
    for i in range(n):
        name = f"s{i}"
        (gold / "slices" / f"{name}.txt").write_text(f"transcript {i} " * 50,
                                                     encoding="utf-8")
        entries.append({"name": name, "project": "p", "session": f"ses_{i}",
                        "chars": 650})
    (gold / "slices_manifest.json").write_text(json.dumps(entries))
    (tmp_path / "corpus" / "manifest.yaml").write_text(
        "corpus_version: 1\ncreated: 2026-09-25\nfrozen: true\n"
        "changes:\n  - version: 1\n    date: 2026-09-25\n"
        "    reason: test corpus\n")
    return tmp_path / "corpus"


def test_load_corpus_ok(tmp_path):
    corpus = loader.load_corpus(_make_corpus(tmp_path))
    assert len(corpus.slices) == 2
    assert corpus.slices[0].capture_version  # capture_version always present
    assert corpus.manifest["frozen"] is True


def test_missing_slice_raises(tmp_path):
    cdir = _make_corpus(tmp_path)
    (cdir / "extraction_gold" / "slices" / "s0.txt").unlink()
    try:
        loader.load_corpus(cdir)
        raise AssertionError("expected FileNotFoundError")
    except FileNotFoundError:
        pass


def test_no_reason_raises(tmp_path):
    cdir = _make_corpus(tmp_path)
    (cdir / "manifest.yaml").write_text("frozen: true\nchanges: []\n")
    try:
        loader.load_corpus(cdir)
        raise AssertionError("expected ValueError")
    except ValueError:
        pass


def test_retrieval_pairs(tmp_path):
    cdir = _make_corpus(tmp_path)
    rdir = cdir / "retrieval"
    rdir.mkdir()
    (rdir / "pairs.jsonl").write_text(
        '{"id": "q1", "query": "x", "expected": ["e1"]}\n'
        '\n# comment\n'
        '{"id": "q2", "query": "y", "expected": ["e2"]}\n', encoding="utf-8")
    pairs = loader.load_retrieval_pairs(cdir)
    assert [p["id"] for p in pairs] == ["q1", "q2"]
    bad = rdir / "pairs.jsonl"
    bad.write_text('{"id": "q9", "query": "z"}\n', encoding="utf-8")
    try:
        loader.load_retrieval_pairs(cdir)
        raise AssertionError("expected ValueError")
    except ValueError:
        pass
