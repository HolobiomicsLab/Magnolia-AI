"""Run-owned, policy-confined read-only MCP server for governed Codex roles."""

from __future__ import annotations

import asyncio
import codecs
import fcntl
import hashlib
import hmac
import multiprocessing
import os
import re
import stat
import sys
import time
from pathlib import Path
from typing import BinaryIO, Callable, Mapping

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
    MAX_SEARCH_TOTAL_BYTES = 4 * 1024 * 1024
    MAX_SEARCH_SECONDS = 2.0
    MAX_PATTERN_LENGTH = 256

    def __init__(
        self,
        policy: RequiredPolicy,
        *,
        cwd: Path,
        policy_path: Path | None = None,
        policy_digest: str | None = None,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        if policy.role not in {"worker", "reviewer"}:
            raise ReadRefused("magnolia-read is restricted to worker and reviewer policies")
        canonical_cwd = cwd.resolve()
        if not cwd.is_absolute() or cwd != canonical_cwd:
            raise ReadRefused("an explicit canonical cwd is required")
        self.policy = policy
        self.cwd = canonical_cwd
        self.policy_path = policy_path
        self.policy_digest = policy_digest
        self.clock = clock or time.monotonic

    @classmethod
    def from_environment(
        cls, env: Mapping[str, str] | None = None, *, cwd: Path | None = None
    ) -> "ReadServer":
        """Build a server from the required policy named in its environment."""
        values = os.environ if env is None else env
        policy_path = values.get("MAGNOLIA_HOOK_POLICY")
        if not policy_path:
            raise ReadRefused("MAGNOLIA_HOOK_POLICY is required")
        policy_digest = values.get("MAGNOLIA_HOOK_POLICY_SHA256")
        if not policy_digest:
            raise ReadRefused("MAGNOLIA_HOOK_POLICY_SHA256 is required")
        return cls.from_policy_file(
            policy_path,
            cwd=cwd,
            expected_sha256=policy_digest,
            control_dir=values.get("MAGNOLIA_CONTROL_DIR"),
        )

    @classmethod
    def from_policy_file(
        cls,
        path: str | Path,
        *,
        cwd: Path | None = None,
        expected_sha256: str | None = None,
        control_dir: str | None = None,
        clock: Callable[[], float] = time.monotonic,
    ) -> "ReadServer":
        """Load and validate one immutable run policy before serving reads."""
        if cwd is None:
            raise ReadRefused("an explicit canonical cwd is required")
        canonical_cwd = cwd.resolve()
        if not cwd.is_absolute() or cwd != canonical_cwd:
            raise ReadRefused("an explicit canonical cwd is required")
        try:
            policy = load_required_policy(
                path,
                expected_sha256=expected_sha256,
                control_dir=control_dir,
            )
        except (OSError, TypeError, ValueError) as exc:
            raise ReadRefused(f"invalid MAGNOLIA_HOOK_POLICY: {exc}") from exc
        return cls(
            policy,
            cwd=canonical_cwd,
            policy_path=Path(path) if expected_sha256 is not None else None,
            policy_digest=expected_sha256,
            clock=clock,
        )

    def _verify_policy_integrity(self) -> None:
        if self.policy_path is None or self.policy_digest is None:
            return
        try:
            current = hashlib.sha256(self.policy_path.read_bytes()).hexdigest()
        except OSError as exc:
            raise ReadRefused(f"policy digest verification failed: {exc}") from exc
        if not hmac.compare_digest(current, self.policy_digest):
            raise ReadRefused("policy digest mismatch after server startup")

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
        self._verify_policy_integrity()
        _bounded_int("offset", offset, minimum=0, maximum=self.MAX_READ_OFFSET)
        _bounded_int("limit", limit, minimum=1, maximum=self.MAX_READ_LINES)
        target = self._path(path)
        try:
            with self._open_regular(target, path) as handle:
                _validate_text_prefix(handle, self.MAX_READ_BYTES)
                body, truncated = _read_lines(
                    handle, offset, limit, self.MAX_READ_BYTES
                )
        except OSError as exc:
            raise ReadRefused(f"cannot read {path!r}: {exc}") from exc
        content = _decode_text_prefix(body, truncated)
        return {
            "path": path,
            "offset": offset,
            "content": content,
            "bytes": len(body),
            "truncated": truncated,
        }

    def list_dir(self, path: str) -> dict[str, object]:
        """List allowed direct children without exposing denied entries."""
        self._verify_policy_integrity()
        target = self._path(path)
        entries: list[str] = []
        descriptor = self._open_directory(target, path)
        try:
            names = sorted(os.listdir(descriptor), key=str.casefold)
            if len(names) > self.MAX_LIST_ENTRIES:
                raise ReadRefused("directory exceeds the bounded listing limit")
            for name in names:
                child = target / name
                if read_target_allowed(str(child), self.cwd, self.policy):
                    mode = os.stat(
                        name,
                        dir_fd=descriptor,
                        follow_symlinks=False,
                    ).st_mode
                    entries.append(name + ("/" if stat.S_ISDIR(mode) else ""))
        except ReadRefused:
            raise
        except OSError as exc:
            raise ReadRefused(f"cannot list {path!r}: {exc}") from exc
        finally:
            os.close(descriptor)
        return {"path": path, "entries": entries, "truncated": False}

    def search(self, pattern: str, path: str) -> dict[str, object]:
        """Return time- and size-bounded literal or isolated-regex matches."""
        self._verify_policy_integrity()
        expression, regex = self._expression(pattern)
        deadline = self.clock() + self.MAX_SEARCH_SECONDS
        target = self._path(path, broad=True)
        if not target.exists():
            raise ReadRefused(f"search target does not exist: {path!r}")
        try:
            records, reasons = self._search_files(target, deadline)
        except ReadRefused:
            raise
        except (OSError, RuntimeError) as exc:
            raise ReadRefused(f"cannot search {path!r}: {exc}") from exc
        matches, match_reason = self._match_records(
            records, expression, regex, deadline
        )
        if match_reason:
            reasons.append(match_reason)
        if len(matches) > self.MAX_SEARCH_RESULTS:
            matches = matches[: self.MAX_SEARCH_RESULTS]
            reasons.append("result limit exceeded")
        return {
            "pattern": pattern,
            "regex": regex,
            "matches": matches,
            "truncated": bool(reasons),
            "reason": "; ".join(dict.fromkeys(reasons)) or None,
        }

    def _search_files(
        self, target: Path, deadline: float
    ) -> tuple[list[dict[str, object]], list[str]]:
        records: list[dict[str, object]] = []
        reasons: list[str] = []
        scanned_bytes = 0
        scanned_files = 0
        binary_files = 0
        files = target.rglob("*") if target.is_dir() else (target,)
        for candidate in files:
            if self.clock() >= deadline:
                reasons.append("time limit exceeded")
                break
            if candidate.is_dir():
                continue
            scanned_files += 1
            if scanned_files > self.MAX_SEARCH_FILES:
                reasons.append("file limit exceeded")
                break
            if not read_target_allowed(str(candidate), self.cwd, self.policy):
                raise ReadRefused(f"policy denied search descendant: {candidate}")
            with self._open_regular(candidate, str(candidate)) as handle:
                found, consumed, file_reasons, is_binary = self._scan_file(
                    candidate,
                    handle,
                    self.MAX_SEARCH_TOTAL_BYTES - scanned_bytes,
                    deadline,
                )
            if is_binary:
                binary_files += 1
            else:
                records.extend(found)
            scanned_bytes += consumed
            reasons.extend(file_reasons)
            if scanned_bytes >= self.MAX_SEARCH_TOTAL_BYTES:
                reasons.append("total byte limit exceeded")
                break
        if binary_files:
            reasons.append(f"binary files skipped: {binary_files}")
        return records, reasons

    def _scan_file(
        self,
        path: Path,
        handle: BinaryIO,
        byte_budget: int,
        deadline: float,
    ) -> tuple[list[dict[str, object]], int, list[str], bool]:
        records: list[dict[str, object]] = []
        reasons: list[str] = []
        consumed = 0
        line_number = 0
        decoder = codecs.getincrementaldecoder("utf-8")()
        reached_eof = False
        while consumed < byte_budget and self.clock() < deadline:
            line_number += 1
            raw = handle.readline(min(self.MAX_SEARCH_LINE_BYTES + 1, byte_budget - consumed))
            if not raw:
                reached_eof = True
                break
            consumed += len(raw)
            is_long = len(raw) > self.MAX_SEARCH_LINE_BYTES and not raw.endswith(b"\n")
            prefix = raw[: self.MAX_SEARCH_LINE_BYTES]
            if _is_binary_chunk(raw, decoder):
                return [], consumed, reasons, True
            while is_long and consumed < byte_budget:
                chunk = handle.readline(min(self.MAX_SEARCH_LINE_BYTES + 1, byte_budget - consumed))
                if not chunk:
                    reached_eof = True
                    break
                consumed += len(chunk)
                if _is_binary_chunk(chunk, decoder):
                    return [], consumed, reasons, True
                if chunk.endswith(b"\n"):
                    break
            if is_long:
                reasons.append("line length limit exceeded")
            records.append({
                "path": _shown_path(path, self.cwd),
                "line": line_number,
                "text": prefix.decode("utf-8", errors="replace").rstrip("\r\n"),
            })
        if reached_eof and _is_incomplete_utf8(decoder):
            return [], consumed, reasons, True
        if self.clock() >= deadline:
            reasons.append("time limit exceeded")
        return records, consumed, reasons, False

    def _expression(self, pattern: str) -> tuple[str, bool]:
        if not isinstance(pattern, str) or not pattern or len(pattern) > self.MAX_PATTERN_LENGTH:
            raise ReadRefused("search pattern must be non-empty and bounded")
        regex = pattern.startswith("re:")
        expression = pattern[3:] if regex else pattern
        if regex and (
            any(marker in expression for marker in ("(", ")", "{", "}"))
            or re.search(r"\\[1-9]", expression)
        ):
            raise ReadRefused("search regex uses unsupported complex constructs")
        if regex:
            try:
                re.compile(expression)
            except re.error as exc:
                raise ReadRefused(f"invalid search regex: {exc}") from exc
        return expression, regex

    def _match_records(
        self,
        records: list[dict[str, object]],
        expression: str,
        regex: bool,
        deadline: float,
    ) -> tuple[list[dict[str, object]], str | None]:
        if not regex:
            return [row for row in records if expression in str(row["text"])], None
        remaining = deadline - self.clock()
        if remaining <= 0:
            return [], "time limit exceeded"
        indices, reason = _regex_indices(
            expression,
            [str(row["text"]) for row in records],
            min(remaining, self.MAX_SEARCH_SECONDS),
            self.MAX_SEARCH_RESULTS + 1,
        )
        return [records[index] for index in indices], reason

    def _open_regular(self, path: Path, shown: str) -> BinaryIO:
        flags = os.O_RDONLY | os.O_NONBLOCK
        flags |= getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0)
        try:
            descriptor = os.open(path, flags)
        except OSError as exc:
            raise ReadRefused(f"cannot read {shown!r}: {exc}") from exc
        try:
            if not stat.S_ISREG(os.fstat(descriptor).st_mode):
                raise ReadRefused(f"read target is not a regular file: {shown!r}")
            opened_path = _descriptor_path(descriptor)
            if not read_target_allowed(opened_path, self.cwd, self.policy):
                raise ReadRefused(f"policy denied opened file: {shown!r}")
            return os.fdopen(descriptor, "rb", closefd=True)
        except BaseException:
            os.close(descriptor)
            raise

    def _open_directory(self, path: Path, shown: str) -> int:
        flags = os.O_RDONLY | os.O_NONBLOCK
        flags |= getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0)
        flags |= getattr(os, "O_DIRECTORY", 0)
        try:
            descriptor = os.open(path, flags)
        except OSError as exc:
            raise ReadRefused(f"cannot list {shown!r}: {exc}") from exc
        try:
            if not stat.S_ISDIR(os.fstat(descriptor).st_mode):
                raise ReadRefused(f"list target is not a directory: {shown!r}")
            opened_path = _descriptor_path(descriptor)
            if not read_target_allowed(opened_path, self.cwd, self.policy):
                raise ReadRefused(f"policy denied opened directory: {shown!r}")
            return descriptor
        except BaseException:
            os.close(descriptor)
            raise


