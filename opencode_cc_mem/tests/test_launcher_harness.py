"""Characterisation of the `magnolia` launcher's OpenCode path.

Runs the real script under /bin/bash in a temporary tree that mirrors
`<tmp>/opencode_cc_mem/softwares/bin/magnolia`, with stub client binaries on
PATH and MAGNOLIA_DRY_RUN=1 (print what exec would receive, start no daemon).
The snapshot must not change in later tasks: it pins today's OpenCode launch.
"""
import json
import os
import shutil
import subprocess
import sys
import time
from pathlib import Path

HERE = Path(__file__).resolve().parent
OPENCODE_DIR = HERE.parent
LAUNCHER = OPENCODE_DIR / "softwares" / "bin" / "magnolia"
STUBS = HERE / "stubs"
SNAPSHOT = HERE / "snapshots" / "opencode.json"
PROJECT = "demo"
ALLOWLIST = {
    "MAGNOLIA_PROJECT_DIR", "MAGNOLIA_PROJECT_WORKSPACE", "MAGNOLIA_WORKSPACE_ROOT",
    "MAGNOLIA_HOOK_POLICY", "MAGNOLIA_OPENCODE_V2", "COMPCHEM_TOOLS_PORT",
    "MCP_TOOL_TIMEOUT",
}


def make_workspace(tmp_path: Path) -> Path:
    """Temporary tree the launcher can run in without the real repo state."""
    root = tmp_path / "ws"
    bin_dir = root / "opencode_cc_mem" / "softwares" / "bin"
    bin_dir.mkdir(parents=True)
    shutil.copy2(LAUNCHER, bin_dir / "magnolia")
    oc = root / "opencode_cc_mem"
    shutil.copy2(OPENCODE_DIR / "opencode.json.template", oc / "opencode.json.template")
    shutil.copy2(OPENCODE_DIR / "AGENTS.md", oc / "AGENTS.md")
    shutil.copytree(OPENCODE_DIR / "rules", oc / "rules")
    (oc / ".magnolia").mkdir()
    (oc / "projects" / PROJECT / ".magnolia").mkdir(parents=True)
    venv_bin = root / ".venv" / "bin"
    venv_bin.mkdir(parents=True)
    (venv_bin / "python3").symlink_to(sys.executable)
    (venv_bin / "activate").write_text("")
    stub_bin = tmp_path / "stubbin"
    stub_bin.mkdir()
    for stub in STUBS.iterdir():
        shutil.copy2(stub, stub_bin / stub.name)
    (tmp_path / "home").mkdir()
    # Daemon stubs: each writes a sentinel when invoked, so the tests can tell
    # whether the launcher started it.
    (bin_dir / "compchem-tools-daemon.sh").write_text(
        '#!/bin/bash\ntouch "$SENTINEL_DIR/daemon-ran-tools"\n')
    agents = bin_dir / "magnolia-agents-daemon"
    agents.write_text(
        "import os, pathlib\n"
        "pathlib.Path(os.environ['SENTINEL_DIR'], 'daemon-ran-agents').touch()\n")
    return root


def run_launcher(tmp_workspace: Path, args, env=None) -> dict:
    """Run the launcher in dry-run mode; return its JSON object."""
    tmp_path = tmp_workspace.parent
    stub_bin = tmp_path / "stubbin"
    full_env = {
        "PATH": f"{stub_bin}:/usr/bin:/bin:/usr/sbin:/sbin",
        "HOME": str(tmp_path / "home"),
        "MAGNOLIA_ALLOW_NONMASTER": "1",
        "MAGNOLIA_DRY_RUN": "1",
        "MAGNOLIA_OPENCODE_V2_BIN": str(stub_bin / "opencode-v2"),
        "MAGNOLIA_AGENTS": "1",
        "SENTINEL_DIR": str(tmp_path),
        "STUB_LOG": str(tmp_path / "stub.log"),
    }
    full_env.update(env or {})
    script = tmp_workspace / "opencode_cc_mem" / "softwares" / "bin" / "magnolia"
    proc = subprocess.run(
        ["/bin/bash", str(script), *args],
        env=full_env, capture_output=True, text=True, timeout=60, stdin=subprocess.DEVNULL,
    )
    assert proc.returncode == 0, proc.stderr
    if full_env["MAGNOLIA_DRY_RUN"] != "1":
        return json.loads(proc.stdout)
    # stdout is exactly one JSON object: a real exec would have printed the stub's.
    return json.loads(proc.stdout)


def _tools_port(project: str) -> int:
    """tools_port_for_project: 8201 + cksum(project) % 90 (v2 base)."""
    crc = subprocess.run(["cksum"], input=project.encode(), capture_output=True).stdout.split()[0]
    return 8201 + int(crc) % 90


