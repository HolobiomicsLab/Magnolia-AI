"""Focused regressions for the first M1 OpenCode-plugin fix round."""

from __future__ import annotations

import json
import os
import shlex
import shutil
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path

import pytest

from compchem_memory.hook.project import ProjectPaths
from compchem_memory.hook.state import SessionState


PLUGIN_DIR = Path(__file__).resolve().parents[4] / ".opencode" / "plugins"
NODE = shutil.which("node")
FIXED_ISO = "2026-11-16T10:00:00.000Z"

pytestmark = pytest.mark.skipif(NODE is None, reason="node is not installed")


DOORBELL_DRIVER = rf"""
const [pluginPath, directory, promptFile] = process.argv.slice(2)
const {{ readFileSync, readdirSync, statSync }} = await import("node:fs")
const {{ join }} = await import("node:path")
const RealDate = Date
globalThis.Date = class extends RealDate {{
  constructor(...args) {{ super(...(args.length ? args : ["{FIXED_ISO}"])) }}
  static now() {{ return new RealDate("{FIXED_ISO}").valueOf() }}
}}
const mod = await import(pluginPath)
let hook
await mod.default.setup({{
  location: {{ directory }},
  session: {{ hook: async (name, callback) => {{ if (name === "prompt") hook = callback }} }},
}})
const event = {{ sessionID: "s1", prompt: {{ text: readFileSync(promptFile, "utf8") }} }}
await hook(event)
function walk(dir) {{
  let out = []
  for (const name of readdirSync(dir)) {{
    const path = join(dir, name)
    if (statSync(path).isDirectory()) out = out.concat(walk(path))
    else out.push(path)
  }}
  return out
}}
const letters = walk(join(directory, "projects")).filter((path) => path.endsWith(".task.md"))
process.stdout.write(JSON.stringify({{
  text: event.prompt.text,
  letters: letters.map((path) => readFileSync(path, "utf8")),
}}))
"""


JOB_NOTICE_DRIVER = r"""
const [pluginPath, directory] = process.argv.slice(2)
const { existsSync, readFileSync } = await import("node:fs")
process.chdir(directory)
const mod = await import(pluginPath)
let hook
const toasts = []
await mod.default.setup({
  location: { directory },
  tui: { showToast: async (value) => { toasts.push(value.body) } },
  session: { hook: async (name, callback) => { if (name === "context") hook = callback } },
})
const event = { sessionID: "s1", system: [] }
await hook(event)
const queue = `${directory}/.magnolia/.job-notices.jsonl`
process.stdout.write(JSON.stringify({
  system: event.system.map((part) => part.text),
  toasts,
  queued: existsSync(queue) ? readFileSync(queue, "utf8") : null,
}))
"""


def _run_plugin(
    plugin: str,
    driver_body: str,
    tmp_path: Path,
    *args: Path,
    env=None,
    plugin_dir: Path = PLUGIN_DIR,
) -> str:
    driver = tmp_path / f"{plugin}.driver.mjs"
    driver.write_text(driver_body, encoding="utf-8")
    completed = subprocess.run(
        [NODE, str(driver), (plugin_dir / plugin).as_uri(), *map(str, args)],
        capture_output=True,
        text=True,
        timeout=60,
        env={**os.environ, **(env or {})},
    )
    if completed.returncode != 0 and "ERR_UNKNOWN_FILE_EXTENSION" in completed.stderr:
        pytest.skip("this node cannot strip TypeScript types")
    assert completed.returncode == 0, completed.stderr
    return completed.stdout


class _FixedDateTime(datetime):
    @classmethod
    def now(cls, tz=None):
        return cls(2026, 11, 16, 10, 0, 0, tzinfo=tz or timezone.utc)