def _descriptor_path(descriptor: int) -> str:
    """Return the path of the opened descriptor on macOS or Linux."""
    if sys.platform == "darwin":
        size = 1024
        try:
            value = fcntl.fcntl(descriptor, 50, b"\0" * size)
        except (OSError, ValueError) as exc:
            raise ReadRefused("cannot verify opened file path") from exc
        path = value.split(b"\0", 1)[0].decode(errors="surrogateescape")
    else:
        try:
            path = os.readlink(f"/proc/self/fd/{descriptor}")
        except OSError as exc:
            raise ReadRefused("cannot verify opened file path") from exc
    if not path or path.endswith(" (deleted)"):
        raise ReadRefused("cannot verify opened file path")
    try:
        opened = os.fstat(descriptor)
        named = os.stat(path)
    except OSError as exc:
        raise ReadRefused("cannot verify opened file identity") from exc
    if (opened.st_dev, opened.st_ino) != (named.st_dev, named.st_ino):
        raise ReadRefused("opened file identity changed during verification")
    return path


def _regex_worker(connection, expression: str, lines: list[str], limit: int) -> None:
    """Evaluate bounded regex input outside the stdio server process."""
    try:
        matcher = re.compile(expression).search
        matches: list[int] = []
        for index, line in enumerate(lines):
            if matcher(line):
                matches.append(index)
                if len(matches) >= limit:
                    break
        connection.send((matches, os.path.realpath(__file__)))
    except BaseException as exc:
        connection.send(exc)
    finally:
        connection.close()


