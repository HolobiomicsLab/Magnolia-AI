"""Project resolution and durable per-session project bindings."""

import json
import os
import subprocess
import sys
from pathlib import Path

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
