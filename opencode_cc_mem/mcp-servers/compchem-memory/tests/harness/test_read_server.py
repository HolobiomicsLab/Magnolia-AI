"""Contract tests for the confined ``magnolia-read`` stdio server."""

from __future__ import annotations

import asyncio
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import shutil
import site
import subprocess
import time
import venv

import pytest
from fastmcp import Client
from fastmcp.client.transports import StdioTransport

from compchem_memory.harness.read_server import (
    ReadRefused,
    ReadServer,
    _descriptor_path,
)


def _policy(path: Path, project: Path, *, role: str = "worker") -> Path:
    path.write_text(json.dumps({
        "schema_version": "magnolia-role-v1",
        "role": role,
        "policy_role": f"campaign-{role}",
        "canonical_read_roots": [str(project.resolve())],
        "read_deny_patterns": ["**/*.env*", "**/*.env*/**", "**/secret/**"],
        "deny_path_forms": ["requested", "resolved"],
        "edit_roots": [],
        "effective_capabilities": ["native_read", "native_glob", "native_grep"],
        "mcp_tool_allowlist": [],
        "delegation_policy": "deny",
        "required_capability_identity": "fixture",
        "deny_unknown_tools": True,
        "deny_shell": True,
        "deny_unconfined_code_execution": True,
        "deny_tools": ["bash", "shell", "delegate", "code"],
    }), encoding="utf-8")
    return path


def _server(tmp_path: Path, *, clock=None) -> tuple[ReadServer, Path, Path]:
    tmp_path.mkdir(parents=True, exist_ok=True)
    project = tmp_path / "project"
    outside = tmp_path / "outside"
    project.mkdir()
    outside.mkdir()
    policy = _policy(tmp_path / "policy.json", project)
    return (
        ReadServer.from_policy_file(
            policy,
            cwd=project,
            control_dir=str(tmp_path / "control"),
            clock=clock,
        ),
        project,
        outside,
    )


def test_read_server_confines_reads_to_policy_roots(tmp_path):
    server, project, outside = _server(tmp_path)
    (project / "notes.txt").write_text("allowed\n", encoding="utf-8")
    (project / "Case.ENV").write_text("denied\n", encoding="utf-8")
    (outside / "outside.txt").write_text("outside\n", encoding="utf-8")
    (project / "alias").symlink_to(outside, target_is_directory=True)

    assert server.read_file("notes.txt")["content"] == "allowed\n"
    assert server.read_file(str(project / "notes.txt"))["content"] == "allowed\n"
    assert server.list_dir(".")["entries"] == ["notes.txt"]
    for path in (
        "../outside/outside.txt",
        "alias/outside.txt",
        "Case.ENV",
    ):
        with pytest.raises(ReadRefused):
            server.read_file(path)
    for path in ("~/.ssh/id_rsa", "$HOME/.ssh/id_rsa"):
        with pytest.raises(ReadRefused, match="policy denied"):
            server.read_file(path)


def test_read_server_refuses_without_policy(tmp_path):
    with pytest.raises(ReadRefused, match="MAGNOLIA_HOOK_POLICY"):
        ReadServer.from_environment({}, cwd=tmp_path)
    with pytest.raises(ReadRefused, match="invalid"):
        ReadServer.from_policy_file(tmp_path / "missing.json", cwd=tmp_path)


def test_read_server_requires_explicit_canonical_cwd(tmp_path, monkeypatch):
    project = tmp_path / "project"
    project.mkdir()
    policy = _policy(tmp_path / "policy.json", project)
    fake_home = tmp_path / "home"
    fake_home.mkdir()
    monkeypatch.chdir(fake_home)

    with pytest.raises(ReadRefused, match="canonical cwd"):
        ReadServer.from_policy_file(policy, control_dir=str(tmp_path / "control"))
    with pytest.raises(ReadRefused, match="canonical cwd"):
        ReadServer.from_policy_file(
            policy,
            cwd=Path("../project"),
            control_dir=str(tmp_path / "control"),
        )

    loaded = ReadServer.from_policy_file(
        policy,
        cwd=project.resolve(),
        control_dir=str(tmp_path / "control"),
    )
    with pytest.raises(ReadRefused, match="canonical cwd"):
        ReadServer(loaded.policy, cwd=Path("."))


