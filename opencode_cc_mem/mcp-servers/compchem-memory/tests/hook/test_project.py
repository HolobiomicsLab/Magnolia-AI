"""Project resolution and durable per-session project bindings."""

import json
import os
import subprocess
import sys
import time
from pathlib import Path

import pytest

from compchem_memory.hook import project as project_module
from compchem_memory.hook.project import ProjectPaths, bind_project, resolve_project, state_home


def _make_workspace(tmp_path: Path) -> Path:
    workspace = tmp_path / "workspace"
    (workspace / "rules").mkdir(parents=True)
    return workspace


def _activate(workspace: Path, name: str) -> Path:
    marker = workspace / ".magnolia" / ".active-project"
    marker.parent.mkdir(exist_ok=True)
    marker.write_text(f"{name}\n", encoding="utf-8")
    return workspace / "projects" / name


def test_resolve_project_root_mode_by_goal(tmp_path):
    workspace = _make_workspace(tmp_path)
    project = workspace / "projects" / "rooted"
    (project / ".magnolia").mkdir(parents=True)
    (project / ".magnolia" / "GOAL.md").write_text("Goal\n", encoding="utf-8")

    resolved = resolve_project(project, {})

    assert resolved == ProjectPaths(
        root=workspace,
        project_dir=project,
        store=project / ".magnolia",
        name="rooted",
        root_mode=True,
    )
    assert resolved.store == resolved.project_dir / ".magnolia"


def test_resolve_project_active_project_marker(tmp_path):
    workspace = _make_workspace(tmp_path)
    project = _activate(workspace, "alpha")

    resolved = resolve_project(workspace, {})

    assert resolved == ProjectPaths(workspace, project, project / ".magnolia", "alpha", False)
    assert resolved.store == resolved.project_dir / ".magnolia"


def test_resolve_project_env_absolute(tmp_path):
    workspace = _make_workspace(tmp_path)
    project = tmp_path / "elsewhere" / "beta"

    resolved = resolve_project(workspace, {"MAGNOLIA_PROJECT_DIR": str(project)})

    assert resolved == ProjectPaths(workspace, project, project / ".magnolia", "beta", False)
    assert resolved.store == resolved.project_dir / ".magnolia"


def test_resolve_project_env_relative_to_cwd(tmp_path):
    workspace = _make_workspace(tmp_path)

    resolved = resolve_project(workspace, {"MAGNOLIA_PROJECT_DIR": "projects/gamma"})

    project = workspace / "projects" / "gamma"
    assert resolved == ProjectPaths(workspace, project, project / ".magnolia", "gamma", False)
    assert resolved.store == resolved.project_dir / ".magnolia"


def test_resolve_project_none_outside_workspace(tmp_path):
    assert resolve_project(tmp_path, {}) is None


def test_resolve_project_outside_root_mode_keeps_cwd_as_root(tmp_path):
    workspace = _make_workspace(tmp_path)
    cwd = workspace / "nested"
    cwd.mkdir()
    _activate(cwd, "alpha")

    resolved = resolve_project(cwd, {})

    assert resolved.root == cwd
    assert resolved.project_dir == cwd / "projects" / "alpha"
    assert not resolved.root_mode


def test_state_home_uses_home_from_supplied_env(tmp_path, monkeypatch):
    home = tmp_path / "client-home"
    process_home = tmp_path / "process-home"
    monkeypatch.setenv("HOME", str(process_home))

    resolved = state_home({"HOME": str(home)})

    assert resolved == home / ".local" / "state" / "magnolia"
    assert resolved.is_dir()
    assert not (process_home / ".local" / "state" / "magnolia").exists()


_BIND_SCRIPT = """
import json
import os
from pathlib import Path
from compchem_memory.hook.project import bind_project

result = bind_project("opencode", "shared/session", Path.cwd(), dict(os.environ))
print(json.dumps(None if result is None else {
    "root": str(result.root),
    "project_dir": str(result.project_dir),
    "store": str(result.store),
    "name": result.name,
    "root_mode": result.root_mode,
}))
"""


