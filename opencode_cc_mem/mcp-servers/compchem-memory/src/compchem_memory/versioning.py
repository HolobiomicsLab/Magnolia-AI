"""Local-first git versioning of the learning tiers (entries/, staging/).

Git the local tool, never GitHub the service — every operation here is fully
offline and never contacts a remote. The .magnolia store gets its own nested git
repo tracking only the knowledge tiers; all plumbing is gitignored.

The idea of version-controlling the learning is inspired by a discussion with
Madina Bekbergenova.
"""

import subprocess
from pathlib import Path

# Ignore everything, then re-include only the knowledge tiers and this file.
# Robust against new plumbing directories appearing later.
_GITIGNORE = "/*\n!/.gitignore\n!/entries/\n!/staging/\n"


def _git(store: Path, *args: str) -> subprocess.CompletedProcess:
    # check=False is intentional: git failures (e.g. git missing) surface to the
    # caller via is_repo()/exit codes rather than raising mid-operation.
    return subprocess.run(
        ["git", "-C", str(store), *args],
        capture_output=True, text=True,
    )


def is_repo(store: str | Path) -> bool:
    return (Path(store) / ".git").is_dir()


def ensure_repo(store: str | Path) -> None:
    """Initialise the nested repo if absent and keep its .gitignore current.
    Sets a local identity so commits never depend on global git config."""
    store = Path(store)
    store.mkdir(parents=True, exist_ok=True)
    if not is_repo(store):
        _git(store, "init", "-q")
        _git(store, "config", "user.name", "magnolia")
        _git(store, "config", "user.email", "magnolia@localhost")
    gi = store / ".gitignore"
    if not gi.exists() or gi.read_text() != _GITIGNORE:
        gi.write_text(_GITIGNORE)


def commit_all(store: Path, message: str) -> str | None:
    """Stage the tracked tiers and commit if anything changed. Returns the new
    commit hash, or None when there is nothing to commit (a clean tree, or only
    gitignored plumbing changed). Never raises on a clean tree."""
    store = Path(store)
    ensure_repo(store)
    _git(store, "add", "-A")
    # `git diff --cached --quiet` exits 0 when nothing is staged, 1 when there is.
    if _git(store, "diff", "--cached", "--quiet").returncode == 0:
        return None
    # A failed commit (e.g. an index.lock race) must not be reported as success:
    # rev-parse would otherwise return the stale prior HEAD, or the literal "HEAD"
    # on a repo with no commits yet.
    if _git(store, "commit", "-q", "-m", message).returncode != 0:
        return None
    rev = _git(store, "rev-parse", "HEAD").stdout.strip()
    return rev if len(rev) in (40, 64) else None
