"""Tests for boot_context: must use assemble_context, must write to file, must regen each boot."""

from pathlib import Path
import time

import pytest


@pytest.fixture
def project_dir(tmp_path):
    pd = tmp_path / "proj"
    (pd / ".magnolia" / "entries").mkdir(parents=True)
    (pd / ".magnolia" / "sessions").mkdir(parents=True)
    (pd / ".magnolia" / "staging").mkdir(parents=True)
    return pd


def test_boot_context_calls_assemble_context(project_dir, monkeypatch):
    from compchem_memory import boot_context

    calls = {"count": 0, "args": None}

    def fake_assemble(task_description, project_dir, skills_dir=None, token_budget=8000, **kw):
        calls["count"] += 1
        calls["args"] = {
            "task_description": task_description,
            "project_dir": project_dir,
            "token_budget": token_budget,
        }
        class FakeResult:
            content = "## Reference\nbody text\n"
            tokens_used = 100
            sources = []
        return FakeResult()

    monkeypatch.setattr(boot_context, "assemble_context", fake_assemble)

    boot_context.regenerate_boot_context(str(project_dir))

    assert calls["count"] == 1
    assert calls["args"]["project_dir"] == str(project_dir)
    written = (project_dir / ".magnolia" / "boot-context.md").read_text()
    assert "Reference" in written


def test_boot_context_regenerated_each_call(project_dir, monkeypatch):
    from compchem_memory import boot_context

    counter = {"n": 0}

    def fake_assemble(task_description, project_dir, skills_dir=None, token_budget=8000, **kw):
        counter["n"] += 1
        class R:
            content = f"call {counter['n']}\n"
            tokens_used = 1
            sources = []
        return R()

    monkeypatch.setattr(boot_context, "assemble_context", fake_assemble)

    boot_context.regenerate_boot_context(str(project_dir))
    first = (project_dir / ".magnolia" / "boot-context.md").read_text()
    time.sleep(0.01)
    boot_context.regenerate_boot_context(str(project_dir))
    second = (project_dir / ".magnolia" / "boot-context.md").read_text()

    assert first != second
    assert "call 1" in first
    assert "call 2" in second
