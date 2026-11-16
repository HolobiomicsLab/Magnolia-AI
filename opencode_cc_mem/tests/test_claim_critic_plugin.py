"""OpenCode claim critic hands turn-end work to Python without awaiting it."""

import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

PLUGIN = Path(__file__).resolve().parents[1] / ".opencode" / "plugins" / "claim-critic.ts"
CAPTURE_PLUGIN = PLUGIN.with_name("magnolia-session-capture.ts")
ACTION_PLUGIN = PLUGIN.with_name("magnolia-action-retrieval.ts")
NODE = shutil.which("node")

DRIVER = r"""
const [pluginPath, directory, mode] = process.argv.slice(2)
const calls = []
globalThis.__MAGNOLIA_TEST_SPAWN = (command, argv, options) => {
  calls.push({
    command, argv,
    options: {
      cwd: options.cwd, detached: options.detached, stdio: options.stdio,
      envKeys: Object.keys(options.env),
      env: {
        PYTHONPATH: options.env.PYTHONPATH,
        MAGNOLIA_ROOT: options.env.MAGNOLIA_ROOT,
        MAGNOLIA_PROJECT_DIR: options.env.MAGNOLIA_PROJECT_DIR,
      },
    },
  })
  return { unref: () => { calls[0].unref = true } }
}
const mod = await import(pluginPath)
let resumed = false
const event = {
  id: "evt_u1", type: "session.execution.succeeded", durable: { aggregateID: "s1" },
}
if (mode === "v2") {
  const ctx = {
    location: { directory },
    event: { subscribe: () => (async function* () {
      yield event
      resumed = true
      if (calls.length !== 1) throw new Error("turn-end handoff was not synchronous")
    })() },
    session: { context: async () => { throw new Error("context awaited before handoff") } },
  }
  await mod.default.setup(ctx)
  for (let index = 0; index < 20 && !resumed; index += 1) {
    await new Promise((resolve) => setImmediate(resolve))
  }
} else {
  const hooks = await mod.default.server({ directory })
  hooks.event({
    event: { id: "evt_u1", type: "session.idle", properties: { sessionID: "s1" } },
  })
  resumed = calls.length === 1
}
const payload = calls.length
  ? JSON.parse((await import("node:fs")).readFileSync(calls[0].argv.at(-1), "utf8"))
  : null
process.stdout.write(JSON.stringify({ calls, payload, resumed }))
"""

CAPTURE_DRIVER = r"""
const [pluginPath, directory] = process.argv.slice(2)
const calls = []
globalThis.__MAGNOLIA_TEST_SPAWN = (command, argv, options) => {
  calls.push({ command, argv, detached: options.detached })
  return { unref: () => {} }
}
let promptHook
let release
const gate = new Promise((resolve) => { release = resolve })
let resumed = false
const mod = await import(pluginPath)
const ctx = {
  location: { directory },
  session: {
    hook: async (_name, callback) => { promptHook = callback },
    context: async () => { throw new Error("capture awaited in process") },
  },
  event: { subscribe: () => (async function* () {
    await gate
    yield {
      id: "evt_u1", type: "session.execution.succeeded", durable: { aggregateID: "s1" },
    }
    resumed = true
    if (calls.length !== 1) throw new Error("capture handoff was not synchronous")
  })() },
}
await mod.default.setup(ctx)
await promptHook({ sessionID: "s1" })
release()
for (let index = 0; index < 20 && !resumed; index += 1) await new Promise((r) => setImmediate(r))
const payload = JSON.parse((await import("node:fs")).readFileSync(calls[0].argv.at(-1), "utf8"))
process.stdout.write(JSON.stringify({ calls, payload, resumed }))
"""

ACTION_DRIVER = r"""
const [pluginPath, directory] = process.argv.slice(2)
const calls = []
globalThis.__MAGNOLIA_TEST_SPAWN = (command, argv, options) => {
  calls.push({ command, argv, detached: options.detached })
  return { unref: () => {} }
}
const hooks = {}
let release
const gate = new Promise((resolve) => { release = resolve })
let resumed = false
const mod = await import(pluginPath)
const ctx = {
  location: { directory },
  tool: { hook: async (name, callback) => { hooks[name] = callback } },
  event: { subscribe: () => (async function* () {
    await gate
    yield {
      id: "evt_u1", type: "session.execution.succeeded", durable: { aggregateID: "s1" },
    }
    resumed = true
    if (calls.length !== 1) throw new Error("probe handoff was not synchronous")
  })() },
}
await mod.default.setup(ctx)
await hooks["execute.before"]({ tool: "write", id: "call-1", input: { path: "result.txt" } })
await hooks["execute.after"]({
  tool: "write", id: "call-1", sessionID: "s1", result: { output: "ok" },
})
release()
for (let index = 0; index < 20 && !resumed; index += 1) await new Promise((r) => setImmediate(r))
const payload = JSON.parse((await import("node:fs")).readFileSync(calls[0].argv.at(-1), "utf8"))
process.stdout.write(JSON.stringify({ calls, payload, resumed }))
"""

pytestmark = pytest.mark.skipif(NODE is None, reason="node is not installed")