def test_read_server_refuses_policy_digest_mismatch(tmp_path):
    project = tmp_path / "project"
    project.mkdir()
    policy = _policy(tmp_path / "policy.json", project)
    digest = hashlib.sha256(policy.read_bytes()).hexdigest()
    required_env = {
        "MAGNOLIA_HOOK_POLICY": str(policy),
        "MAGNOLIA_HOOK_POLICY_SHA256": digest,
        "MAGNOLIA_CONTROL_DIR": str(tmp_path / "control"),
    }

    with pytest.raises(ReadRefused, match="control directory"):
        ReadServer.from_environment(
            {
                "MAGNOLIA_HOOK_POLICY": str(policy),
                "MAGNOLIA_HOOK_POLICY_SHA256": digest,
            },
            cwd=project,
        )

    server = ReadServer.from_environment(
        required_env,
        cwd=project,
    )
    (project / "allowed.txt").write_text("ok\n", encoding="utf-8")
    assert server.read_file("allowed.txt")["content"] == "ok\n"

    policy.write_text(policy.read_text(encoding="utf-8") + "\n", encoding="utf-8")
    with pytest.raises(ReadRefused, match="digest"):
        server.read_file("allowed.txt")
    with pytest.raises(ReadRefused, match="digest"):
        ReadServer.from_environment(
            required_env,
            cwd=project,
        )


def test_descriptor_path_requires_opened_inode_identity(tmp_path):
    target = tmp_path / "target.txt"
    target.write_text("opened\n", encoding="utf-8")
    descriptor = os.open(target, os.O_RDONLY)
    try:
        target.unlink()
        target.write_text("replacement\n", encoding="utf-8")
        with pytest.raises(ReadRefused, match="identity|verify"):
            _descriptor_path(descriptor)
    finally:
        os.close(descriptor)


def test_read_server_search_refuses_denied_descendants(tmp_path):
    server, project, outside = _server(tmp_path)
    clean = project / "clean"
    clean.mkdir()
    (clean / "ok.txt").write_text("needle\n", encoding="utf-8")
    assert server.search("needle", "clean")["matches"][0]["line"] == 1

    (project / "secret").mkdir()
    (project / "secret" / "token.txt").write_text("needle\n", encoding="utf-8")
    with pytest.raises(ReadRefused):
        server.search("needle", ".")

    escaping = project / "escaping"
    escaping.mkdir()
    (outside / "loot.txt").write_text("needle\n", encoding="utf-8")
    (escaping / "out").symlink_to(outside, target_is_directory=True)
    with pytest.raises(ReadRefused):
        server.search("needle", "escaping")


def test_read_server_bounds_output(tmp_path, monkeypatch):
    server, project, _outside = _server(tmp_path)
    (project / "many.txt").write_text("".join(f"line-{i}\n" for i in range(20)))
    read = server.read_file("many.txt", offset=3, limit=2)
    assert read["content"] == "line-3\nline-4\n"
    assert read["truncated"] is True
    with pytest.raises(ReadRefused, match="limit"):
        server.read_file("many.txt", limit=server.MAX_READ_LINES + 1)

    monkeypatch.setattr(server, "MAX_SEARCH_RESULTS", 2)
    found = server.search("line", "many.txt")
    assert len(found["matches"]) == 2
    assert found["truncated"] is True
    with pytest.raises(ReadRefused, match="complex"):
        server.search("re:^(line+)+$", "many.txt")


def test_read_server_refuses_binary_reads_and_counts_search_skips(tmp_path):
    server, project, _outside = _server(tmp_path)
    (project / "clean.txt").write_text("needle clean\n", encoding="utf-8")
    (project / "binary.bin").write_bytes(b"needle hidden\n\xff\xfe")
    (project / "late-nul.txt").write_bytes(
        b"needle hidden\n" + b"text\n" * 20 + b"late\x00nul\n"
    )

    for name in ("binary.bin", "late-nul.txt"):
        with pytest.raises(ReadRefused, match="binary"):
            server.read_file(name, limit=100)

    result = server.search("needle", ".")

    assert result["matches"] == [
        {"path": "clean.txt", "line": 1, "text": "needle clean"}
    ]
    assert result["truncated"] is True
    assert "binary files skipped: 2" in result["reason"]


