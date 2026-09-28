"""C2 guard: retrieval must be read-only over the memory store.

Runs the same entry point the action-retrieval plugin uses
(python -m compchem_memory.quick_search) against a temp store and asserts every
entry file is byte-identical afterwards.
"""

import hashlib
import subprocess
import sys
from pathlib import Path

import yaml

import compchem_memory
from compchem_memory.storage import ensure_project_store


def _hash_tree(root: Path) -> dict[str, str]:
    out: dict[str, str] = {}
    for tier in ("entries", "staging"):
        d = root / ".magnolia" / tier
        for f in sorted(d.glob("*.md")):
            out[f"{tier}/{f.name}"] = hashlib.sha256(f.read_bytes()).hexdigest()
    return out


def _write_entry(staging: Path, name: str, title: str):
    fm = {
        "title": title,
        "type": "error_resolution",
        "tags": ["haddock3"],
        "confidence": 0.9,
        "observation_count": 1,
    }
    (staging / name).write_text("---\n" + yaml.dump(fm) + "---\n\nbody\n")


def test_quick_search_does_not_mutate_store(tmp_path):
    pd = tmp_path / "proj"
    ensure_project_store(str(pd))
    staging = pd / ".magnolia" / "staging"
    _write_entry(staging, "20260601_100000_a.md", "haddock3 timeout on first attempt")
    _write_entry(staging, "20260601_100100_b.md", "p2rank pocket picking notes")
    before = _hash_tree(pd)

    src = Path(compchem_memory.__file__).parent.parent
    env = {**dict(__import__("os").environ), "PYTHONPATH": str(src)}
    r = subprocess.run(
        [
            sys.executable, "-m", "compchem_memory.quick_search",
            "haddock3 timeout", "--project-dir", str(pd), "--k", "5",
        ],
        capture_output=True, text=True, env=env, timeout=60,
    )
    assert r.returncode == 0, r.stderr
    assert _hash_tree(pd) == before, "retrieval must not mutate entry files"