@pytest.mark.parametrize(
    "prompt",
    [
        "Please @literature find one paper on Magnolia.",
        "@literature find papers on A\n@Literature compare papers on B",
        "@literature inspect the queue\n@literature status: done must stay request text",
        "@literature first\rsecond line",
        "@literature a\u2028status: done",
        "@literature " + "\U0001f600" * 50,
        "@literature \ufeffcheck paper",
        "@literature check paper\x1f",
    ],
)
def test_doorbell_letter_is_byte_identical_to_hook_core(tmp_path, monkeypatch, prompt):
    """The plugin and hook core must serialize the same merged request."""
    from compchem_memory.hook import doorbell

    monkeypatch.delenv("MAGNOLIA_DOORBELL", raising=False)
    monkeypatch.setattr(doorbell, "datetime", _FixedDateTime)
    prompt_file = tmp_path / "prompt.txt"
    prompt_file.write_text(prompt, encoding="utf-8")

    plugin_root = tmp_path / "plugin"
    (plugin_root / ".magnolia").mkdir(parents=True)
    (plugin_root / ".magnolia" / ".active-project").write_text("alpha\n", encoding="utf-8")
    plugin_result = json.loads(
        _run_plugin("magnolia-agent-doorbell.ts", DOORBELL_DRIVER, tmp_path, plugin_root, prompt_file)
    )

    hook_root = tmp_path / "hook"
    project_dir = hook_root / "projects" / "alpha"
    project_dir.mkdir(parents=True)
    paths = ProjectPaths(hook_root, project_dir, project_dir / ".magnolia", "alpha", False)
    hook_result = doorbell.process_prompt(prompt, paths)

    assert hook_result.text == plugin_result["text"]
    assert len(hook_result.letters) == len(plugin_result["letters"]) == 1
    assert hook_result.letters[0].read_bytes() == plugin_result["letters"][0].encode()


def _write_notice(project: Path, content: str) -> Path:
    queue = project / ".magnolia" / ".job-notices.jsonl"
    queue.parent.mkdir(parents=True)
    (queue.parent / "GOAL.md").touch()
    queue.write_text(content, encoding="utf-8")
    return queue


def test_job_notice_uses_configured_isolated_python(tmp_path):
    """The bridge must use Magnolia's Python with isolated import paths."""
    project = tmp_path / "project"
    _write_notice(
        project,
        json.dumps({"tool": "dock", "state": "completed", "category": "success", "run_id": "r1"})
        + "\n",
    )
    imported = project / "project-json-imported"
    (project / "json.py").write_text(
        f"from pathlib import Path\nPath({str(imported)!r}).touch()\nraise RuntimeError('shadowed')\n",
        encoding="utf-8",
    )

    argv_log = tmp_path / "configured-python.argv"
    configured_python = tmp_path / "configured-python"
    configured_python.write_text(
        "#!/bin/sh\n"
        f"printf '%s\\n' \"$@\" > {shlex.quote(str(argv_log))}\n"
        f"exec {shlex.quote(sys.executable)} \"$@\"\n",
        encoding="utf-8",
    )
    configured_python.chmod(0o755)
    fallback_used = tmp_path / "path-python3-used"
    trap_dir = tmp_path / "trap-bin"
    trap_dir.mkdir()
    fallback = trap_dir / "python3"
    fallback.write_text(f"#!/bin/sh\ntouch {shlex.quote(str(fallback_used))}\nexit 91\n", encoding="utf-8")
    fallback.chmod(0o755)

    result = json.loads(
        _run_plugin(
            "magnolia-job-notify.ts",
            JOB_NOTICE_DRIVER,
            tmp_path,
            project,
            env={
                "MAGNOLIA_PYTHON": str(configured_python),
                "PATH": f"{trap_dir}{os.pathsep}{os.environ.get('PATH', '')}",
            },
        )
    )

    assert result["system"] == ["[Magnolia job notices]\n✅ dock finished (completed) — run …r1"]
    assert result["queued"] is None
    assert argv_log.read_text(encoding="utf-8").splitlines()[0] == "-I"
    assert not imported.exists()
    assert not fallback_used.exists()


