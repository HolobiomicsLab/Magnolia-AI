"""Byte parity with the real OpenCode plugins, executed under Node."""

import json
import os
import shutil
import subprocess
from pathlib import Path
from types import SimpleNamespace

import pytest

from compchem_memory.hook.boot_context import MAX_PAYLOAD_CHARS, build_boot_payload
from compchem_memory.hook.directive import DIRECTIVE, retrieval_directive
from compchem_memory.hook.project import ProjectPaths

PLUGIN_DIR = Path(__file__).resolve().parents[4] / ".opencode" / "plugins"
NODE = shutil.which("node") or "node"

pytestmark = pytest.mark.skipif(not Path(NODE).exists(), reason="node is not installed")


def run_plugin(plugin_file, driver_body, *args) -> str:
    """Run a real TypeScript plugin with a temporary JavaScript driver."""
    driver = Path(args[0]) / "driver.mjs"
    driver.write_text(driver_body, encoding="utf-8")
    env = dict(os.environ)
    env.pop("MAGNOLIA_AUTORETRIEVE", None)
    env.pop("MAGNOLIA_PROJECT_WORKSPACE", None)
    completed = subprocess.run(
        [NODE, str(driver), (PLUGIN_DIR / plugin_file).as_uri(), *map(str, args[1:])],
        capture_output=True,
        text=True,
        timeout=60,
        env=env,
    )
    if completed.returncode != 0 and "ERR_UNKNOWN_FILE_EXTENSION" in completed.stderr:
        pytest.skip("this node cannot strip TypeScript types")
    assert completed.returncode == 0, completed.stderr
    return completed.stdout


BOOT_DRIVER = r"""
const [pluginPath, directory] = process.argv.slice(2)
const mod = await import(pluginPath)
let hook
await mod.default.setup({
  location: { directory },
  session: { hook: async (name, callback) => { if (name === "context") hook = callback } },
})
const event = { sessionID: "s1", system: [] }
await hook(event)
process.stdout.write(JSON.stringify(event.system.map((part) => part.text)))
"""


def _workspace(tmp_path, oversized=False):
    project_dir = tmp_path / "projects" / "alpha"
    store = project_dir / ".magnolia"
    store.mkdir(parents=True)
    (tmp_path / ".magnolia").mkdir()
    (tmp_path / ".magnolia" / ".active-project").write_text("alpha\n", encoding="utf-8")
    (store / "boot-context.md").write_text(
        "x" * MAX_PAYLOAD_CHARS if oversized else "\nBoot text\n", encoding="utf-8"
    )
    (store / "audit-report.md").write_text("Audit text\n", encoding="utf-8")
    rules = tmp_path / "rules"
    rules.mkdir()
    (rules / "z-last.md").write_text("Last rule\n", encoding="utf-8")
    (rules / "a-first.md").write_text("First rule\n", encoding="utf-8")
    return ProjectPaths(tmp_path, project_dir, store, "alpha", False)


@pytest.mark.parametrize("oversized", [False, True], ids=["complete", "oversized"])
def test_boot_payload_matches_plugin(tmp_path, oversized):
    paths = _workspace(tmp_path, oversized)

    plugin_parts = json.loads(run_plugin("magnolia-boot-context.ts", BOOT_DRIVER, tmp_path, tmp_path))

    assert plugin_parts == [build_boot_payload(paths)]


DIRECTIVE_DRIVER = r"""
const [pluginPath, directory] = process.argv.slice(2)
const mod = await import(pluginPath)
let hook
await mod.default.setup({
  location: { directory },
  session: { hook: async (name, callback) => { if (name === "prompt") hook = callback } },
})
const appended = []
for (const text of ["first prompt", "second prompt"]) {
  const event = { sessionID: "s1", prompt: { text } }
  await hook(event)
  appended.push(event.prompt.text.slice(text.length))
}
process.stdout.write(JSON.stringify(appended))
"""


def test_directive_matches_plugin_and_second_prompt_is_unchanged(tmp_path, monkeypatch):
    monkeypatch.delenv("MAGNOLIA_AUTORETRIEVE", raising=False)
    project_dir = tmp_path / "project"
    store = project_dir / ".magnolia"
    store.mkdir(parents=True)
    (store / "GOAL.md").write_text("goal\n", encoding="utf-8")
    paths = ProjectPaths(tmp_path, project_dir, store, "project", True)
    state = SimpleNamespace(data={})

    plugin_parts = json.loads(
        run_plugin("magnolia-auto-retrieval.ts", DIRECTIVE_DRIVER, tmp_path, project_dir)
    )

    assert retrieval_directive(paths, state, "s1") == DIRECTIVE
    assert plugin_parts == [DIRECTIVE, ""]
    assert retrieval_directive(paths, state, "s1") is None
