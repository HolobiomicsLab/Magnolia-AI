"""Run-owned, policy-confined read-only MCP server for governed Codex roles."""

from __future__ import annotations

import os
import re
from collections.abc import Callable
from pathlib import Path
from typing import BinaryIO, Mapping

from fastmcp import FastMCP

from compchem_memory.hook.policy import (
    RequiredPolicy,
    load_required_policy,
    read_target_allowed,
)


class ReadRefused(PermissionError):
    """A fail-closed read-server refusal suitable for an MCP tool error."""


class ReadServer:
    """Three bounded read operations sharing the required hook's path policy."""

    DEFAULT_READ_LIMIT = 200
    MAX_READ_OFFSET = 100_000
    MAX_READ_LINES = 1_000
    MAX_READ_BYTES = 64 * 1024
    MAX_LIST_ENTRIES = 1_000
    MAX_SEARCH_RESULTS = 200
    MAX_SEARCH_FILES = 10_000
    MAX_SEARCH_LINE_BYTES = 8 * 1024
    MAX_PATTERN_LENGTH = 256

    def __init__(self, policy: RequiredPolicy, *, cwd: Path) -> None:
        if policy.role not in {"worker", "reviewer"}:
            raise ReadRefused("magnolia-read is restricted to worker and reviewer policies")
        self.policy = policy
        self.cwd = cwd.resolve()

    @classmethod
    def from_environment(
        cls, env: Mapping[str, str] | None = None, *, cwd: Path | None = None
    ) -> "ReadServer":
        """Build a server from the required policy named in its environment."""
        values = os.environ if env is None else env
        policy_path = values.get("MAGNOLIA_HOOK_POLICY")
        if not policy_path:
            raise ReadRefused("MAGNOLIA_HOOK_POLICY is required")
        return cls.from_policy_file(policy_path, cwd=cwd)

    @classmethod
    def from_policy_file(
        cls, path: str | Path, *, cwd: Path | None = None
    ) -> "ReadServer":
        """Load and validate one immutable run policy before serving reads."""
        try:
            policy = load_required_policy(path)
        except (OSError, TypeError, ValueError) as exc:
            raise ReadRefused(f"invalid MAGNOLIA_HOOK_POLICY: {exc}") from exc
        return cls(policy, cwd=cwd or Path.cwd())

    def _path(self, value: str, *, broad: bool = False) -> Path:
        if not isinstance(value, str) or not read_target_allowed(
            value, self.cwd, self.policy, broad=broad
        ):
            raise ReadRefused(f"policy denied path: {value!r}")
        raw = Path(value)
        return raw if raw.is_absolute() else self.cwd / raw

    def read_file(
        self, path: str, offset: int = 0, limit: int = DEFAULT_READ_LIMIT
    ) -> dict[str, object]:
        """Return bounded lines from one allowed regular file."""
        _bounded_int("offset", offset, minimum=0, maximum=self.MAX_READ_OFFSET)
        _bounded_int("limit", limit, minimum=1, maximum=self.MAX_READ_LINES)
        target = self._path(path)
        if not target.is_file():
            raise ReadRefused(f"read target is not a regular file: {path!r}")
        try:
            with target.open("rb") as handle:
                body, truncated = _read_lines(
                    handle, offset, limit, self.MAX_READ_BYTES
                )
        except OSError as exc:
            raise ReadRefused(f"cannot read {path!r}: {exc}") from exc
        return {
            "path": path,
            "offset": offset,
            "content": body.decode("utf-8", errors="replace"),
            "bytes": len(body),
            "truncated": truncated,
        }

    def list_dir(self, path: str) -> dict[str, object]:
        """List allowed direct children without exposing denied entries."""
        target = self._path(path)
        if not target.is_dir():
            raise ReadRefused(f"list target is not a directory: {path!r}")
        entries: list[str] = []
        try:
            children = sorted(target.iterdir(), key=lambda item: item.name.casefold())
            if len(children) > self.MAX_LIST_ENTRIES:
                raise ReadRefused("directory exceeds the bounded listing limit")
            for child in children:
                if read_target_allowed(str(child), self.cwd, self.policy):
                    entries.append(child.name + ("/" if child.is_dir() else ""))
        except ReadRefused:
            raise
        except OSError as exc:
            raise ReadRefused(f"cannot list {path!r}: {exc}") from exc
        return {"path": path, "entries": entries, "truncated": False}

    def search(self, pattern: str, path: str) -> dict[str, object]:
        """Return bounded literal or conservative-regex matches under a root."""
        matcher, regex = self._matcher(pattern)
        target = self._path(path, broad=True)
        if not target.exists():
            raise ReadRefused(f"search target does not exist: {path!r}")
        files = [target] if target.is_file() else sorted(target.rglob("*"))
        if len(files) > self.MAX_SEARCH_FILES:
            raise ReadRefused("search exceeds the bounded file limit")
        try:
            matches, truncated = self._search_files(files, matcher)
        except ReadRefused:
            raise
        except (OSError, RuntimeError) as exc:
            raise ReadRefused(f"cannot search {path!r}: {exc}") from exc
        return {
            "pattern": pattern,
            "regex": regex,
            "matches": matches,
            "truncated": truncated,
        }

    def _search_files(
        self, files: list[Path], matcher: Callable[[str], object]
    ) -> tuple[list[dict[str, object]], bool]:
        matches: list[dict[str, object]] = []
        for candidate in files:
            if not candidate.is_file() or candidate.is_symlink():
                continue
            if not read_target_allowed(str(candidate), self.cwd, self.policy):
                raise ReadRefused(f"policy denied search descendant: {candidate}")
            matches.extend(self._search_file(candidate, matcher))
            if len(matches) >= self.MAX_SEARCH_RESULTS:
                return matches[: self.MAX_SEARCH_RESULTS], True
        return matches, False

    def _search_file(
        self, path: Path, matcher: Callable[[str], object]
    ) -> list[dict[str, object]]:
        matches: list[dict[str, object]] = []
        with path.open("rb") as handle:
            for line_number, raw in enumerate(handle, start=1):
                if b"\x00" in raw:
                    break
                text = raw[: self.MAX_SEARCH_LINE_BYTES].decode(
                    "utf-8", errors="replace"
                ).rstrip("\r\n")
                if matcher(text):
                    matches.append({
                        "path": _shown_path(path, self.cwd),
                        "line": line_number,
                        "text": text,
                    })
                if len(matches) >= self.MAX_SEARCH_RESULTS:
                    break
        return matches

    def _matcher(self, pattern: str) -> tuple[Callable[[str], object], bool]:
        if not isinstance(pattern, str) or not pattern or len(pattern) > self.MAX_PATTERN_LENGTH:
            raise ReadRefused("search pattern must be non-empty and bounded")
        regex = pattern.startswith("re:")
        expression = pattern[3:] if regex else re.escape(pattern)
        if regex and (
            any(marker in expression for marker in ("(", ")", "{", "}"))
            or re.search(r"\\[1-9]", expression)
        ):
            raise ReadRefused("search regex uses unsupported complex constructs")
        try:
            compiled = re.compile(expression)
        except re.error as exc:
            raise ReadRefused(f"invalid search regex: {exc}") from exc
        return compiled.search, regex