def test_opencode_launch_unchanged(tmp_path):
    ws = make_workspace(tmp_path)
    out = run_launcher(ws, [PROJECT], {"MAGNOLIA_HOOK_POLICY": "enforce", "MCP_TOOL_TIMEOUT": "123"})
    stub = str(tmp_path / "stubbin" / "opencode-v2")
    assert out["argv"] == [stub]
    project_dir = ws / "opencode_cc_mem" / "projects" / PROJECT
    assert Path(out["cwd"]).resolve() == project_dir.resolve()
    env = dict(out["env"])
    root = env.pop("MAGNOLIA_WORKSPACE_ROOT")
    assert Path(root).resolve() == (ws / "opencode_cc_mem").resolve()
    assert env == {
        "MAGNOLIA_PROJECT_DIR": ".",
        "MAGNOLIA_PROJECT_WORKSPACE": "1",
        "MAGNOLIA_HOOK_POLICY": "enforce",
        "MAGNOLIA_OPENCODE_V2": "1",
        "COMPCHEM_TOOLS_PORT": str(_tools_port(PROJECT)),
        "MCP_TOOL_TIMEOUT": "123",
    }
    # The binary check calls the stub's --version, exactly once.
    calls = [json.loads(line) for line in (tmp_path / "stub.log").read_text().splitlines()]
    assert calls == [{"name": "opencode-v2", "argv": ["--version"]}]


def test_dry_run_starts_no_daemon(tmp_path):
    ws = make_workspace(tmp_path)
    run_launcher(ws, [PROJECT])
    time.sleep(0.5)  # the agents daemon would be started in the background
    assert not (tmp_path / "daemon-ran-tools").exists()
    assert not (tmp_path / "daemon-ran-agents").exists()


def test_real_launch_starts_both_daemons(tmp_path):
    """Control: without the dry run the same tree runs both daemons and execs the stub."""
    ws = make_workspace(tmp_path)
    out = run_launcher(ws, [PROJECT], {"MAGNOLIA_DRY_RUN": "0"})
    assert out["argv"] == ["opencode-v2"]  # the stub itself ran
    assert (tmp_path / "daemon-ran-tools").exists()
    deadline = time.time() + 3
    while time.time() < deadline and not (tmp_path / "daemon-ran-agents").exists():
        time.sleep(0.1)
    assert (tmp_path / "daemon-ran-agents").exists()


def test_dry_run_prints_no_secret_names(tmp_path):
    """Guards against a future widening of the allowlist to secret-looking names."""
    ws = make_workspace(tmp_path)
    out = run_launcher(ws, [PROJECT], {"OPENAI_API_KEY": "sk-sentinel", "MY_TOKEN": "t"})
    assert not any(w in k for k in out["env"] for w in ("KEY", "TOKEN", "SECRET", "PASSWORD"))
    assert "sk-sentinel" not in json.dumps(out)


def test_opencode_config_snapshot(tmp_path):
    ws = make_workspace(tmp_path)
    run_launcher(ws, [PROJECT])
    rendered = (ws / "opencode_cc_mem" / "projects" / PROJECT / "opencode.json").read_text()
    for prefix in sorted({str(tmp_path), str(tmp_path.resolve())}, key=len, reverse=True):
        rendered = rendered.replace(prefix, "@@TMP@@")
    if os.environ.get("UPDATE_SNAPSHOT") == "1":
        SNAPSHOT.write_text(rendered)
    assert SNAPSHOT.exists(), "snapshot missing; record it with UPDATE_SNAPSHOT=1"
    assert rendered == SNAPSHOT.read_text()


# ---- --harness claude|codex|vibe (Task 9) ---------------------------------

PKG_SRC = OPENCODE_DIR / "mcp-servers" / "compchem-memory" / "src"


def run_raw(ws: Path, args, env=None, stubs=None, dry=True):
    """Run the launcher and return the CompletedProcess (any exit code).

    `stubs` names the directory put first on PATH (default: all stubs). The
    workspace python is a bare symlink to the test interpreter, so the package
    is made importable through PYTHONPATH.
    """
    tmp_path = ws.parent
    stub_bin = stubs or tmp_path / "stubbin"
    full_env = {
        "PATH": f"{stub_bin}:/usr/bin:/bin:/usr/sbin:/sbin",
        "HOME": str(tmp_path / "home"),
        "MAGNOLIA_ALLOW_NONMASTER": "1",
        "MAGNOLIA_DRY_RUN": "1" if dry else "0",
        "MAGNOLIA_OPENCODE_V2_BIN": str(tmp_path / "stubbin" / "opencode-v2"),
        "MAGNOLIA_AGENTS": "1",
        "SENTINEL_DIR": str(tmp_path),
        "STUB_LOG": str(tmp_path / "stub.log"),
        "PYTHONPATH": str(PKG_SRC),
    }
    full_env.update(env or {})
    script = ws / "opencode_cc_mem" / "softwares" / "bin" / "magnolia"
    return subprocess.run(
        ["/bin/bash", str(script), *args],
        env=full_env, capture_output=True, text=True, timeout=60, stdin=subprocess.DEVNULL,
    )


