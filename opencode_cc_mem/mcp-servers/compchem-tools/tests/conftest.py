"""Shared pytest fixtures for compchem-tools tests."""
from __future__ import annotations
from subprocess import CompletedProcess
from typing import Any
import pytest


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