def test_read_server_offset_cannot_skip_binary_prefix(tmp_path):
    server, project, _outside = _server(tmp_path)
    (project / "binary-prefix.txt").write_bytes(b"hidden\x00data\nvisible tail\n")

    with pytest.raises(ReadRefused, match="binary"):
        server.read_file("binary-prefix.txt", offset=1, limit=1)

    result = server.search("visible", "binary-prefix.txt")
    assert result["matches"] == []
    assert result["reason"] == "binary files skipped: 1"


def test_read_server_late_nul_within_read_cap_matches_search(tmp_path):
    server, project, _outside = _server(tmp_path)
    (project / "late-binary.txt").write_bytes(
        b"visible first page\n" + b"text\n" * 20 + b"late\x00nul\n"
    )

    with pytest.raises(ReadRefused, match="binary"):
        server.read_file("late-binary.txt", limit=1)

    result = server.search("visible", "late-binary.txt")
    assert result["matches"] == []
    assert result["reason"] == "binary files skipped: 1"


def test_read_server_stdio_refuses_missing_project_dir(tmp_path):
    project = tmp_path / "project"
    project.mkdir()
    policy = _policy(tmp_path / "policy.json", project)
    runner = (
        Path(__file__).parents[2]
        / "src"
        / "compchem_memory"
        / "hook"
        / "runner.py"
    )
    result = subprocess.run(
        [os.sys.executable, "-I", str(runner), "--read-server"],
        cwd=project,
        env={
            "HOME": str(tmp_path / "home"),
            "MAGNOLIA_HOOK_POLICY": str(policy),
            "MAGNOLIA_HOOK_POLICY_SHA256": hashlib.sha256(
                policy.read_bytes()
            ).hexdigest(),
            "MAGNOLIA_CONTROL_DIR": str(tmp_path / "control"),
            "PYTHONPYCACHEPREFIX": str(tmp_path / "pycache"),
        },
        capture_output=True,
        text=True,
        timeout=10,
    )

    assert result.returncode != 0
    assert "explicit canonical cwd is required" in result.stderr


def test_read_server_proves_file_count_deadline_and_read_byte_caps(
    tmp_path, monkeypatch
):
    class StepClock:
        def __init__(self):
            self.value = 0.0

        def __call__(self):
            current = self.value
            self.value += 1.0
            return current

    server, project, _outside = _server(tmp_path)
    for index in range(4):
        (project / f"file-{index}.txt").write_text("needle\n", encoding="utf-8")
    monkeypatch.setattr(server, "MAX_SEARCH_FILES", 2)
    capped = server.search("needle", ".")
    assert "file limit exceeded" in capped["reason"]

    clocked, clocked_project, _outside = _server(
        tmp_path / "clocked", clock=StepClock()
    )
    (clocked_project / "one.txt").write_text("needle\n", encoding="utf-8")
    (clocked_project / "two.txt").write_text("needle\n", encoding="utf-8")
    deadline = clocked.search("needle", ".")
    assert "time limit exceeded" in deadline["reason"]

    monkeypatch.setattr(server, "MAX_READ_BYTES", 32)
    (project / "large.txt").write_text("x" * 100 + "\n", encoding="utf-8")
    large = server.read_file("large.txt", limit=1)
    assert large["bytes"] <= 32
    assert len(large["content"].encode("utf-8")) <= 32
    assert large["truncated"] is True


@pytest.mark.parametrize("role", ("helper", "evaluator"))
def test_read_server_refuses_non_campaign_read_roles(tmp_path, role):
    project = tmp_path / role
    project.mkdir()
    policy = _policy(tmp_path / f"{role}.json", project, role=role)

    with pytest.raises(ReadRefused, match="worker and reviewer"):
        ReadServer.from_policy_file(
            policy, cwd=project, control_dir=str(tmp_path / "control")
        )


def test_dedicated_worker_and_reviewer_servers_cannot_cross_roots(tmp_path):
    worker_root = tmp_path / "worker"
    reviewer_root = tmp_path / "reviewer"
    worker_root.mkdir()
    reviewer_root.mkdir()
    (worker_root / "worker.txt").write_text("worker\n", encoding="utf-8")
    (reviewer_root / "reviewer.txt").write_text("reviewer\n", encoding="utf-8")
    worker = ReadServer.from_policy_file(
        _policy(tmp_path / "worker.json", worker_root),
        cwd=worker_root,
        control_dir=str(tmp_path / "control"),
    )
    reviewer = ReadServer.from_policy_file(
        _policy(tmp_path / "reviewer.json", reviewer_root, role="reviewer"),
        cwd=reviewer_root,
        control_dir=str(tmp_path / "control"),
    )

    assert worker.read_file(str(worker_root / "worker.txt"))["content"] == "worker\n"
    assert reviewer.read_file(str(reviewer_root / "reviewer.txt"))["content"] == "reviewer\n"
    with pytest.raises(ReadRefused, match="policy denied"):
        worker.read_file(str(reviewer_root / "reviewer.txt"))
    with pytest.raises(ReadRefused, match="policy denied"):
        reviewer.read_file(str(worker_root / "worker.txt"))