def stubs_without(tmp_path: Path, *names: str) -> Path:
    """A stub directory lacking the named binaries."""
    folder = tmp_path / "stubbin-partial"
    folder.mkdir()
    for stub in STUBS.iterdir():
        if stub.name not in names:
            shutil.copy2(stub, folder / stub.name)
    return folder


def write_setup(ws: Path, data: dict) -> Path:
    path = ws / "opencode_cc_mem" / ".magnolia" / "llm-setup.json"
    path.write_text(json.dumps(data))
    return path


def dry_json(proc) -> dict:
    assert proc.returncode == 0, proc.stderr
    return json.loads(proc.stdout)


def test_harness_flag_beats_setup_key(tmp_path):
    ws = make_workspace(tmp_path)
    write_setup(ws, {"harness": "codex"})
    out = dry_json(run_raw(ws, ["--harness", "claude", PROJECT]))
    assert out["argv"][0] == "claude"


def test_setup_key_selects_harness(tmp_path):
    ws = make_workspace(tmp_path)
    write_setup(ws, {"harness": "codex"})
    out = dry_json(run_raw(ws, [PROJECT]))
    assert out["argv"][0] == "codex"
    # No key and no flag: the default stays OpenCode.
    write_setup(ws, {"main_model": "x/y"})
    out = dry_json(run_raw(ws, [PROJECT]))
    assert out["argv"] == [str(tmp_path / "stubbin" / "opencode-v2")]


def test_unknown_harness_is_an_error(tmp_path):
    ws = make_workspace(tmp_path)
    proc = run_raw(ws, ["--harness", "emacs", PROJECT])
    assert proc.returncode == 1
    assert "emacs" in proc.stderr and proc.stdout == ""
    write_setup(ws, {"harness": "emacs"})
    proc = run_raw(ws, [PROJECT])
    assert proc.returncode == 1 and "emacs" in proc.stderr


def test_missing_client_binary_exits_before_daemons(tmp_path):
    ws = make_workspace(tmp_path)
    stubs = stubs_without(tmp_path, "claude")
    proc = run_raw(ws, ["--harness", "claude", PROJECT], stubs=stubs, dry=False)
    assert proc.returncode == 1
    assert "claude" in proc.stderr
    time.sleep(0.5)
    assert not (tmp_path / "daemon-ran-tools").exists()
    assert not (tmp_path / "daemon-ran-agents").exists()


def test_claude_launch_without_any_opencode_binary(tmp_path):
    ws = make_workspace(tmp_path)
    stubs = stubs_without(tmp_path, "opencode", "opencode-v2")
    env = {"MAGNOLIA_OPENCODE_V2_BIN": str(tmp_path / "absent" / "opencode")}
    out = dry_json(run_raw(ws, ["--harness", "claude", PROJECT], env=env, stubs=stubs))
    assert out["argv"][0] == "claude"
    # Nothing asked the OpenCode binaries anything.
    log = tmp_path / "stub.log"
    assert not log.exists() or "opencode" not in log.read_text()


def test_claude_launch_argv(tmp_path):
    ws = make_workspace(tmp_path)
    out = dry_json(run_raw(ws, ["--harness", "claude", PROJECT]))
    project_dir = (ws / "opencode_cc_mem" / "projects" / PROJECT).resolve()
    assert Path(out["cwd"]).resolve() == project_dir
    env = out["env"]
    assert os.path.isabs(env["MAGNOLIA_PROJECT_DIR"])
    assert Path(env["MAGNOLIA_PROJECT_DIR"]).resolve() == project_dir
    policy = Path(env["MAGNOLIA_HOOK_POLICY"])
    assert policy.name == "policy.json" and policy.is_file()
    assert env["MCP_TOOL_TIMEOUT"] == "660000"
    assert set(env) <= ALLOWLIST
    argv = out["argv"]
    assert argv[0] == "claude" and "--strict-mcp-config" in argv
    assert not any(a.startswith("--dangerously") or a in ("--bare", "--auto-approve", "--trust")
                   for a in argv)
    # render_config still ran: the daemon and campaigns read opencode.json.
    assert (project_dir / "opencode.json").is_file()
    assert not (tmp_path / "daemon-ran-tools").exists()