def _run_handoff(tmp_path: Path, mode: str, python: str = "/test/python") -> dict:
    project = tmp_path / "project"
    (project / ".magnolia").mkdir(parents=True)
    (project / ".magnolia" / "GOAL.md").write_text("goal\n", encoding="utf-8")
    driver = tmp_path / f"detached-{mode}.mjs"
    driver.write_text(DRIVER, encoding="utf-8")
    proc = subprocess.run(
        [NODE, str(driver), PLUGIN.as_uri(), str(project), mode],
        env={**os.environ, "MAGNOLIA_CRITIC": "1", "MAGNOLIA_PYTHON": python},
        capture_output=True, text=True, timeout=60,
    )
    if proc.returncode != 0 and "ERR_UNKNOWN_FILE_EXTENSION" in proc.stderr:
        pytest.skip("this node cannot strip TypeScript types")
    assert proc.returncode == 0, proc.stderr
    return json.loads(proc.stdout)


@pytest.mark.parametrize("mode", ["v1", "v2"])
def test_turn_end_synchronously_spawns_detached_python_worker(tmp_path, mode):
    result = _run_handoff(tmp_path, mode)

    assert result["resumed"] is True
    call = result["calls"][0]
    assert call["command"] == "/test/python"
    assert call["argv"][0] == "-I"
    assert "compchem_memory.hook.background" in call["argv"]
    assert call["argv"][-2] == "--opencode-turn"
    assert call["options"]["detached"] is True
    assert call["options"]["stdio"][0] == "ignore"
    assert call["options"]["stdio"][1] == call["options"]["stdio"][2]
    assert call["options"]["env"]["MAGNOLIA_PROJECT_DIR"].endswith("/project")
    assert "PYTHONPATH" not in call["options"]["env"]
    assert call["unref"] is True
    assert result["payload"]["operation"] == "critic"
    assert result["payload"]["session_id"] == "s1"
    assert result["payload"]["turn_id"] == "msg_u1"


def _run_driver(tmp_path: Path, plugin: Path, source: str, env: dict | None = None) -> dict:
    project = tmp_path / "project"
    (project / ".magnolia").mkdir(parents=True)
    (project / ".magnolia" / "GOAL.md").write_text("goal\n", encoding="utf-8")
    driver = tmp_path / f"{plugin.stem}-driver.mjs"
    driver.write_text(source, encoding="utf-8")
    proc = subprocess.run(
        [NODE, str(driver), plugin.as_uri(), str(project)],
        env={**os.environ, **(env or {})}, capture_output=True, text=True, timeout=60,
    )
    if proc.returncode != 0 and "ERR_UNKNOWN_FILE_EXTENSION" in proc.stderr:
        pytest.skip("this node cannot strip TypeScript types")
    assert proc.returncode == 0, proc.stderr
    return json.loads(proc.stdout)


def test_capture_turn_end_spawns_before_reading_session_context(tmp_path):
    result = _run_driver(tmp_path, CAPTURE_PLUGIN, CAPTURE_DRIVER)
    assert result["resumed"] is True
    assert result["calls"][0]["detached"] is True
    assert result["payload"]["operation"] == "capture"
    assert result["payload"]["turn_id"] == "msg_u1"


def test_action_probe_turn_end_hands_injected_items_to_worker(tmp_path):
    hit = tmp_path / "project" / ".magnolia" / "staging" / "entry.md"
    fake_python = tmp_path / "fake-python"
    fake_python.write_text(
        "#!/bin/sh\nprintf '%s\\n' '" + json.dumps({
            "path": str(hit), "score": 3, "title": "Alpha memory", "gist": "Use alpha",
        }) + "'\n",
        encoding="utf-8",
    )
    fake_python.chmod(0o755)
    result = _run_driver(
        tmp_path, ACTION_PLUGIN, ACTION_DRIVER, {"MAGNOLIA_PYTHON": str(fake_python)},
    )
    assert result["resumed"] is True
    assert result["calls"][0]["detached"] is True
    assert result["payload"]["operation"] == "probe"
    assert result["payload"]["items"][0]["callID"] == "call-1"
    assert result["payload"]["turn_id"] == "msg_u1"


def test_turn_end_worker_environment_is_an_allowlist(tmp_path):
    result = _run_handoff(tmp_path, "v1")

    options = result["calls"][0]["options"]
    env = options["env"]
    assert env["MAGNOLIA_ROOT"]
    assert env["MAGNOLIA_PROJECT_DIR"].endswith("/project")
    assert "PYTHONPATH" not in options["envKeys"]
    assert "OPENCODE_SERVER_PASSWORD" not in options["envKeys"]


def test_turn_end_isolated_worker_cannot_import_project_json(tmp_path):
    result = _run_handoff(tmp_path, "v1", sys.executable)
    project = tmp_path / "project"
    marker = tmp_path / "project-json-imported"
    (project / "json.py").write_text(
        f"from pathlib import Path\nPath({str(marker)!r}).touch()\n",
        encoding="utf-8",
    )
    job_path = Path(result["calls"][0]["argv"][-1])
    job_path.unlink()
    missing = project / ".magnolia" / "hook-workers" / "missing.json"
    command = result["calls"][0]["command"]
    argv = [*result["calls"][0]["argv"][:-1], str(missing)]

    completed = subprocess.run(
        [command, *argv], cwd=result["calls"][0]["options"]["cwd"],
        env={"HOME": os.environ["HOME"], "PATH": os.environ["PATH"]},
        capture_output=True, text=True, timeout=60,
    )

    assert completed.returncode == 0, completed.stderr
    assert not marker.exists()
