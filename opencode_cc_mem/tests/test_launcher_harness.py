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
