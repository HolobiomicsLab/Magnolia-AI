"""One process-safe writer lease for a project's durable memory store."""

from __future__ import annotations

import asyncio
import contextvars
import fcntl
import os
import stat
from contextlib import contextmanager
from functools import wraps
from pathlib import Path
from typing import Any, Awaitable, Callable, Iterator


_ACTIVE_PROJECTS: contextvars.ContextVar[frozenset[Path]] = contextvars.ContextVar(
    "magnolia_writer_lease_projects", default=frozenset()
)


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


def _store_is_read_only(project: Path) -> bool:
    """Return whether an existing store has no write bits for any principal."""
    store = project / ".magnolia"
    try:
        return store.is_dir() and not stat.S_IMODE(store.stat().st_mode) & 0o222
    except OSError:
        return False


def acquire_project_writer_lease(project_dir: str | Path) -> tuple[Path, int]:
    """Acquire the canonical project lock and return its project and descriptor."""
    project = project_from_store(project_dir)
    store = project / ".magnolia"
    store.mkdir(mode=0o700, parents=True, exist_ok=True)
    path = store.resolve() / ".writer.lock"
    flags = os.O_RDWR | os.O_CREAT | getattr(os, "O_NOFOLLOW", 0)
    descriptor = os.open(path, flags, 0o600)
    try:
        if not stat.S_ISREG(os.fstat(descriptor).st_mode):
            raise RuntimeError("memory writer lease is not a regular file")
        os.fchmod(descriptor, 0o600)
        fcntl.flock(descriptor, fcntl.LOCK_EX)
    except BaseException:
        os.close(descriptor)
        raise
    return project, descriptor


def release_project_writer_lease(descriptor: int) -> None:
    """Release and close one acquired project writer descriptor."""
    try:
        fcntl.flock(descriptor, fcntl.LOCK_UN)
    finally:
        os.close(descriptor)


@contextmanager
def project_writer_lease(project_dir: str | Path) -> Iterator[None]:
    """Hold the shared lease, nesting safely within an already leased call."""
    project = project_from_store(project_dir)
    active = _ACTIVE_PROJECTS.get()
    if project in active:
        yield
        return
    if _store_is_read_only(project):
        yield
        return
    project, descriptor = acquire_project_writer_lease(project)
    token = _ACTIVE_PROJECTS.set(active | {project})
    try:
        yield
    finally:
        _ACTIVE_PROJECTS.reset(token)
        release_project_writer_lease(descriptor)


async def _finish_despite_cancellation(task: asyncio.Task[Any]) -> Any:
    """Wait for one owned task even when its caller receives repeated cancellation."""
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
    """Run an async operation under a lease that survives caller cancellation."""
    project = project_from_store(project_dir)
    if project in _ACTIVE_PROJECTS.get():
        return await operation()
    if _store_is_read_only(project):
        return await operation()
    acquisition = asyncio.create_task(asyncio.to_thread(acquire_project_writer_lease, project))
    try:
        project, descriptor = await asyncio.shield(acquisition)
    except asyncio.CancelledError:
        _project, descriptor = await _finish_despite_cancellation(acquisition)
        release = asyncio.create_task(
            asyncio.to_thread(release_project_writer_lease, descriptor)
        )
        await _finish_despite_cancellation(release)
        raise
    token = _ACTIVE_PROJECTS.set(_ACTIVE_PROJECTS.get() | {project})
    call = asyncio.create_task(operation())
    cancelled = False
    try:
        try:
            return await asyncio.shield(call)
        except asyncio.CancelledError:
            cancelled = True
            await _finish_despite_cancellation(call)
            raise
    finally:
        _ACTIVE_PROJECTS.reset(token)
        release = asyncio.create_task(
            asyncio.to_thread(release_project_writer_lease, descriptor)
        )
        await _finish_despite_cancellation(release)
        if cancelled and not call.done():
            raise RuntimeError("cancelled memory writer did not finish")


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