def test_read_server_rechecks_open_descriptor_and_refuses_fifo(tmp_path, monkeypatch):
    server, project, outside = _server(tmp_path)
    race = project / "race.txt"
    race.write_text("benign\n", encoding="utf-8")
    loot = outside / "loot.txt"
    loot.write_text("LOOT\n", encoding="utf-8")
    original = __import__(
        "compchem_memory.harness.read_server", fromlist=["read_target_allowed"]
    ).read_target_allowed
    swapped = False

    def swap_after_check(path, cwd, policy, broad=False):
        nonlocal swapped
        allowed = original(path, cwd, policy, broad=broad)
        if allowed and path == "race.txt" and not swapped:
            swapped = True
            race.unlink()
            race.symlink_to(loot)
        return allowed

    monkeypatch.setattr(
        "compchem_memory.harness.read_server.read_target_allowed", swap_after_check
    )
    with pytest.raises(ReadRefused):
        server.read_file("race.txt")

    fifo = project / "pipe"
    os.mkfifo(fifo)
    started = time.monotonic()
    with pytest.raises(ReadRefused, match="regular file"):
        server.read_file("pipe")
    assert time.monotonic() - started < 1
    with pytest.raises(ReadRefused, match="regular file"):
        server.search("needle", "pipe")


def test_read_server_search_caps_line_and_total_bytes(tmp_path, monkeypatch):
    server, project, _outside = _server(tmp_path)
    monkeypatch.setattr(server, "MAX_SEARCH_LINE_BYTES", 32)
    monkeypatch.setattr(server, "MAX_SEARCH_TOTAL_BYTES", 96)
    (project / "long.txt").write_text("a" * 200 + " needle\n", encoding="utf-8")

    result = server.search("needle", "long.txt")

    assert result["truncated"] is True
    assert "line" in result["reason"] or "byte" in result["reason"]


@pytest.mark.asyncio
async def test_pathological_search_does_not_block_parallel_stdio_read(tmp_path):
    _server_instance, project, _outside = _server(tmp_path)
    policy = _policy(tmp_path / "stdio-policy.json", project, role="reviewer")
    (project / "notes.txt").write_text("hello\n", encoding="utf-8")
    (project / "long.txt").write_text("a" * 8_192 + "\n", encoding="utf-8")
    transport = StdioTransport(
        command=os.environ.get("PYTHON", os.sys.executable),
        args=["-P", "-m", "compchem_memory.harness.read_server"],
        env={
            **os.environ,
            "MAGNOLIA_HOOK_POLICY": str(policy),
                "MAGNOLIA_HOOK_POLICY_SHA256": hashlib.sha256(
                    policy.read_bytes()
                ).hexdigest(),
                "MAGNOLIA_CONTROL_DIR": str(tmp_path / "control"),
                "MAGNOLIA_PROJECT_DIR": str(project.resolve()),
            },
        cwd=str(project),
    )
    async with Client(transport) as client:
        slow = asyncio.create_task(client.call_tool(
            "search",
            {"pattern": "re:a*a*a*a*a*b", "path": "long.txt"},
            raise_on_error=False,
        ))
        await asyncio.sleep(0.1)
        read = await asyncio.wait_for(client.call_tool(
            "read_file", {"path": "notes.txt"}, raise_on_error=False
        ), timeout=1.0)
        assert read.is_error is False
        result = await asyncio.wait_for(slow, timeout=5.0)
        assert "truncated" in result.content[0].text


