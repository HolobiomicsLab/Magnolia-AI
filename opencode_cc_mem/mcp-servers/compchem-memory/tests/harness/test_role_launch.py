import hashlib
import importlib.util
import json
import os
import py_compile
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

from compchem_memory.harness import RENDERERS
from compchem_memory.harness.neutral import NeutralConfig
from compchem_memory.harness.protocol import LaunchContext
from compchem_memory.harness.ownership import is_magnolia_hook_command
from compchem_memory.harness.render_common import (
    _required_pre_tool_command,
    harness_dir,
    hook_command,
    policy_files,
)
from compchem_memory.hook.events import native_tool


ROOT = Path(__file__).parents[4]
FIXTURES = ROOT / "tests" / "fixtures" / "executors"


def test_noninteractive_policy_is_run_owned_and_required(tmp_path):
    project = tmp_path / "project"
    control = tmp_path / "run" / "control"
    context = LaunchContext(
        root=tmp_path,
        project="project-id",
        project_dir=project,
        python="/venv/bin/python",
        tools_port=8123,
        model="selected-model",
        setup={},
        run_dir=tmp_path / "run",
        control_dir=control,
        work_dir=tmp_path / "run" / "work",
        role="helper",
        invocation_id="helper-1",
        noninteractive=True,
        role_policy={"role": "helper", "mcp_tool_allowlist": []},
    )

    files, env = policy_files(NeutralConfig([], frozenset({"bash"})), context, "claude")
    expected_dir = control / "executors" / "helper-1"
    policy_path = expected_dir / "policy.json"

    assert harness_dir(context, "claude") == expected_dir
    assert set(files) == {policy_path}
    assert json.loads(files[policy_path]) == {
        "deny_tools": ["bash"],
        "mcp_tool_allowlist": [],
        "role": "helper",
    }
    assert env == {
        "MAGNOLIA_HOOK_POLICY": str(policy_path.resolve()),
        "MAGNOLIA_HOOK_POLICY_REQUIRED": "1",
        "MAGNOLIA_DOORBELL": "0",
    }


def test_interactive_policy_location_and_environment_stay_compatible(make_render):
    neutral, context = make_render()

    files, env = policy_files(neutral, context, "codex")

    expected = context.project_dir / ".magnolia" / "harness" / "codex" / "policy.json"
    assert set(files) == {expected}
    assert env == {"MAGNOLIA_HOOK_POLICY": str(expected)}


@pytest.mark.parametrize("missing", ("control", "invocation"))
def test_noninteractive_policy_requires_run_owned_location(tmp_path, missing):
    context = LaunchContext(
        root=tmp_path,
        project="project-id",
        project_dir=tmp_path / "project",
        python="/venv/bin/python",
        tools_port=8123,
        model=None,
        setup={},
        control_dir=None if missing == "control" else tmp_path / "run" / "control",
        invocation_id=None if missing == "invocation" else "one",
        noninteractive=True,
    )

    with pytest.raises(ValueError, match="control_dir and invocation_id"):
        harness_dir(context, "claude")


def test_role_policy_cannot_weaken_neutral_tool_denial(tmp_path):
    context = LaunchContext(
        root=tmp_path,
        project="project-id",
        project_dir=tmp_path / "project",
        python="/venv/bin/python",
        tools_port=8123,
        model=None,
        setup={},
        control_dir=tmp_path / "run" / "control",
        invocation_id="one",
        noninteractive=True,
        role_policy={"role": "helper", "deny_tools": []},
    )

    files, _ = policy_files(
        NeutralConfig([], frozenset({"bash", "shell"})), context, "claude"
    )

    assert json.loads(next(iter(files.values())))["deny_tools"] == ["bash", "shell"]


