# tests/test_versioning.py
from pathlib import Path
import subprocess

from compchem_memory import versioning


def _git(store, *args):
    return subprocess.run(["git", "-C", str(store), *args],
                          capture_output=True, text=True)


def test_ensure_repo_inits_nested_repo_with_identity(tmp_path):
    store = tmp_path / ".magnolia"
    versioning.ensure_repo(store)

    assert versioning.is_repo(store)
    assert (store / ".git").is_dir()
    # local identity is set so commits never depend on global config (HPC-safe)
    assert _git(store, "config", "user.name").stdout.strip() == "magnolia"
    assert _git(store, "config", "user.email").stdout.strip() == "magnolia@localhost"


def test_ensure_repo_repairs_corrupted_gitignore(tmp_path):
    store = tmp_path / ".magnolia"
    versioning.ensure_repo(store)
    (store / ".gitignore").write_text("bogus content")

    versioning.ensure_repo(store)  # must restore the canonical ignore rules

    assert (store / ".gitignore").read_text() == "/*\n!/.gitignore\n!/entries/\n!/staging/\n"


def test_ensure_repo_tracks_only_entries_and_staging(tmp_path):
    store = tmp_path / ".magnolia"
    (store / "entries").mkdir(parents=True)
    (store / "staging").mkdir(parents=True)
    (store / "sessions").mkdir(parents=True)
    versioning.ensure_repo(store)
    (store / "entries" / "a.md").write_text("keep me")
    (store / "staging" / "b.md").write_text("keep me")
    (store / "sessions" / "log.jsonl").write_text("ignore me")
    (store / "boot-context.md").write_text("ignore me")

    _git(store, "add", "-A")
    tracked = _git(store, "diff", "--cached", "--name-only").stdout.split()

    assert "entries/a.md" in tracked
    assert "staging/b.md" in tracked
    assert "sessions/log.jsonl" not in tracked
    assert "boot-context.md" not in tracked


def test_ensure_repo_is_idempotent(tmp_path):
    store = tmp_path / ".magnolia"
    versioning.ensure_repo(store)
    versioning.ensure_repo(store)  # must not raise or reinit
    assert versioning.is_repo(store)