def test_alternate_launch_handles_awkward_project_and_installation_paths(tmp_path):
    base = tmp_path / 'installation "quoted" é'
    base.mkdir()
    ws = make_workspace(base)
    project = 'project "quoted" 雪 with spaces'
    projects = ws / "opencode_cc_mem" / "projects"
    (projects / PROJECT).rename(projects / project)

    out = dry_json(run_raw(ws, ["--harness", "claude", project]))

    project_dir = (projects / project).resolve()
    assert Path(out["cwd"]).resolve() == project_dir
    assert Path(out["env"]["MAGNOLIA_PROJECT_DIR"]).resolve() == project_dir
    rendered = json.loads((project_dir / "opencode.json").read_text(encoding="utf-8"))
    memory = rendered["mcp"]["servers"]["compchem-memory"]
    assert memory["environment"]["MAGNOLIA_PROJECT_DIR"] == str(project_dir)


def test_usage_prints_setup_example_without_executing_it(tmp_path):
    ws = make_workspace(tmp_path)
    setup = tmp_path / "stubbin" / "setup"
    setup.write_text('#!/bin/bash\ntouch "$SENTINEL_DIR/usage-ran-setup"\n', encoding="utf-8")
    setup.chmod(0o755)

    proc = run_raw(ws, ["--help"])

    assert proc.returncode == 0
    assert proc.stderr == ""
    assert "setup --harness H [--model M]" in proc.stdout
    assert not (tmp_path / "usage-ran-setup").exists()


def test_model_from_flag_and_setup(tmp_path):
    ws = make_workspace(tmp_path)
    write_setup(ws, {"models": {"claude": "sonnet"}})
    argv = dry_json(run_raw(ws, ["--harness", "claude", PROJECT]))["argv"]
    assert argv[argv.index("--model") + 1] == "sonnet"
    argv = dry_json(run_raw(ws, ["--harness", "claude", "--model", "opus", PROJECT]))["argv"]
    assert argv[argv.index("--model") + 1] == "opus"


def test_dry_run_hides_sentinel_credentials(tmp_path):
    ws = make_workspace(tmp_path)
    env = {"MAGNOLIA_LLM_API_KEY": "sk-sentinel-main", "MAGNOLIA_CRITIC_API_KEY": "sk-sentinel-critic"}
    for harness in ("claude", "codex", "vibe"):
        proc = run_raw(ws, ["--harness", harness, PROJECT], env=env)
        assert "sk-sentinel" not in proc.stdout + proc.stderr
        assert not any(w in k for k in dry_json(proc)["env"]
                       for w in ("KEY", "TOKEN", "SECRET", "PASSWORD"))


def test_real_claude_launch_execs_client_after_daemons(tmp_path):
    ws = make_workspace(tmp_path)
    proc = run_raw(ws, ["--harness", "claude", PROJECT], dry=False)
    assert proc.returncode == 0, proc.stderr
    out = json.loads(proc.stdout)  # the stub ran
    assert out["argv"][0] == "claude" and "--settings" in out["argv"]
    assert (tmp_path / "daemon-ran-tools").exists()


def test_setup_set_json_keeps_other_keys(tmp_path):
    ws = make_workspace(tmp_path)
    path = write_setup(ws, {"main_model": "a/b", "models": {"codex": "gpt"}, "memory_enabled": True})
    script = ws / "opencode_cc_mem" / "softwares" / "bin" / "magnolia"
    # Source the helpers only: run the function through a tiny driver.
    driver = (
        f'LLM_SETUP="{path}"; PYTHON_CMD="{ws}/.venv/bin/python3"\n'
        + _helper_source(script, "setup_get_json") + _helper_source(script, "setup_set_json")
        + 'setup_set_json models.claude \'"sonnet"\'\nsetup_set_json harness \'"claude"\'\n'
        + 'echo "$(setup_get_json models.claude)|$(setup_get_json models.codex)|$(setup_get_json nope.x)|$(setup_get_json harness)"\n'
    )
    proc = subprocess.run(["/bin/bash", "-c", driver], capture_output=True, text=True)
    assert proc.returncode == 0, proc.stderr
    assert proc.stdout.strip() == "sonnet|gpt||claude"
    assert json.loads(path.read_text()) == {
        "main_model": "a/b", "memory_enabled": True, "harness": "claude",
        "models": {"codex": "gpt", "claude": "sonnet"},
    }
    assert not list(path.parent.glob("*.tmp*"))