@pytest.mark.parametrize(
    ("client", "hook_file", "hook_marker"),
    (
        ("claude", "settings.json", "PreToolUse"),
        ("codex", ".codex/hooks.json", "PreToolUse"),
        ("vibe", ".vibe/hooks.toml", "magnolia-pre-tool"),
    ),
)
def test_noninteractive_role_env_all_three_clients(
    tmp_path, client, hook_file, hook_marker
):
    project = tmp_path / "project"
    control = tmp_path / "run" / "control"
    context = LaunchContext(
        root=tmp_path,
        project="project-id",
        project_dir=project,
        python="/venv/bin/python",
        tools_port=8123,
        model="selected-model",
        setup={},
        run_dir=tmp_path / "run",
        control_dir=control,
        work_dir=tmp_path / "run" / "work",
        role="reviewer",
        invocation_id=f"{client}-reviewer",
        noninteractive=True,
        role_policy={"role": "reviewer", "mcp_tool_allowlist": []},
    )

    launch = RENDERERS[client](NeutralConfig([], frozenset({"bash"})), context)

    policy_path = control / "executors" / f"{client}-reviewer" / "policy.json"
    assert launch.env["MAGNOLIA_HOOK_POLICY"] == str(policy_path.resolve())
    assert launch.env["MAGNOLIA_HOOK_POLICY_REQUIRED"] == "1"
    assert launch.env["MAGNOLIA_DOORBELL"] == "0"
    if client == "claude":
        rendered_hook = launch.files[policy_path.parent / hook_file]
    else:
        rendered_hook = launch.files[project / hook_file]
    text = rendered_hook.decode("utf-8")
    assert hook_marker in text
    assert any(
        is_magnolia_hook_command(command)
        for command in _rendered_commands(rendered_hook, client)
    )
    assert not any(
        forbidden in " ".join(launch.argv) + text
        for forbidden in ("--trust", "--dangerously", "bypass")
    )


def _rendered_commands(rendered_hook: bytes, client: str) -> list[str]:
    """Extract hook commands from one client's native configuration."""
    if client == "vibe":
        import tomllib

        hooks = tomllib.loads(rendered_hook.decode("utf-8"))["hooks"]
        return [hook["command"] for hook in hooks]
    hooks = json.loads(rendered_hook)["hooks"]
    return [
        hook["command"]
        for groups in hooks.values()
        for group in groups
        for hook in group["hooks"]
    ]


def _pre_tool_command(launch, project, policy_path, client):
    if client == "claude":
        settings = json.loads(launch.files[policy_path.parent / "settings.json"])
        return settings["hooks"]["PreToolUse"][0]["hooks"][0]["command"]
    if client == "codex":
        hooks = json.loads(launch.files[project / ".codex/hooks.json"])
        return hooks["hooks"]["PreToolUse"][0]["hooks"][0]["command"]
    import tomllib

    hooks = tomllib.loads(launch.files[project / ".vibe/hooks.toml"].decode())["hooks"]
    return next(hook["command"] for hook in hooks if hook["name"] == "magnolia-pre-tool")


def _worker_policy(project, work):
    return {
        "schema_version": "magnolia-role-v1",
        "role": "worker",
        "policy_role": "campaign-worker",
        "canonical_read_roots": [str(project)],
        "read_deny_patterns": ["**/*.env*", "**/*.env*/**"],
        "deny_path_forms": ["requested", "resolved"],
        "edit_roots": [str(work)],
        "effective_capabilities": [
            "native_read", "native_glob", "native_grep", "native_edit",
        ],
        "mcp_tool_allowlist": [
            "mcp:compchem-memory.memory_get_context",
            "mcp:compchem-memory.memory_search",
            "mcp:compchem-memory.memory_get_run_history",
        ],
        "delegation_policy": "deny",
        "required_capability_identity": "fixture-identity",
        "deny_unknown_tools": True,
        "deny_shell": True,
        "deny_unconfined_code_execution": True,
    }


def _native_payload(client, fixture, path):
    payload = json.loads(json.dumps(fixture))
    payload["cwd"] = str(path.parent)
    path_field = "path" if client == "vibe" else "file_path"
    payload["tool_input"][path_field] = str(path)
    return payload


