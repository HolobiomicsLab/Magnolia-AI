# tests/test_reflex_common.py
import json
from pathlib import Path
import yaml
from compchem_memory.reflex_common import (
    parse_frontmatter_file, pending_indices, prior_rejected_keys, fenced_preview,
)


def _write(p, meta, body):
    p.write_text("---\n" + yaml.dump(meta) + "---\n\n" + body + "\n")
    return str(p)


def test_parse_frontmatter_file_reads_meta_and_body(tmp_path):
    p = tmp_path / "a.md"
    _write(p, {"title": "T", "type": "note"}, "the body")
    e = parse_frontmatter_file(p)
    assert e["id"] == "a.md"
    assert e["meta"]["title"] == "T"
    assert e["body"] == "the body"


def test_parse_frontmatter_file_missing_returns_none(tmp_path):
    assert parse_frontmatter_file(tmp_path / "nope.md") is None


def test_pending_indices_excludes_applied_and_rejected():
    data = {"proposals": [0, 1, 2, 3], "applied": [1], "rejected": [3]}
    assert pending_indices(data) == [0, 2]


def test_prior_rejected_keys_uses_key_extractor(tmp_path):
    art = tmp_path / "art.json"
    art.write_text(json.dumps({
        "proposals": [{"src": "x.md"}, {"src": "y.md"}],
        "rejected": [1],
    }))
    keys = prior_rejected_keys(art, lambda p: p["src"])
    assert keys == {"y.md"}


def test_prior_rejected_keys_missing_artifact_is_empty(tmp_path):
    assert prior_rejected_keys(tmp_path / "none.json", lambda p: p) == set()


def test_fenced_preview_truncates_and_tilde_fences():
    block = fenced_preview("```\ncode\n```\n" + "x" * 2000, limit=1500)
    text = "\n".join(block)
    assert "~~~~" in text
    assert "(truncated)" in text