def _read_lines(
    handle: BinaryIO, offset: int, limit: int, max_bytes: int
) -> tuple[bytes, bool]:
    for _ in range(offset):
        if not _skip_line(handle, max_bytes):
            break
    lines: list[bytes] = []
    byte_count = 0
    for _ in range(limit):
        remaining = max_bytes - byte_count
        line = handle.readline(remaining + 1)
        if not line:
            break
        lines.append(line[:remaining])
        byte_count += min(len(line), remaining)
        if len(line) > remaining:
            return b"".join(lines), True
    body = b"".join(lines)
    return body, len(body) >= max_bytes or bool(handle.read(1))


def _skip_line(handle: BinaryIO, chunk_bytes: int) -> bool:
    found = False
    while chunk := handle.readline(chunk_bytes + 1):
        found = True
        if chunk.endswith(b"\n") or len(chunk) <= chunk_bytes:
            break
    return found


def _shown_path(path: Path, cwd: Path) -> str:
    try:
        return str(path.relative_to(cwd))
    except ValueError:
        return str(path)


def _bounded_int(
    name: str, value: int, *, minimum: int, maximum: int | None = None
) -> None:
    if isinstance(value, bool) or not isinstance(value, int) or value < minimum:
        raise ReadRefused(f"{name} must be an integer >= {minimum}")
    if maximum is not None and value > maximum:
        raise ReadRefused(f"{name} exceeds the maximum of {maximum}")


mcp = FastMCP("magnolia-read")
_SERVER: ReadServer | None = None


def _active() -> ReadServer:
    if _SERVER is None:
        raise ReadRefused("magnolia-read has not loaded its required policy")
    return _SERVER


@mcp.tool()
def read_file(path: str, offset: int = 0, limit: int = ReadServer.DEFAULT_READ_LIMIT):
    """Read bounded lines from one policy-allowed file."""
    return _active().read_file(path, offset, limit)


@mcp.tool()
def list_dir(path: str):
    """List one policy-allowed directory with bounded output."""
    return _active().list_dir(path)


@mcp.tool()
def search(pattern: str, path: str):
    """Search literal text, or a conservative ``re:`` regex, under one root."""
    return _active().search(pattern, path)


def main() -> None:
    """Load the run policy before starting the stdio MCP transport."""
    global _SERVER
    _SERVER = ReadServer.from_environment()
    mcp.run()


if __name__ == "__main__":
    main()