@pytest.mark.asyncio
async def test_isolated_read_server_ignores_project_shadow_modules(tmp_path):
    repository = tmp_path / "copied-repository" / "opencode_cc_mem"
    source = repository / "mcp-servers" / "compchem-memory" / "src"
    package_source = Path(__file__).parents[2] / "src" / "compchem_memory"
    shutil.copytree(package_source, source / "compchem_memory")
    foreign_source = tmp_path / "foreign-checkout" / "src"
    shutil.copytree(package_source, foreign_source / "compchem_memory")
    foreign_trap = tmp_path / "foreign-imported"
    foreign_read_server = (
        foreign_source / "compchem_memory" / "harness" / "read_server.py"
    )
    foreign_body = foreign_read_server.read_text(encoding="utf-8")
    future = "from __future__ import annotations\n"
    foreign_read_server.write_text(
        foreign_body.replace(
            future,
            future
            + "from pathlib import Path\n"
            + f"Path({str(foreign_trap)!r}).write_text('foreign')\n",
            1,
        ),
        encoding="utf-8",
    )
    isolated_venv = tmp_path / "venv"
    venv.EnvBuilder(with_pip=False).create(isolated_venv)
    isolated_python = isolated_venv / "bin" / "python"
    purelib = subprocess.run(
        [str(isolated_python), "-I", "-c", "import site; print(site.getsitepackages()[0])"],
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()
    Path(purelib, "foreign-checkout.pth").write_text(
        f"{foreign_source}\n{site.getsitepackages()[0]}\n",
        encoding="utf-8",
    )
    project = tmp_path / "project"
    project.mkdir()
    trap = tmp_path / "shadow-imported"
    package = project / "compchem_memory"
    package.mkdir()
    (package / "__init__.py").write_text(
        f"from pathlib import Path\nPath({str(trap)!r}).write_text('package')\n",
        encoding="utf-8",
    )
    for name in ("json.py", "signal.py", "sitecustomize.py"):
        (project / name).write_text(
            f"from pathlib import Path\nPath({str(trap)!r}).write_text({name!r})\n",
            encoding="utf-8",
        )
    (project / "shadow.pth").write_text(
        f"import pathlib; pathlib.Path({str(trap)!r}).write_text('pth')\n",
        encoding="utf-8",
    )
    (project / "notes.txt").write_text("alpha\nneedle-123\nomega\n", encoding="utf-8")
    policy = _policy(tmp_path / "policy.json", project, role="reviewer")
    runner = source / "compchem_memory" / "hook" / "runner.py"
    transport = StdioTransport(
        command=str(isolated_python),
        args=["-I", str(runner), "--read-server"],
        env={
            "HOME": str(tmp_path),
            "PATH": os.environ.get("PATH", ""),
            "MAGNOLIA_HOOK_POLICY": str(policy),
            "MAGNOLIA_HOOK_POLICY_SHA256": hashlib.sha256(policy.read_bytes()).hexdigest(),
            "MAGNOLIA_CONTROL_DIR": str(tmp_path / "control"),
            "MAGNOLIA_PROJECT_DIR": str(project.resolve()),
        },
        cwd=str(project),
    )

    async with Client(transport) as client:
        tools = await client.list_tools()
        result = await client.call_tool(
            "search",
            {"pattern": "re:needle-[0-9]+", "path": "notes.txt"},
            raise_on_error=False,
        )

    assert {tool.name for tool in tools} == {"read_file", "list_dir", "search"}
    assert result.is_error is False
    assert "needle-123" in result.content[0].text
    assert not trap.exists()
    assert not foreign_trap.exists()


@pytest.mark.asyncio
async def test_regex_worker_uses_run_owned_bytecode_cache(tmp_path):
    repository = tmp_path / "writable-repository" / "opencode_cc_mem"
    source = repository / "mcp-servers" / "compchem-memory" / "src"
    shutil.copytree(
        Path(__file__).parents[2] / "src" / "compchem_memory",
        source / "compchem_memory",
        ignore=shutil.ignore_patterns("__pycache__", "*.pyc"),
    )
    project = tmp_path / "project"
    project.mkdir()
    (project / "notes.txt").write_text(
        "alpha\nneedle-123\nomega\n", encoding="utf-8"
    )
    policy = _policy(tmp_path / "policy.json", project)
    control = tmp_path / "run" / "control"
    cache = control / "executors" / "fixture" / "pycache"
    runner = source / "compchem_memory" / "hook" / "runner.py"
    environment = {
        **os.environ,
        "PYTHONPYCACHEPREFIX": str(cache),
        "MAGNOLIA_HOOK_POLICY": str(policy),
        "MAGNOLIA_HOOK_POLICY_SHA256": hashlib.sha256(policy.read_bytes()).hexdigest(),
        "MAGNOLIA_CONTROL_DIR": str(control),
        "MAGNOLIA_PROJECT_DIR": str(project.resolve()),
    }

    async def search() -> dict:
        transport = StdioTransport(
            command=os.sys.executable,
            args=["-I", str(runner), "--read-server"],
            env=environment,
            cwd=str(project),
        )
        async with Client(transport) as client:
            result = await client.call_tool(
                "search",
                {"pattern": "re:needle-[0-9]+", "path": "notes.txt"},
                raise_on_error=False,
            )
        assert result.is_error is False
        return json.loads(result.content[0].text)

    first_result = await search()
    repository_cache_files = list(repository.rglob("*.pyc"))

    read_server = source / "compchem_memory" / "harness" / "read_server.py"
    body = read_server.read_text(encoding="utf-8")
    marker = '    """Evaluate bounded regex input outside the stdio server process."""\n'
    trap = tmp_path / "planted-bytecode-executed"
    forged = marker + (
        f"    open({str(trap)!r}, 'a').write('executed\\n')\n"
        "    connection.send(([0, 1, 2], os.path.realpath(__file__)))\n"
        "    connection.close()\n"
        "    return\n"
    )
    code = compile(body.replace(marker, forged, 1), str(read_server), "exec")
    stat = read_server.stat()
    from importlib._bootstrap_external import _code_to_timestamp_pyc

    planted_cache = Path(importlib.util.cache_from_source(str(read_server)))
    planted_cache.parent.mkdir(parents=True, exist_ok=True)
    planted_cache.write_bytes(
        _code_to_timestamp_pyc(code, stat.st_mtime, stat.st_size)
    )

    second_result = await search()

    expected_matches = [{"path": "notes.txt", "line": 2, "text": "needle-123"}]
    assert first_result["matches"] == expected_matches
    assert repository_cache_files == []
    assert list(cache.rglob("*.pyc"))
    assert second_result["matches"] == expected_matches
    assert not trap.exists()


@pytest.mark.asyncio
async def test_codex_stdio_servers_keep_bytecode_out_of_read_only_source(tmp_path):
    repository = tmp_path / "readonly-repository" / "opencode_cc_mem"
    source = repository / "mcp-servers" / "compchem-memory" / "src"
    shutil.copytree(
        Path(__file__).parents[2] / "src" / "compchem_memory",
        source / "compchem_memory",
        ignore=shutil.ignore_patterns("__pycache__", "*.pyc"),
    )
    project = tmp_path / "project"
    project.mkdir()
    policy_path = _policy(tmp_path / "policy.json", project)
    control = tmp_path / "run" / "control"
    control.mkdir(parents=True)
    cache = control / "executors" / "fixture" / "pycache"
    runner = source / "compchem_memory" / "hook" / "runner.py"
    common_env = {
        **os.environ,
        "PYTHONPYCACHEPREFIX": str(cache),
        "MAGNOLIA_PROJECT_DIR": str(project),
    }
    transports = (
        StdioTransport(
            command=os.sys.executable,
            args=["-I", str(runner), "--memory-server"],
            env=common_env,
            cwd=str(project),
        ),
        StdioTransport(
            command=os.sys.executable,
            args=["-I", str(runner), "--read-server"],
            env={
                **common_env,
                "MAGNOLIA_HOOK_POLICY": str(policy_path),
                "MAGNOLIA_HOOK_POLICY_SHA256": hashlib.sha256(
                    policy_path.read_bytes()
                ).hexdigest(),
                "MAGNOLIA_CONTROL_DIR": str(control),
            },
            cwd=str(project),
        ),
    )
    paths = sorted(repository.rglob("*"), reverse=True)
    try:
        for path in paths:
            path.chmod(0o555 if path.is_dir() else 0o444)
        repository.chmod(0o555)
        for transport in transports:
            async with Client(transport) as client:
                assert await client.list_tools()
    finally:
        repository.chmod(0o755)
        for path in reversed(paths):
            path.chmod(0o755 if path.is_dir() else 0o644)

    assert not list(repository.rglob("*.pyc"))
    assert not list(repository.rglob("__pycache__"))
    assert list(cache.rglob("*.pyc"))