@pytest.mark.parametrize("client", ("claude", "codex", "vibe"))
def test_rendered_required_hook_preserves_allowed_stdin_and_denies(client, tmp_path):
    project = tmp_path / "project"
    work = project / "run" / "work"
    control = tmp_path / "run" / "control"
    work.mkdir(parents=True)
    allowed_file = project / "allowed.txt"
    allowed_file.write_text("ok", encoding="utf-8")
    denied_file = tmp_path / "outside.txt"
    denied_file.write_text("secret", encoding="utf-8")
    context = LaunchContext(
        root=ROOT,
        project="project-id",
        project_dir=project,
        python=sys.executable,
        tools_port=8123,
        model="selected-model",
        setup={},
        run_dir=tmp_path / "run",
        control_dir=control,
        work_dir=work,
        role="worker",
        invocation_id=f"{client}-worker",
        noninteractive=True,
        role_policy=_worker_policy(project, work),
    )
    launch = RENDERERS[client](NeutralConfig([], frozenset({"bash"})), context)
    policy_path = control / "executors" / f"{client}-worker" / "policy.json"
    policy_path.parent.mkdir(parents=True, exist_ok=True)
    policy_path.write_bytes(launch.files[policy_path])
    command = _pre_tool_command(launch, project, policy_path, client)
    env = dict(os.environ)
    env.update(launch.env)
    env["MAGNOLIA_HOOK_POLICY_SHA256"] = hashlib.sha256(
        policy_path.read_bytes()
    ).hexdigest()
    env["MAGNOLIA_CONTROL_DIR"] = str(control)
    fixture = json.loads(
        (FIXTURES / client / "synthetic" / "native-tools.json").read_text()
    )
    allowed_payloads = [_native_payload(client, fixture["read"], allowed_file)]
    if client in {"claude", "vibe"}:
        allowed_payloads.append(_native_payload(client, fixture["write"], work / "new.txt"))
    allowed_payloads.append({
        "session_id": f"synthetic-{client}",
        "cwd": str(project),
        "hook_event_name": "pre_tool" if client == "vibe" else "PreToolUse",
        "tool_name": native_tool(client, "compchem-memory", "memory_search"),
        "tool_input": {"query": "allowed"},
    })

    for payload in allowed_payloads:
        result = subprocess.run(
            ["sh", "-c", command], input=json.dumps(payload), capture_output=True,
            text=True, timeout=30, env=env,
        )
        assert result.returncode == 0
        assert result.stdout == ""

    denied_payload = _native_payload(client, fixture["read"], denied_file)
    denied = subprocess.run(
        ["sh", "-c", command], input=json.dumps(denied_payload), capture_output=True,
        text=True, timeout=30, env=env,
    )
    denial = json.loads(denied.stdout)
    if client == "vibe":
        assert denial["decision"] == "deny"
    else:
        assert denial["hookSpecificOutput"]["permissionDecision"] == "deny"
    assert denied.returncode == 0


@pytest.mark.parametrize("client", ("claude", "codex", "vibe"))
@pytest.mark.parametrize("failure", ("missing-interpreter", "import-failure"))
def test_noninteractive_pre_tool_hook_failure_blocks(
    tmp_path, client, failure
):
    project = tmp_path / "project"
    control = tmp_path / "run" / "control"
    interpreter = tmp_path / "missing-python"
    if failure == "import-failure":
        interpreter.write_text(
            "#!/bin/sh\necho 'No module named compchem_memory' >&2\nexit 1\n",
            encoding="utf-8",
        )
        interpreter.chmod(0o755)
    context = LaunchContext(
        root=tmp_path,
        project="project-id",
        project_dir=project,
        python=str(interpreter),
        tools_port=8123,
        model="selected-model",
        setup={},
        run_dir=tmp_path / "run",
        control_dir=control,
        work_dir=tmp_path / "run" / "work",
        role="reviewer",
        invocation_id=f"{client}-reviewer",
        noninteractive=True,
        role_policy={"role": "reviewer", "mcp_tool_allowlist": []},
    )
    launch = RENDERERS[client](NeutralConfig([], frozenset({"bash"})), context)
    policy_path = control / "executors" / f"{client}-reviewer" / "policy.json"
    command = _pre_tool_command(launch, project, policy_path, client)
    env = dict(os.environ)
    env.update(launch.env)

    result = subprocess.run(
        ["sh", "-c", command],
        input="{}", capture_output=True, text=True, timeout=10, env=env,
    )

    payload = json.loads(result.stdout)
    if client == "vibe":
        assert payload["decision"] == "deny"
    else:
        assert payload["hookSpecificOutput"]["permissionDecision"] == "deny"
    assert result.returncode == 0


def test_noninteractive_pre_tool_hook_timeout_blocks():
    command = _required_pre_tool_command("sleep 5", "claude", timeout_s=1)

    assert command.startswith("/bin/sh -c ")

    result = subprocess.run(
        ["sh", "-c", command],
        input="{}", capture_output=True, text=True, timeout=4,
    )

    payload = json.loads(result.stdout)
    assert payload["hookSpecificOutput"]["permissionDecision"] == "deny"
    assert result.returncode == 0


