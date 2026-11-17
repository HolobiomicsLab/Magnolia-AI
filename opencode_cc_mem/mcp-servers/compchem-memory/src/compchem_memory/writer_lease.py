"""One process-safe writer lease for a project's durable memory store.

The lease covers only short store critical sections: read-modify-write of
entries, run records, proposal artifacts, cursors and the version commit. It is
never held across an LLM, network or long subprocess call; long writers compute
first, then lease, re-validate what they read and write. Nothing takes it at
import or server start, and hooks never wait on it.
"""

from __future__ import annotations

import asyncio
import contextvars
import errno
import fcntl
import hashlib
import os
import stat
import time
from contextlib import contextmanager
from functools import wraps
from pathlib import Path
from typing import Any, Awaitable, Callable, Iterable, Iterator


_ACTIVE_PROJECTS: contextvars.ContextVar[frozenset[Path]] = contextvars.ContextVar(
    "magnolia_writer_lease_projects", default=frozenset()
)
# A genuinely read-only project or store has no lease to take: its writers fail
# on their own write exactly as they did before the lease existed.
_READ_ONLY_ERRORS = frozenset({errno.EACCES, errno.EPERM, errno.EROFS})
_POLL_FIRST_SECONDS = 0.002
_POLL_MAX_SECONDS = 0.05


def project_from_store(path: str | Path) -> Path:
    """Resolve either a project root or a path within its ``.magnolia`` store."""
    resolved = Path(path).expanduser().resolve()
    for candidate in (resolved, *resolved.parents):
        if candidate.name == ".magnolia":
            return candidate.parent
    return resolved.parent if resolved.is_file() else resolved


def has_project_writer_lease(project_dir: str | Path) -> bool:
    """Return whether the current execution context already owns this lease."""
    return project_from_store(project_dir) in _ACTIVE_PROJECTS.get()


def holds_any_project_writer_lease() -> bool:
    """Return whether the current execution context owns any project lease."""
    return bool(_ACTIVE_PROJECTS.get())


def _store_is_read_only(project: Path) -> bool:
    """Return whether the store, or its project before creation, is read-only."""
    store = project / ".magnolia"
    try:
        target = store if store.exists() else project
        return target.is_dir() and not stat.S_IMODE(target.stat().st_mode) & 0o222
    except OSError:
        return False


def _open_lock(project: Path) -> int | None:
    """Open the project's lock file, or return None for an unwritable project."""
    store = project / ".magnolia"
    try:
        store.mkdir(mode=0o700, parents=True, exist_ok=True)
        path = store.resolve() / ".writer.lock"
        flags = os.O_RDWR | os.O_CREAT | getattr(os, "O_NOFOLLOW", 0)
        descriptor = os.open(path, flags, 0o600)
    except OSError as error:
        if error.errno in _READ_ONLY_ERRORS and _store_is_read_only(project):
            return None
        raise
    try:
        if not stat.S_ISREG(os.fstat(descriptor).st_mode):
            raise RuntimeError("memory writer lease is not a regular file")
        os.fchmod(descriptor, 0o600)
    except BaseException:
        os.close(descriptor)
        raise
    return descriptor


def _try_lock(descriptor: int) -> bool:
    try:
        fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        return False
    return True


def acquire_project_writer_lease(project_dir: str | Path) -> tuple[Path, int]:
    """Block until the canonical project lock is held; return project and descriptor."""
    project = project_from_store(project_dir)
    descriptor = _open_lock(project)
    if descriptor is None:
        raise PermissionError(errno.EACCES, "project store is not writable", str(project))
    try:
        fcntl.flock(descriptor, fcntl.LOCK_EX)
    except BaseException:
        os.close(descriptor)
        raise
    return project, descriptor


def release_project_writer_lease(descriptor: int) -> None:
    """Release and close one acquired project writer descriptor (never blocks)."""
    try:
        fcntl.flock(descriptor, fcntl.LOCK_UN)
    finally:
        os.close(descriptor)


