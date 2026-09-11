"""Shared pytest fixtures for compchem-tools tests."""
from __future__ import annotations

import os
import tempfile
from subprocess import CompletedProcess
from typing import Any

import pytest

# Hermetic tests: importing the server starts a background poller, and the
# @captured wrappers can fire inline LLM extraction. Both do real I/O
# (network/quota/stray project writes); disable them unless a test opts in.
os.environ.setdefault("MAGNOLIA_DISABLE_BACKGROUND_POLLER", "1")
os.environ.setdefault("MAGNOLIA_DISABLE_INLINE_EXTRACT", "1")

# The wrapper exports a cwd-relative project pin ("projects/<name>"); redirect
# it to a throwaway absolute dir so env-defaulted code paths cannot scaffold
# stores inside the package tree.
os.environ["MAGNOLIA_PROJECT_DIR"] = tempfile.mkdtemp(prefix="magnolia-tests-")


@pytest.fixture(autouse=True)
def _no_capture_writes(monkeypatch):
    """Keep @captured tool calls from scaffolding <cwd>/.magnolia: constructing
    a SessionManager creates its sessions dir even when nothing is recorded, and
    draining notices creates the store dir."""
    from compchem_memory import capture as _capture

    monkeypatch.setattr(_capture, "get_session_manager", lambda *a, **k: None)
    monkeypatch.setattr(
        _capture, "_attach_distill_notices", lambda result, project_dir: result
    )


class _FakeSubprocessRunner:
    """Records every subprocess.run call and answers from a canned-response dict.

    Tests populate `runner.canned[<substring>] = CompletedProcess(...)` to control
    what specific commands return. A command matches a canned key if the key
    appears as a substring in the joined command line. The first match wins.
    Unmatched calls return a successful CompletedProcess with empty stdout.
    """

    def __init__(self) -> None:
        self.calls: list[list[str]] = []
        self.canned: dict[str, CompletedProcess] = {}

    def __call__(self, args: list[str], *_a: Any, **kwargs: Any) -> CompletedProcess:
        self.calls.append(args)
        joined = " ".join(args)
        for pat, resp in self.canned.items():
            if pat in joined:
                return resp
        return CompletedProcess(args=args, returncode=0, stdout="", stderr="")


@pytest.fixture
def fake_subprocess(monkeypatch) -> _FakeSubprocessRunner:
    """Replace subprocess.run inside compchem_tools.tools.ssh_slurm with a recorder.

    Usage in a test:
        def test_thing(fake_subprocess):
            fake_subprocess.canned["sbatch"] = CompletedProcess(
                args=[], returncode=0, stdout="Submitted batch job 12345\\n"
            )
            ... call code under test ...
            assert any("sbatch" in " ".join(c) for c in fake_subprocess.calls)
    """
    runner = _FakeSubprocessRunner()
    monkeypatch.setattr("compchem_tools.tools.ssh_slurm.subprocess.run", runner)
    return runner