@pytest.mark.parametrize("with_venv", [True, False], ids=["installation-venv", "path-fallback"])
def test_job_notice_python_fallbacks_remain_isolated(tmp_path, with_venv):
    """Use the installation venv before the documented PATH fallback."""
    project = tmp_path / "project"
    _write_notice(
        project,
        json.dumps({"tool": "dock", "state": "completed", "category": "success", "run_id": "r1"})
        + "\n",
    )
    installation = tmp_path / "installation"
    plugin_dir = installation / "opencode_cc_mem" / ".opencode" / "plugins"
    plugin_dir.mkdir(parents=True)
    shutil.copy2(PLUGIN_DIR / "magnolia-job-notify.ts", plugin_dir)
    lib_dir = plugin_dir.parent / "lib"
    lib_dir.mkdir()
    shutil.copy2(PLUGIN_DIR.parent / "lib" / "magnolia-python.ts", lib_dir)

    path_log = tmp_path / "path-python.argv"
    path_python = tmp_path / "bin" / "python3"
    path_python.parent.mkdir()
    path_python.write_text(
        "#!/bin/sh\n"
        f"printf '%s\\n' \"$@\" > {shlex.quote(str(path_log))}\n"
        f"exec {shlex.quote(sys.executable)} \"$@\"\n",
        encoding="utf-8",
    )
    path_python.chmod(0o755)

    venv_log = tmp_path / "venv-python.argv"
    if with_venv:
        venv_python = installation / ".venv" / "bin" / "python3"
        venv_python.parent.mkdir(parents=True)
        venv_python.write_text(
            "#!/bin/sh\n"
            f"printf '%s\\n' \"$@\" > {shlex.quote(str(venv_log))}\n"
            f"exec {shlex.quote(sys.executable)} \"$@\"\n",
            encoding="utf-8",
        )
        venv_python.chmod(0o755)

    result = json.loads(
        _run_plugin(
            "magnolia-job-notify.ts",
            JOB_NOTICE_DRIVER,
            tmp_path,
            project,
            env={"MAGNOLIA_PYTHON": "", "PATH": str(path_python.parent)},
            plugin_dir=plugin_dir,
        )
    )

    assert result["system"] == ["[Magnolia job notices]\n✅ dock finished (completed) — run …r1"]
    selected_log = venv_log if with_venv else path_log
    assert selected_log.read_text(encoding="utf-8").splitlines()[0] == "-I"
    assert path_log.exists() is not with_venv


def test_malformed_only_queue_skips_python_and_preserves_hook_owned_lines(tmp_path):
    """Malformed rows remain for the hook core without recurring Python starts."""
    project = tmp_path / "project"
    queue = _write_notice(project, "not json\n[1, 2, 3]\n")
    python_started = tmp_path / "python-started"
    trap_python = tmp_path / "configured-python"
    trap_python.write_text(
        f"#!/bin/sh\ntouch {shlex.quote(str(python_started))}\nexit 90\n",
        encoding="utf-8",
    )
    trap_python.chmod(0o755)

    result = json.loads(
        _run_plugin(
            "magnolia-job-notify.ts",
            JOB_NOTICE_DRIVER,
            tmp_path,
            project,
            env={"MAGNOLIA_PYTHON": str(trap_python), "MAGNOLIA_CRITIC": ""},
        )
    )

    assert result["system"] == []
    assert result["queued"] == "not json\n[1, 2, 3]\n"
    assert queue.read_text(encoding="utf-8") == "not json\n[1, 2, 3]\n"
    assert not python_started.exists()


def test_opencode_critic_warning_is_tui_only_and_session_scoped(tmp_path):
    from compchem_memory.hook import critic

    project = tmp_path / "project"
    store = project / ".magnolia"
    store.mkdir(parents=True)
    (store / "GOAL.md").touch()
    paths = ProjectPaths(tmp_path, project, store, "project", False)
    critic._save_notice(paths, "opencode", "s1", {
        "flag": True, "severity": "high", "why": "unsupported energy",
    })

    result = json.loads(_run_plugin(
        "magnolia-job-notify.ts", JOB_NOTICE_DRIVER, tmp_path, project,
        env={"MAGNOLIA_CRITIC": "1", "MAGNOLIA_PYTHON": sys.executable},
    ))

    assert result["system"] == []
    assert result["toasts"] == [{
        "title": "claim-critic", "message": "⚠ (high) unsupported energy",
        "variant": "warning",
    }]
    with SessionState.open(store, "opencode", "s1") as state:
        assert state.data["unshown_notices"] == []