def test_required_hook_ignores_editable_install_and_forged_source_pycache(tmp_path):
    installation = tmp_path / "repo" / "opencode_cc_mem"
    source = installation / "mcp-servers" / "compchem-memory" / "src"
    shutil.copytree(
        ROOT / "mcp-servers" / "compchem-memory" / "src" / "compchem_memory",
        source / "compchem_memory",
    )
    repository_venv = installation.parent / ".venv"
    interpreter = repository_venv / "bin" / "python3"
    interpreter.parent.mkdir(parents=True)
    interpreter.symlink_to(sys.executable)
    (repository_venv / "pyvenv.cfg").write_text(
        f"home = {Path(sys.executable).resolve().parent}\n"
        "include-system-site-packages = true\n",
        encoding="utf-8",
    )
    editable = tmp_path / "editable"
    editable_hook = editable / "compchem_memory" / "hook"
    editable_hook.mkdir(parents=True)
    (editable / "compchem_memory" / "__init__.py").write_text("")
    (editable_hook / "__init__.py").write_text("")
    (editable_hook / "__main__.py").write_text(
        "def main():\n    print('{\"marker\": \"editable\"}')\n"
    )
    site_packages = (
        repository_venv
        / "lib"
        / f"python{sys.version_info.major}.{sys.version_info.minor}"
        / "site-packages"
    )
    site_packages.mkdir(parents=True)
    (site_packages / "editable.pth").write_text(str(editable) + "\n")
    main_source = source / "compchem_memory" / "hook" / "__main__.py"
    malicious = tmp_path / "malicious.py"
    malicious.write_text(
        "def main():\n    print('{\"marker\": \"forged\"}')\n"
    )
    forged = Path(importlib.util.cache_from_source(str(main_source)))
    forged.parent.mkdir(exist_ok=True)
    py_compile.compile(
        str(malicious),
        cfile=str(forged),
        doraise=True,
        invalidation_mode=py_compile.PycInvalidationMode.UNCHECKED_HASH,
    )
    context = LaunchContext(
        root=installation,
        project="project-id",
        project_dir=tmp_path / "project",
        python=str(interpreter),
        tools_port=8123,
        model="haiku",
        setup={},
        run_dir=tmp_path / "run",
        control_dir=tmp_path / "run" / "control",
        work_dir=tmp_path / "run" / "work",
        role="worker",
        invocation_id="worker-one",
        noninteractive=True,
    )
    command = hook_command(context, "claude", "pre-tool")
    cache = tmp_path / "run" / "control" / "executors" / "worker-one" / "pycache"

    result = subprocess.run(
        command,
        shell=True,
        input="{}",
        capture_output=True,
        text=True,
        timeout=20,
        env={
            **os.environ,
            "PYTHONPYCACHEPREFIX": str(cache),
            "MAGNOLIA_HOOK_POLICY_REQUIRED": "1",
        },
    )

    assert "editable" not in result.stdout
    assert "forged" not in result.stdout
    assert json.loads(result.stdout)["hookSpecificOutput"]["permissionDecision"] == "deny"
    assert result.returncode == 0


@pytest.mark.parametrize("mode", ("--memory-server", "--background"))
def test_isolated_runner_ignores_project_local_packages_and_stdlib_shadows(
    tmp_path, mode
):
    installation = tmp_path / "repo" / "opencode_cc_mem"
    source = installation / "mcp-servers" / "compchem-memory" / "src"
    shutil.copytree(
        ROOT / "mcp-servers" / "compchem-memory" / "src" / "compchem_memory",
        source / "compchem_memory",
    )
    marker = tmp_path / "loaded.json"
    target = "server.py" if mode == "--memory-server" else "hook/background.py"
    (source / "compchem_memory" / target).write_text(
        "import json, os, compchem_memory\n"
        "from pathlib import Path\n"
        "def _record():\n"
        "    Path(os.environ['ISOLATION_MARKER']).write_text("
        "json.dumps({'json': json.__file__, 'package': compchem_memory.__file__}))\n"
        + (
            "class _Mcp:\n    def run(self): _record()\n"
            "mcp = _Mcp()\n"
            if mode == "--memory-server"
            else "def main(): _record()\n"
        ),
        encoding="utf-8",
    )
    project = tmp_path / "project"
    local_package = project / "compchem_memory"
    local_package.mkdir(parents=True)
    trap = project / "project-imported"
    (local_package / "__init__.py").write_text(
        f"from pathlib import Path\nPath({str(trap)!r}).write_text('package')\n",
        encoding="utf-8",
    )
    (project / "json.py").write_text(
        f"from pathlib import Path\nPath({str(trap)!r}).write_text('json')\n",
        encoding="utf-8",
    )
    runner = source / "compchem_memory" / "hook" / "runner.py"

    result = subprocess.run(
        [sys.executable, "-I", str(runner), mode],
        cwd=project,
        capture_output=True,
        text=True,
        timeout=20,
        env={**os.environ, "ISOLATION_MARKER": str(marker)},
    )

    assert result.returncode == 0, result.stderr
    loaded = json.loads(marker.read_text(encoding="utf-8"))
    assert loaded["package"] == str(source / "compchem_memory" / "__init__.py")
    assert loaded["json"] != str(project / "json.py")
    assert not trap.exists()


