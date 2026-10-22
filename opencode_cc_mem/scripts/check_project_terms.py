#!/usr/bin/env python3
"""Block known project terms from entering the public repository.

The deny list is private. Set PROJECT_TERMS_DENY_FILE to a local file, or
PROJECT_TERMS_DENY in CI. The plaintext terms are never stored in this repo.
"""

from __future__ import annotations

import argparse
import hashlib
import os
import re
import subprocess
import sys
from pathlib import Path

TOKEN_RE = re.compile(r"[a-z0-9]+")
SUB_LENGTHS = range(4, 10)
MAX_BYTES = 2_000_000
BINARY_PROBE = 8192
DEFAULT_DENY_FILE = Path.home() / ".config" / "magnolia" / "project-terms-deny.txt"


def load_deny() -> set[str]:
    raw = ""
    path = os.environ.get("PROJECT_TERMS_DENY_FILE")
    if path:
        p = Path(path)
        if not p.is_file():
            raise SystemExit(f"deny file not found: {p}")
        raw = p.read_text(encoding="utf-8", errors="replace")
    elif os.environ.get("PROJECT_TERMS_DENY"):
        raw = os.environ["PROJECT_TERMS_DENY"]
    elif DEFAULT_DENY_FILE.is_file():
        raw = DEFAULT_DENY_FILE.read_text(encoding="utf-8", errors="replace")
    else:
        raise SystemExit(
            "no private deny list: set PROJECT_TERMS_DENY_FILE or PROJECT_TERMS_DENY"
        )

    terms = [
        line.strip().lower()
        for line in raw.splitlines()
        if line.strip() and not line.lstrip().startswith("#")
    ]
    if not terms:
        raise SystemExit("private deny list is empty")
    return {hashlib.sha256(t.encode()).hexdigest() for t in terms}


def is_binary(data: bytes) -> bool:
    return b"\0" in data[:BINARY_PROBE]


def scan_path(path: str, deny: set[str]) -> list[str]:
    for token in TOKEN_RE.findall(path.lower()):
        for length in SUB_LENGTHS:
            if len(token) < length:
                break
            for i in range(len(token) - length + 1):
                h = hashlib.sha256(token[i:i + length].encode()).hexdigest()
                if h in deny:
                    return [f"{path}: path contains a forbidden project term (hash {h[:10]})"]
    return []


def scan_text(text: str, label: str, deny: set[str]) -> list[str]:
    hits: list[str] = []
    for line_no, line in enumerate(text.lower().splitlines(), 1):
        hit = False
        for token in TOKEN_RE.findall(line):
            if hit:
                break
            for length in SUB_LENGTHS:
                if len(token) < length:
                    break
                for i in range(len(token) - length + 1):
                    h = hashlib.sha256(token[i:i + length].encode()).hexdigest()
                    if h in deny:
                        hits.append(f"{label}:{line_no}: forbidden project term (hash {h[:10]})")
                        hit = True
                        break
                if hit:
                    break
    return hits


def scan_bytes(data: bytes, label: str, deny: set[str]) -> list[str]:
    if is_binary(data):
        return []
    if len(data) > MAX_BYTES:
        print(f"WARN: skipping large text file: {label}", file=sys.stderr)
        return []
    return scan_text(data.decode("utf-8", errors="replace"), label, deny)


def tracked_files() -> list[str]:
    out = subprocess.run(["git", "ls-files", "-z"], check=True, capture_output=True)
    return [p for p in out.stdout.decode("utf-8", errors="replace").split("\0") if p]


def staged_files() -> list[tuple[str, bytes]]:
    out = subprocess.run(
        ["git", "diff", "--cached", "--name-only", "--diff-filter=ACMR", "-z"],
        check=True,
        capture_output=True,
    )
    paths = [p for p in out.stdout.decode("utf-8", errors="replace").split("\0") if p]
    result: list[tuple[str, bytes]] = []
    for path in paths:
        blob = subprocess.run(["git", "show", f":{path}"], check=True, capture_output=True)
        result.append((path, blob.stdout))
    return result


def main() -> int:
    ap = argparse.ArgumentParser(
        description="Block known project terms from the public repository."
    )
    group = ap.add_mutually_exclusive_group()
    group.add_argument("--all", action="store_true", help="scan all tracked files")
    group.add_argument("--staged", action="store_true", help="scan staged content")
    group.add_argument("--file", nargs="+", help="scan specific files")
    args = ap.parse_args()

    deny = load_deny()
    hits: list[str] = []
    scanned = 0

    if args.file:
        for name in args.file:
            p = Path(name)
            if not p.is_file():
                print(f"WARN: not a file: {name}", file=sys.stderr)
                continue
            scanned += 1
            hits.extend(scan_path(name, deny))
            hits.extend(scan_bytes(p.read_bytes(), name, deny))
    elif args.staged:
        for path, data in staged_files():
            scanned += 1
            hits.extend(scan_path(path, deny))
            hits.extend(scan_bytes(data, path, deny))
    else:
        for path in tracked_files():
            p = Path(path)
            if not p.exists():
                continue
            scanned += 1
            hits.extend(scan_path(path, deny))
            hits.extend(scan_bytes(p.read_bytes(), path, deny))

    for hit in hits:
        print(hit)
    print(f"scanned {scanned} file(s); matches: {len(hits)}")
    return 1 if hits else 0


if __name__ == "__main__":
    raise SystemExit(main())