def _helper_source(script: Path, name: str) -> str:
    """The text of one top-level shell function of the launcher."""
    lines = script.read_text().splitlines()
    start = next(i for i, line in enumerate(lines) if line.startswith(f"{name}() {{"))
    end = next(i for i in range(start + 1, len(lines)) if lines[i] == "}")
    return "\n".join(lines[start:end + 1]) + "\n"


def test_setup_harness_writes_after_check_passes(tmp_path):
    ws = make_workspace(tmp_path)
    path = write_setup(ws, {"main_model": "a/b"})
    proc = run_raw(ws, ["setup", "--harness", "claude", "--model", "sonnet", PROJECT],
                   env={"STUB_SAY": "MAIN_MODEL_OK"})
    assert proc.returncode == 0, proc.stderr
    assert proc.stdout.count("\n") == 1 and "claude" in proc.stdout and "OpenCode" not in proc.stdout
    data = json.loads(path.read_text())
    assert data["harness"] == "claude" and data["models"]["claude"] == "sonnet"
    assert data["main_model"] == "a/b"
    calls = [json.loads(line) for line in (tmp_path / "stub.log").read_text().splitlines()]
    assert calls == [{"name": "claude",
                      "argv": ["-p", "Reply with exactly: MAIN_MODEL_OK", "--model", "sonnet"]}]


def test_setup_harness_failure_writes_nothing(tmp_path):
    ws = make_workspace(tmp_path)
    path = write_setup(ws, {"main_model": "a/b"})
    for harness in ("claude", "codex", "vibe"):
        proc = run_raw(ws, ["setup", "--harness", harness, "--model", "m", PROJECT],
                       env={"STUB_SAY": "no answer"})
        assert proc.returncode == 1
        assert harness in proc.stderr
    assert json.loads(path.read_text()) == {"main_model": "a/b"}


def test_setup_harness_call_shapes(tmp_path):
    ws = make_workspace(tmp_path)
    run_raw(ws, ["setup", "--harness", "codex", "--model", "gpt-x", PROJECT], env={"STUB_SAY": "MAIN_MODEL_OK"})
    run_raw(ws, ["setup", "--harness", "vibe", PROJECT], env={"STUB_SAY": "MAIN_MODEL_OK"})
    calls = [json.loads(line) for line in (tmp_path / "stub.log").read_text().splitlines()]
    assert calls[0] == {"name": "codex", "argv": ["exec", "-m", "gpt-x", "Reply with exactly: MAIN_MODEL_OK"]}
    assert calls[1] == {"name": "vibe", "argv": ["-p", "Reply with exactly: MAIN_MODEL_OK"]}


def test_setup_check_needs_exit_zero_and_exact_line(tmp_path):
    ws = make_workspace(tmp_path)
    path = write_setup(ws, {"main_model": "a/b"})
    cases = [
        ({}, 1),  # the stub echoes the prompt as JSON: must fail
        ({"STUB_SAY": "Reply with exactly: MAIN_MODEL_OK"}, 1),
        ({"STUB_SAY": "MAIN_MODEL_OK", "STUB_EXIT": "3"}, 1),  # token but nonzero exit
        ({"STUB_SAY": "  MAIN_MODEL_OK  "}, 0),
    ]
    for env, code in cases:
        proc = run_raw(ws, ["setup", "--harness", "claude", "--model", "m", PROJECT], env=env)
        assert proc.returncode == code, (env, proc.stderr)
        if code:
            assert json.loads(path.read_text()) == {"main_model": "a/b"}


def test_model_flag_is_an_error_for_opencode(tmp_path):
    ws = make_workspace(tmp_path)
    for args in (["--model", "x", PROJECT], ["--model=x", PROJECT]):
        proc = run_raw(ws, args)
        assert proc.returncode == 1 and "unknown option" in proc.stderr


def test_non_string_harness_key_is_ignored(tmp_path):
    ws = make_workspace(tmp_path)
    for bad in (None, 3, ["claude"], {"a": 1}):
        write_setup(ws, {"harness": bad})
        out = dry_json(run_raw(ws, [PROJECT]))
        assert out["argv"] == [str(tmp_path / "stubbin" / "opencode-v2")]


def test_launch_failure_prints_only_the_cli_line(tmp_path):
    ws = make_workspace(tmp_path)
    (ws / "opencode_cc_mem" / "opencode.json.template").write_text("{not json")
    proc = run_raw(ws, ["--harness", "claude", PROJECT])
    assert proc.returncode == 1
    assert "not prepared" not in proc.stderr
    assert not (tmp_path / "daemon-ran-tools").exists()
