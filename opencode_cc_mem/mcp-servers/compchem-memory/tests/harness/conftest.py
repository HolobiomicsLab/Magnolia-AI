import shutil
from pathlib import Path

import pytest

from compchem_memory.harness import __main__ as launch_cli
from compchem_memory.harness.neutral import load
from compchem_memory.harness.protocol import LaunchContext

REPO = Path(__file__).parents[4]


@pytest.fixture
def make_render(tmp_path):
    """Build (neutral, context) over a scratch Magnolia root and the real template."""

    def make(setup=None, model=None, project="demo", python="/venv/bin/python"):
        root = tmp_path / "repo" / "opencode_cc_mem"
        (root / "rules").mkdir(parents=True, exist_ok=True)
        shutil.copy(REPO / "opencode.json.template", root / "opencode.json.template")
        (root / "AGENTS.md").write_text("AGENTS BODY\n", encoding="utf-8")
        (root / "rules" / "b.md").write_text("rule b\n", encoding="utf-8")
        (root / "rules" / "a.md").write_text("rule a\n", encoding="utf-8")
        (root / "rules" / "ignored.txt").write_text("x", encoding="utf-8")
        skill = root / ".opencode" / "skills" / "alpha"
        skill.mkdir(parents=True, exist_ok=True)
        (skill / "SKILL.md").write_text("alpha\n", encoding="utf-8")
        setup = setup if setup is not None else {
            "memory_enabled": True, "memory_model": "mem-model",
        }
        project_dir = root / "projects" / project
        context = LaunchContext(
            root=root, project=project, project_dir=project_dir, python=python,
            tools_port=8123, model=model, setup=setup,
        )
        neutral = load(
            root / "opencode.json.template", launch_cli._substitutions(context), setup
        )
        return neutral, context

    return make