def _regex_indices(
    expression: str, lines: list[str], timeout: float, limit: int
) -> tuple[list[int], str | None]:
    context = multiprocessing.get_context("spawn")
    receiver, sender = context.Pipe(duplex=False)
    process = context.Process(
        target=_regex_worker,
        args=(sender, expression, lines, limit),
        daemon=True,
    )
    process.start()
    sender.close()
    try:
        if not receiver.poll(timeout):
            process.terminate()
            process.join(timeout=1)
            return [], "regex time limit exceeded"
        result = receiver.recv()
        process.join(timeout=1)
    finally:
        receiver.close()
        if process.is_alive():
            process.kill()
            process.join(timeout=1)
    if isinstance(result, BaseException):
        raise ReadRefused(f"regex search failed: {result}")
    if (
        not isinstance(result, tuple)
        or len(result) != 2
        or result[1] != os.path.realpath(__file__)
    ):
        raise ReadRefused("regex worker did not load this repository source")
    return result[0], None


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


def _validate_text_prefix(handle: BinaryIO, max_bytes: int) -> None:
    """Classify one file from its bounded prefix before paging through it."""
    scanned = handle.read(max_bytes + 1)
    _decode_text_prefix(scanned[:max_bytes], len(scanned) > max_bytes)
    handle.seek(0)