def _runner_tree(root):
    source = root / "mcp-servers" / "compchem-memory" / "src"
    package = source / "compchem_memory"
    shutil.copytree(
        ROOT / "mcp-servers" / "compchem-memory" / "src" / "compchem_memory",
        package,
        ignore=shutil.ignore_patterns("__pycache__"),
    )
    return package / "hook" / "runner.py"


def _run_isolated_runner(runner, tmp_path):
    return subprocess.run(
        [sys.executable, "-I", str(runner), "claude", "pre-tool"],
        input="{}", capture_output=True, text=True, timeout=20,
        env={
            "PATH": os.environ.get("PATH", ""),
            "MAGNOLIA_HOOK_POLICY_REQUIRED": "1",
            "PYTHONPYCACHEPREFIX": str(tmp_path / "pycache"),
        },
    )


@pytest.mark.parametrize("location", ("file-symlink", "directory-symlink", "wrong-tree"))
def test_runner_refuses_relocated_or_symlinked_source(tmp_path, location):
    runner = _runner_tree(tmp_path / "repo")
    alternate = _runner_tree(tmp_path / "alternate")
    (alternate.parent / "__main__.py").write_text(
        "def main():\n    print('ALT_TREE_RAN')\n"
    )
    if location == "file-symlink":
        runner.unlink()
        runner.symlink_to(alternate)
    elif location == "directory-symlink":
        shutil.rmtree(runner.parent)
        runner.parent.symlink_to(alternate.parent, target_is_directory=True)
    else:
        destination = tmp_path / "unexpected" / "compchem_memory"
        destination.parent.mkdir()
        shutil.copytree(alternate.parent.parent, destination)
        runner = destination / "hook" / "runner.py"
    result = _run_isolated_runner(runner, tmp_path)
    assert result.returncode != 0
    assert "ALT_TREE_RAN" not in result.stdout
    assert "runner" in result.stderr.lower()


@pytest.mark.parametrize("module", ("json", "typing", "re", "pathlib", "fractions", "statistics"))
def test_runner_keeps_standard_library_before_repository_source(tmp_path, module):
    runner = _runner_tree(tmp_path / "repo")
    source = runner.parents[2]
    (source / f"{module}.py").write_text("raise RuntimeError('SHADOW_STDLIB_RAN')\n")
    (runner.parent / "__main__.py").write_text(
        f"import {module}, sys\n"
        "def main():\n"
        f"    print({module}.__file__)\n"
        "    print(*sys.path, sep=chr(10))\n"
    )
    result = _run_isolated_runner(runner, tmp_path)
    assert result.returncode == 0, result.stderr
    lines = result.stdout.splitlines()
    assert not Path(lines[0]).is_relative_to(source)
    search = lines[1:]
    assert str(source) not in search
    assert any(
        Path(path).name in {"site-packages", "dist-packages"}
        for path in search
    )


def test_runner_never_puts_repository_source_on_sys_path(tmp_path):
    runner = _runner_tree(tmp_path / "repo")
    source = runner.parents[2]
    marker = "SHADOW_MODULE_RAN"
    for module in ("_winapi", "nt", "json"):
        (source / f"{module}.py").write_text(
            f"print({marker!r} + ':{module}')\n", encoding="utf-8"
        )
    (runner.parent / "__main__.py").write_text(
        "import importlib, sys\n"
        "def main():\n"
        "    for name in ('_winapi', 'nt', 'json'):\n"
        "        sys.modules.pop(name, None)\n"
        "        try:\n"
        "            importlib.import_module(name)\n"
        "        except ImportError:\n"
        "            pass\n"
        "    print(*sys.path, sep=chr(10))\n",
        encoding="utf-8",
    )

    result = _run_isolated_runner(runner, tmp_path)

    assert result.returncode == 0, result.stderr
    assert marker not in result.stdout
    assert str(source) not in result.stdout.splitlines()
