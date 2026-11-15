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
from compchem_memory.hook.action_retrieval import on_post_tool
from compchem_memory.hook.events import HookEvent, ToolCall
from compchem_memory.hook.project import ProjectPaths
from compchem_memory.hook.state import SessionState

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


DOORBELL_DRIVER = r"""
const [pluginPath, directory, promptFile] = process.argv.slice(2)
const { readFileSync, readdirSync, statSync } = await import("node:fs")
const { join } = await import("node:path")
const mod = await import(pluginPath)
let hook
await mod.default.setup({
  location: { directory },
  session: { hook: async (name, callback) => { if (name === "prompt") hook = callback } },
})
const event = { sessionID: "s1", prompt: { text: readFileSync(promptFile, "utf8") } }
await hook(event)
function walk(dir) {
  let out = []
  for (const name of readdirSync(dir)) {
    const path = join(dir, name)
    if (statSync(path).isDirectory()) out = out.concat(walk(path))
    else out.push(path)
  }
  return out
}
const letters = []
try { letters.push(...walk(join(directory, "projects")).filter((p) => p.endsWith(".task.md"))) } catch {}
process.stdout.write(JSON.stringify({
  text: event.prompt.text,
  letters: letters.map((p) => readFileSync(p, "utf8")),
}))
"""


def test_doorbell_matches_plugin(tmp_path, monkeypatch):
    from compchem_memory.hook.doorbell import process_prompt
    from compchem_memory.hook.project import ProjectPaths

    monkeypatch.delenv("MAGNOLIA_DOORBELL", raising=False)
    prompt = "Please @literature find three recent papers on spatial metabolomics.\nthanks"
    prompt_file = tmp_path / "prompt.txt"
    prompt_file.write_text(prompt, encoding="utf-8")

    plugin_workspace = tmp_path / "plugin"
    store = plugin_workspace / "projects" / "alpha" / ".magnolia"
    store.mkdir(parents=True)
    (plugin_workspace / ".magnolia").mkdir()
    (plugin_workspace / ".magnolia" / ".active-project").write_text("alpha\n", encoding="utf-8")
    plugin_result = json.loads(
        run_plugin("magnolia-agent-doorbell.ts", DOORBELL_DRIVER, tmp_path, plugin_workspace, prompt_file)
    )

    python_workspace = tmp_path / "python"
    (python_workspace / "projects" / "alpha").mkdir(parents=True)
    paths = ProjectPaths(
        python_workspace, python_workspace / "projects" / "alpha",
        python_workspace / "projects" / "alpha" / ".magnolia", "alpha", False,
    )
    python_result = process_prompt(prompt, paths)

    assert plugin_result["letters"], "the plugin wrote no letter"
    assert len(python_result.letters) == len(plugin_result["letters"])

    def without_date(text):
        return [line for line in text.splitlines() if not line.startswith("- date: ")]

    assert python_result.text == plugin_result["text"]
    assert without_date(python_result.letters[0].read_text(encoding="utf-8")) == without_date(
        plugin_result["letters"][0]
    )


ACTION_DRIVER = r"""
const [pluginPath, directory] = process.argv.slice(2)
const { readFile, writeFile } = await import("node:fs/promises")
// Bun exposes import.meta.dir; Node exposes the equivalent import.meta.dirname.
const nodePlugin = new URL("./magnolia-action-retrieval-node.ts", import.meta.url)
await writeFile(nodePlugin, (await readFile(new URL(pluginPath), "utf8")).replaceAll("import.meta.dir", "import.meta.dirname"))
const mod = await import(nodePlugin)
const hooks = {}
await mod.default.setup({
  location: { directory },
  tool: { hook: async (name, callback) => { hooks[name] = callback } },
})
await hooks["execute.before"]({
  tool: "compchem-tools_run_shell", id: "c1", sessionID: "s1",
  input: { cmd: "haddock3 run workflow" },
})
const event = {
  tool: "compchem-tools_run_shell", id: "c1", sessionID: "s1",
  result: { output: "RESULT" },
}
await hooks["execute.after"](event)
process.stdout.write(JSON.stringify(event.result.output.slice(0, -"RESULT".length)))
"""


def test_action_retrieval_block_matches_plugin_v2(tmp_path, monkeypatch):
    plugin_workspace = tmp_path / "plugin"
    plugin_store = plugin_workspace / ".magnolia"
    plugin_store.mkdir(parents=True)
    (plugin_store / "GOAL.md").write_text("goal\n", encoding="utf-8")
    hit_path = plugin_store / "staging" / "parity.md"
    hit_path.parent.mkdir()
    hit_path.write_text("entry\n", encoding="utf-8")
    hits = [
        {
            "title": "Haddock timeout recovery",
            "type": "error_resolution",
            "tier": "staging",
            "score": 4,
            "provisional": True,
            "confidence": 0.9,
            "path": str(hit_path),
            "gist": "Retry with the saved workflow parameters.",
        }
    ]
    fake_python = tmp_path / "fake-quick-search"
    fake_python.write_text(
        "#!/bin/sh\nprintf '%s\\n' " + json.dumps(json.dumps(hits[0])) + "\n",
        encoding="utf-8",
    )
    fake_python.chmod(0o755)
    monkeypatch.setenv("MAGNOLIA_PYTHON", str(fake_python))
    plugin_block = json.loads(
        run_plugin(
            "magnolia-action-retrieval.ts",
            ACTION_DRIVER,
            tmp_path,
            plugin_workspace,
        )
    )

    python_project = tmp_path / "python"
    python_store = python_project / ".magnolia"
    python_store.mkdir(parents=True)
    monkeypatch.setattr("compchem_memory.quick_search.search", lambda query, project_dir, k=5: hits)
    paths = ProjectPaths(tmp_path, python_project, python_store, "python", True)
    event = HookEvent(
        harness="claude",
        event="post-tool",
        session_id="s1",
        cwd=str(python_project),
        tool=ToolCall(
            name="run_shell",
            server="compchem-tools",
            raw_name="mcp__compchem-tools__run_shell",
            call_id="c1",
            input={"cmd": "haddock3 run workflow"},
            output="RESULT",
        ),
    )
    note = on_post_tool(event, paths, __import__("time").monotonic() + 10)

    assert note is not None
    assert note.text == plugin_block