def _decode_text_prefix(body: bytes, truncated: bool) -> str:
    """Decode a scanned prefix while refusing definite binary content."""
    if b"\x00" in body:
        raise ReadRefused("binary content refused: NUL byte in scanned prefix")
    try:
        return body.decode("utf-8")
    except UnicodeDecodeError as exc:
        if truncated and exc.reason == "unexpected end of data" and exc.end == len(body):
            return body[: exc.start].decode("utf-8")
        raise ReadRefused(
            "binary content refused: non-UTF-8 data in scanned prefix"
        ) from exc


def _is_binary_chunk(raw: bytes, decoder) -> bool:
    """Feed one search chunk into the file-wide binary detector."""
    if b"\x00" in raw:
        return True
    try:
        decoder.decode(raw, final=False)
    except UnicodeDecodeError:
        return True
    return False


def _is_incomplete_utf8(decoder) -> bool:
    """Return whether an EOF leaves an invalid UTF-8 suffix."""
    try:
        decoder.decode(b"", final=True)
    except UnicodeDecodeError:
        return True
    return False


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
async def read_file(
    path: str, offset: int = 0, limit: int = ReadServer.DEFAULT_READ_LIMIT
):
    """Read bounded lines from one policy-allowed file."""
    return await asyncio.to_thread(_active().read_file, path, offset, limit)


@mcp.tool()
async def list_dir(path: str):
    """List one policy-allowed directory with bounded output."""
    return await asyncio.to_thread(_active().list_dir, path)


@mcp.tool()
async def search(pattern: str, path: str):
    """Search literal text, or a conservative ``re:`` regex, under one root."""
    return await asyncio.to_thread(_active().search, pattern, path)


def main() -> None:
    """Load the run policy before starting the stdio MCP transport."""
    global _SERVER
    cwd = os.environ.get("MAGNOLIA_PROJECT_DIR")
    _SERVER = ReadServer.from_environment(cwd=Path(cwd) if cwd else None)
    mcp.run()


if __name__ == "__main__":
    main()