@contextmanager
def project_writer_lease(project_dir: str | Path) -> Iterator[None]:
    """Hold the shared lease for one short critical section, nesting safely."""
    project = project_from_store(project_dir)
    active = _ACTIVE_PROJECTS.get()
    if project in active or _store_is_read_only(project):
        yield
        return
    descriptor = _open_lock(project)
    if descriptor is None:
        yield
        return
    try:
        fcntl.flock(descriptor, fcntl.LOCK_EX)
    except BaseException:
        os.close(descriptor)
        raise
    token = _ACTIVE_PROJECTS.set(active | {project})
    try:
        yield
    finally:
        _ACTIVE_PROJECTS.reset(token)
        release_project_writer_lease(descriptor)


async def _finish_shielded(task: asyncio.Task[Any]) -> Any:
    """Wait for an owned task to finish whatever cancellation the caller receives.

    The anyio shield stops level-triggered scope cancellation from being
    re-delivered on every await (no spinning); a native ``Task.cancel`` is
    delivered once per call, so the loop below runs once per cancellation.
    """
    import anyio

    with anyio.CancelScope(shield=True):
        while not task.done():
            try:
                await asyncio.shield(task)
            except asyncio.CancelledError:
                continue
    return task.result()


async def run_with_project_writer_lease(
    project_dir: str | Path,
    operation: Callable[[], Awaitable[Any]],
) -> Any:
    """Run an async operation under the lease, safely across caller cancellation.

    Waiting polls a non-blocking lock with backoff on the event loop, so waiters
    never occupy executor threads and a cancelled waiter holds nothing. Once the
    lease is held, the operation finishes and the lease is released inline before
    any cancellation propagates.
    """
    project = project_from_store(project_dir)
    if project in _ACTIVE_PROJECTS.get() or _store_is_read_only(project):
        return await operation()
    descriptor = _open_lock(project)
    if descriptor is None:
        return await operation()
    try:
        delay = _POLL_FIRST_SECONDS
        while not _try_lock(descriptor):
            await asyncio.sleep(delay)
            delay = min(delay * 2, _POLL_MAX_SECONDS)
    except BaseException:
        os.close(descriptor)
        raise
    token = _ACTIVE_PROJECTS.set(_ACTIVE_PROJECTS.get() | {project})
    try:
        call = asyncio.create_task(operation())
    finally:
        _ACTIVE_PROJECTS.reset(token)
    try:
        try:
            return await asyncio.shield(call)
        except asyncio.CancelledError:
            await _finish_shielded(call)
            raise
    finally:
        release_project_writer_lease(descriptor)


def leased_store_writer(function: Callable[..., Any]) -> Callable[..., Any]:
    """Lease a writer whose first argument is a project or store path."""
    @wraps(function)
    def wrapped(store: str | Path, *args: Any, **kwargs: Any) -> Any:
        with project_writer_lease(project_from_store(store)):
            return function(store, *args, **kwargs)

    return wrapped


def leased_source_writer(function: Callable[..., Any]) -> Callable[..., Any]:
    """Lease a writer whose first argument is a sequence of store paths."""
    @wraps(function)
    def wrapped(sources: list[str], *args: Any, **kwargs: Any) -> Any:
        if not sources:
            return function(sources, *args, **kwargs)
        with project_writer_lease(project_from_store(sources[0])):
            return function(sources, *args, **kwargs)

    return wrapped


def leased_project_method(function: Callable[..., Any]) -> Callable[..., Any]:
    """Lease a manager method whose first argument is ``project_dir``."""
    @wraps(function)
    def wrapped(self: Any, *args: Any, **kwargs: Any) -> Any:
        project_dir = kwargs["project_dir"] if "project_dir" in kwargs else args[0]
        with project_writer_lease(project_dir):
            return function(self, *args, **kwargs)

    return wrapped


def file_fingerprint(paths: Iterable[str | Path]) -> dict[str, str | None]:
    """Hash the content of files a long writer read, for re-validation under the lease."""
    fingerprint: dict[str, str | None] = {}
    for path in paths:
        try:
            fingerprint[str(path)] = hashlib.sha256(Path(path).read_bytes()).hexdigest()
        except OSError:
            fingerprint[str(path)] = None
    return fingerprint


def fingerprint_unchanged(fingerprint: dict[str, str | None]) -> bool:
    """Return whether every fingerprinted file still has the content that was read."""
    return file_fingerprint(fingerprint) == fingerprint