def _bind_in_subprocess(workspace: Path, state: Path) -> dict | None:
    env = dict(os.environ)
    env["XDG_STATE_HOME"] = str(state)
    completed = subprocess.run(
        [sys.executable, "-c", _BIND_SCRIPT],
        cwd=workspace,
        env=env,
        check=True,
        capture_output=True,
        text=True,
    )
    return json.loads(completed.stdout)


def test_binding_survives_marker_change(tmp_path):
    workspace = _make_workspace(tmp_path)
    state = tmp_path / "state"
    project_a = _activate(workspace, "alpha")
    first = _bind_in_subprocess(workspace, state)
    _activate(workspace, "beta")
    second = _bind_in_subprocess(workspace, state)

    assert first == second
    assert first["project_dir"] == str(project_a)
    assert len(list((state / "magnolia" / "bindings").glob("*.json"))) == 1


def test_binding_concurrent_first_events(tmp_path):
    workspace = _make_workspace(tmp_path)
    state = tmp_path / "state"
    project = _activate(workspace, "alpha")
    env = dict(os.environ)
    env["XDG_STATE_HOME"] = str(state)
    processes = [
        subprocess.Popen(
            [sys.executable, "-c", _BIND_SCRIPT],
            cwd=workspace,
            env=env,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )
        for _ in range(8)
    ]

    results = []
    for process in processes:
        stdout, stderr = process.communicate(timeout=10)
        assert process.returncode == 0, stderr
        results.append(json.loads(stdout))

    assert all(result["project_dir"] == str(project) for result in results)
    assert len(results) == 8
    bindings = list((state / "magnolia" / "bindings").glob("*.json"))
    assert len(bindings) == 1
    assert bindings[0].name == "opencode-shared_session.json"


def test_binding_without_session_start(tmp_path):
    workspace = _make_workspace(tmp_path)
    state = tmp_path / "state"
    project = _activate(workspace, "alpha")

    bound = bind_project("vibe", "first-post-tool", workspace, {"XDG_STATE_HOME": str(state)})

    assert bound.project_dir == project
    binding = state_home({"XDG_STATE_HOME": str(state)}) / "bindings" / "vibe-first-post-tool.json"
    assert json.loads(binding.read_text(encoding="utf-8"))["project"]["name"] == "alpha"


def test_binding_none_is_persistent_and_sanitised(tmp_path):
    state = tmp_path / "state"
    harness = "client/name"
    session = "session:id"

    assert bind_project(harness, session, tmp_path, {"XDG_STATE_HOME": str(state)}) is None
    _activate(tmp_path, "late")
    assert bind_project(harness, session, tmp_path, {"XDG_STATE_HOME": str(state)}) is None

    binding = state / "magnolia" / "bindings" / "client_name-session_id.json"
    assert json.loads(binding.read_text(encoding="utf-8")) == {"project": None}
    assert binding.parent.stat().st_mode & 0o777 == 0o700
    assert state_home({"XDG_STATE_HOME": str(state)}).stat().st_mode & 0o777 == 0o700


@pytest.mark.parametrize("contents", [b"", b"{}", b"\xff\xfe garbage"])
def test_binding_damaged_file_is_no_project(tmp_path, contents):
    state = tmp_path / "state"
    bindings = state_home({"XDG_STATE_HOME": str(state)}) / "bindings"
    bindings.mkdir()
    (bindings / "codex-damaged.json").write_bytes(contents)
    started = time.monotonic()

    bound = bind_project("codex", "damaged", tmp_path, {"XDG_STATE_HOME": str(state)})

    assert bound is None
    assert time.monotonic() - started < 0.5


def test_binding_creation_fsyncs_before_link(tmp_path, monkeypatch):
    state = tmp_path / "state"
    events = []
    real_fsync = os.fsync
    real_link = os.link

    def recording_fsync(descriptor):
        events.append("fsync")
        real_fsync(descriptor)

    def recording_link(source, destination):
        events.append("link")
        real_link(source, destination)

    monkeypatch.setattr(project_module.os, "fsync", recording_fsync)
    monkeypatch.setattr(project_module.os, "link", recording_link)

    assert bind_project("codex", "synced", tmp_path, {"XDG_STATE_HOME": str(state)}) is None
    assert events == ["fsync", "link"]
