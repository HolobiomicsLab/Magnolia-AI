"""Required campaign-role policy checks across neutral tool targets."""

from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import shutil
import subprocess

import pytest

from compchem_memory.hook.adapters import claude, codex
from compchem_memory.hook.events import HookEvent, ToolCall, ToolTarget
from compchem_memory.hook import policy
from compchem_memory.hook.policy import decide


def _write_policy(
    path: Path,
    project: Path,
    work: Path,
    *,
    edit_roots: list[str] | None = None,
    role: str = "worker",
) -> dict[str, str]:
    if project.resolve().is_relative_to(path.parent.resolve()):
        path = path.parent / "policies" / path.name
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "schema_version": "magnolia-role-v1",
        "role": role,
        "policy_role": f"campaign-{role}",
        "canonical_read_roots": [str(project)],
        "read_deny_patterns": ["**/*.env*", "**/*.env*/**", "**/secret/**"],
        "deny_path_forms": ["requested", "resolved"],
        "edit_roots": [str(work)] if edit_roots is None else edit_roots,
        "effective_capabilities": [
            "native_read", "native_glob", "native_grep", "native_edit",
        ],
        "mcp_tool_allowlist": [
            "mcp:compchem-memory.memory_get_context",
            "mcp:compchem-memory.memory_search",
            "mcp:compchem-memory.memory_get_run_history",
            "mcp:magnolia-read.read_file",
            "mcp:magnolia-read.list_dir",
            "mcp:magnolia-read.search",
        ],
        "delegation_policy": "deny",
        "required_capability_identity": "fixture-identity",
        "deny_unknown_tools": True,
        "deny_shell": True,
        "deny_unconfined_code_execution": True,
        "deny_tools": ["bash", "shell", "delegate", "code"],
    }
    path.write_text(json.dumps(payload), encoding="utf-8")
    return {
        "MAGNOLIA_HOOK_POLICY": str(path),
        "MAGNOLIA_HOOK_POLICY_REQUIRED": "1",
        "MAGNOLIA_HOOK_POLICY_SHA256": hashlib.sha256(path.read_bytes()).hexdigest(),
        "MAGNOLIA_CONTROL_DIR": str(path.parent / "control"),
    }


def _event(
    cwd: Path,
    action: str | None,
    *targets: ToolTarget,
    raw_name: str = "Read",
) -> HookEvent:
    return HookEvent(
        "claude",
        "pre-tool",
        "session",
        str(cwd),
        tool=ToolCall(
            raw_name.lower(), None, raw_name, "call", {},
            policy_action=action, targets=tuple(targets),
        ),
    )


def test_role_policy_all_targets(tmp_path):
    project = tmp_path / "project"
    work = project / "run" / "work"
    outside = tmp_path / "outside"
    secret = project / "secret"
    work.mkdir(parents=True)
    clean = project / "clean"
    clean.mkdir()
    outside.mkdir()
    secret.mkdir()
    nested_secret = project / "conf.env.d" / "sub"
    nested_secret.mkdir(parents=True)
    (project / "allowed.txt").write_text("ok", encoding="utf-8")
    (project / ".env").write_text("secret", encoding="utf-8")
    (outside / "outside.txt").write_text("no", encoding="utf-8")
    (secret / "token.txt").write_text("no", encoding="utf-8")
    (nested_secret / "key").write_text("no", encoding="utf-8")
    (project / "alias").symlink_to(secret, target_is_directory=True)
    outside_parent = outside / "inner"
    outside_parent.mkdir()
    (outside / "loot.txt").write_text("no", encoding="utf-8")
    (project / "link-out").symlink_to(outside_parent, target_is_directory=True)
    env = _write_policy(tmp_path / "policy.json", project, work)

    allowed = (
        _event(project, "native_read", ToolTarget("allowed.txt", "read")),
        _event(project, "native_edit", ToolTarget(str(work / "new.txt"), "edit")),
        _event(project, "native_edit", ToolTarget(str(work / "missing" / "new.txt"), "edit")),
        _event(
            project,
            "native_grep",
            ToolTarget(str(clean), "search", broad=True),
            raw_name="Grep",
        ),
    )
    assert all(decide(event, env) is None for event in allowed)

    denied = (
        _event(project, "native_read", ToolTarget(".env", "read")),
        _event(project, "native_read", ToolTarget(".ENV", "read")),
        _event(project, "native_read", ToolTarget("conf.env.d/sub/key", "read")),
        _event(project, "native_read", ToolTarget("~/.ssh/id_rsa", "read")),
        _event(project, "native_read", ToolTarget("alias/token.txt", "read")),
        _event(project, "native_read", ToolTarget("link-out/../loot.txt", "read")),
        _event(work, "native_read", ToolTarget("../../../outside/outside.txt", "read")),
        _event(project, "native_read", ToolTarget(str(outside / "new" / "file"), "read")),
        _event(
            project,
            "native_edit",
            ToolTarget(str(work / "a.txt"), "edit"),
            ToolTarget(str(outside / "b.txt"), "edit"),
            raw_name="Patch",
        ),
        _event(
            project,
            "native_edit",
            ToolTarget(str(work / "source.txt"), "edit"),
            ToolTarget(str(outside / "destination.txt"), "edit"),
            raw_name="Move",
        ),
        _event(
            project,
            "native_glob",
            ToolTarget(str(project), "search", broad=True),
            raw_name="Glob",
        ),
        _event(project, "native_read", raw_name="Read"),
        _event(project, None, raw_name="UnknownTool"),
    )
    assert all(decide(event, env) is not None for event in denied)

    reviewer_env = _write_policy(
        tmp_path / "reviewer.json", project, work, edit_roots=[]
    )
    assert decide(
        _event(project, "native_edit", ToolTarget(str(work / "review.txt"), "edit")),
        reviewer_env,
    ) is not None

    omitted_root = claude.parse(
        {
            "session_id": "session",
            "cwd": str(clean),
            "hook_event_name": "PreToolUse",
            "tool_name": "Glob",
            "tool_input": {"pattern": "**/*.py"},
        },
        "pre-tool",
    )
    assert omitted_root is not None
    assert omitted_root.tool is not None
    assert omitted_root.tool.targets[0].path == str(clean)
    assert decide(omitted_root, env) is None

    narrowed_search = claude.parse(
        {
            "session_id": "session",
            "cwd": str(project),
            "hook_event_name": "PreToolUse",
            "tool_name": "Glob",
            "tool_input": {"pattern": "**/*.py"},
        },
        "pre-tool",
    )
    assert narrowed_search is not None
    assert decide(narrowed_search, env) is not None

    unsafe_searches = (
        {"tool_name": "Grep", "tool_input": {"glob": "{.env,*.py}"}},
        {"tool_name": "Grep", "tool_input": {"glob": "**/sub/**"}},
        {"tool_name": "Glob", "tool_input": {"pattern": "../outside/**"}},
        {"tool_name": "Glob", "tool_input": {"pattern": str(outside / "*")}},
        {"tool_name": "Grep", "tool_input": {"path": str(project), "glob": "*.txt"}},
    )
    for search in unsafe_searches:
        parsed = claude.parse(
            {
                "session_id": "session",
                "cwd": str(project),
                "hook_event_name": "PreToolUse",
                **search,
            },
            "pre-tool",
        )
        assert parsed is not None
        assert decide(parsed, env) is not None

    ambiguous_searches = (
        "{..,x}/outside/*",
        ".{.,}/outside/*",
        r"\.\./outside/*",
        "[.][.]/outside/*",
        "{/etc,x}/*",
        f"{{{outside},x}}/*",
    )
    for pattern in ambiguous_searches:
        parsed = claude.parse(
            {
                "session_id": "session",
                "cwd": str(project),
                "hook_event_name": "PreToolUse",
                "tool_name": "Glob",
                "tool_input": {"path": str(clean), "pattern": pattern},
            },
            "pre-tool",
        )
        assert parsed is not None
        assert decide(parsed, env) is not None

    assert decide(
        _event(project, "native_read", ToolTarget("$HOME/.ssh/id_rsa", "read")),
        env,
    ) is not None
    dollar_search = claude.parse(
        {
            "session_id": "session",
            "cwd": str(project),
            "hook_event_name": "PreToolUse",
            "tool_name": "Glob",
            "tool_input": {"path": str(clean), "pattern": "$HOME/**/*"},
        },
        "pre-tool",
    )
    assert dollar_search is not None
    assert decide(dollar_search, env) is not None


def test_broad_search_scan_bound_denies(tmp_path, monkeypatch):
    project = tmp_path / "project"
    work = project / "work"
    work.mkdir(parents=True)
    for index in range(3):
        (project / f"allowed-{index}.txt").write_text("ok", encoding="utf-8")
    env = _write_policy(tmp_path / "policy.json", project, work)
    monkeypatch.setattr(policy, "BROAD_SEARCH_MAX_ENTRIES", 2)

    event = _event(
        project,
        "native_grep",
        ToolTarget(str(project), "search", broad=True),
        raw_name="Grep",
    )

    assert decide(event, env) is not None


def test_campaign_mcp_shell_member_tool_delegation_and_inner_call_denied(tmp_path):
    project = tmp_path / "project"
    work = project / "run" / "work"
    work.mkdir(parents=True)
    env = _write_policy(tmp_path / "policy.json", project, work)

    allowed_memory = _event(
        project, "mcp", raw_name="mcp__compchem-memory__memory_search"
    )
    assert decide(allowed_memory, env) is None

    denied_names = (
        "Bash",
        "mcp__compchem-tools__run_shell",
        "mcp__member__read_private",
        "Agent",
        "Task",
        "spawn_agent",
        "agents.spawn",
        "run_typescript",
        "code",
        "NotebookEdit",
        "apply_patch",
        "Move",
    )
    for raw_name in denied_names:
        assert decide(_event(project, None, raw_name=raw_name), env) is not None


def test_hook_allows_read_server_tools_only_for_worker_reviewer(tmp_path):
    project = tmp_path / "project"
    work = project / "run" / "work"
    work.mkdir(parents=True)
    (project / "allowed.txt").write_text("ok\n", encoding="utf-8")

    for role in ("worker", "reviewer"):
        env = _write_policy(tmp_path / f"{role}.json", project, work, role=role)
        for raw_name, access, broad in (
            ("mcp__magnolia_read__read_file", "read", False),
            ("mcp__magnolia_read__list_dir", "read", False),
            ("mcp__magnolia_read__search", "search", True),
        ):
            event = _event(
                project,
                "mcp",
                ToolTarget(str(project / "allowed.txt"), access, broad=broad),
                raw_name=raw_name,
            )
            assert decide(event, env) is None

        parsed = codex.parse({
            "hook_event_name": "PreToolUse",
            "session_id": "session",
            "cwd": str(project),
            "tool_name": "mcp__magnolia_read__read_file",
            "tool_input": {"path": "allowed.txt"},
        }, "pre-tool")
        assert parsed is not None
        assert decide(parsed, env) is None
        parsed.tool.input["path"] = "secret/token.txt"
        parsed.tool.targets = (ToolTarget("secret/token.txt", "read"),)
        assert decide(parsed, env) is not None

    helper = _write_policy(tmp_path / "helper.json", project, work, role="helper")
    assert decide(
        _event(
            project,
            "mcp",
            ToolTarget(str(project / "allowed.txt"), "read"),
            raw_name="mcp__magnolia_read__read_file",
        ),
        helper,
    ) is not None
    assert decide(
        _event(
            project,
            "mcp",
            ToolTarget(str(project / "allowed.txt"), "read"),
            raw_name="mcp__other__read_file",
        ),
        _write_policy(tmp_path / "worker-other.json", project, work),
    ) is not None


def test_codex_apply_patch_is_confined_to_worker_edit_root(tmp_path):
    project = tmp_path / "project"
    work = project / "run" / "work"
    work.mkdir(parents=True)
    outside = project / "outside.txt"
    env = _write_policy(tmp_path / "worker.json", project, work)

    def event(patch: str):
        parsed = codex.parse({
            "hook_event_name": "PreToolUse",
            "session_id": "session",
            "cwd": str(project),
            "tool_name": "apply_patch",
            "tool_input": {"command": patch},
        }, "pre-tool")
        assert parsed is not None
        return parsed

    assert decide(event(
        f"*** Begin Patch\n*** Add File: {work / 'ok.txt'}\n+ok\n*** End Patch"
    ), env) is None
    assert decide(event(
        f"*** Begin Patch\n*** Add File: {outside}\n+no\n*** End Patch"
    ), env) is not None
    assert decide(event(
        "*** Begin Patch\n"
        f"*** Update File: {work / 'ok.txt'}\n"
        f"*** Move to: {outside}\n"
        "*** End Patch"
    ), env) is not None


def test_codex_apply_patch_decodes_indented_headers_and_fails_closed(tmp_path):
    project = tmp_path / "project"
    work = project / "run" / "work"
    work.mkdir(parents=True)
    env = _write_policy(tmp_path / "worker.json", project, work)

    def decision(command: str, *, key: str = "command"):
        event = codex.parse({
            "hook_event_name": "PreToolUse",
            "session_id": "session",
            "cwd": str(project),
            "tool_name": "apply_patch",
            "tool_input": {key: command},
        }, "pre-tool")
        assert event is not None
        return decide(event, env)

    outside = project / "control.txt"
    for whitespace in ("  ", "\t"):
        command = (
            "*** Begin Patch\n"
            f"*** Add File: {work / 'ok.txt'}\n"
            "+ok\n"
            f"{whitespace}*** Add File: {outside}\n"
            "+escaped\n"
            "*** End Patch"
        )
        assert decision(command) is not None

    malformed = "*** Begin Patch\n*** Rename File: hidden.txt\n*** End Patch"
    assert decision(malformed) is not None
    valid = f"*** Begin Patch\n*** Add File: {work / 'ok.txt'}\n+ok\n*** End Patch"
    assert decision(valid, key="patch") is not None


def test_required_policy_digest_mismatch_denies_every_tool(tmp_path):
    project = tmp_path / "project"
    work = project / "run" / "work"
    work.mkdir(parents=True)
    policy_path = tmp_path / "policy.json"
    env = _write_policy(policy_path, project, work)
    policy_path = Path(env["MAGNOLIA_HOOK_POLICY"])
    allowed = _event(project, "native_read", ToolTarget(str(project), "read"))
    assert decide(allowed, env) is None

    payload = json.loads(policy_path.read_text(encoding="utf-8"))
    payload["edit_roots"] = [str(project)]
    policy_path.write_text(json.dumps(payload), encoding="utf-8")

    assert decide(allowed, env) is not None


def test_edit_targets_never_include_control_or_policy_directory(tmp_path):
    project = tmp_path / "project"
    run = project / "runs" / "r1"
    work = run / "work"
    control = run / "control"
    policy_dir = project / "policy-store"
    work.mkdir(parents=True)
    control.mkdir(parents=True)
    policy_dir.mkdir()
    policy_path = policy_dir / "policy.json"
    env = _write_policy(policy_path, project, work, edit_roots=[str(project)])
    env["MAGNOLIA_CONTROL_DIR"] = str(control)

    for target in (control / "state.json", policy_dir / "replacement.json"):
        event = _event(
            project,
            "native_edit",
            ToolTarget(str(target), "edit"),
            raw_name="apply_patch",
        )
        assert decide(event, env) is not None

    allowed = _event(
        project,
        "native_edit",
        ToolTarget(str(project / "src.py"), "edit"),
        raw_name="apply_patch",
    )
    assert decide(allowed, env) is None


def test_protected_edit_roots_follow_case_insensitive_filesystem_semantics(tmp_path):
    project = tmp_path / "project"
    work = project / "work"
    control = project / "control"
    policy_dir = project / "policy-store"
    work.mkdir(parents=True)
    control.mkdir()
    policy_dir.mkdir()
    control_variant = control.with_name(control.name.swapcase())
    policy_variant = policy_dir.with_name(policy_dir.name.swapcase())
    try:
        is_case_insensitive = (
            os.path.samefile(control, control_variant)
            and os.path.samefile(policy_dir, policy_variant)
        )
    except OSError:
        is_case_insensitive = False
    if not is_case_insensitive:
        pytest.skip("filesystem is case-sensitive; case-variant aliases do not exist")
    policy_path = policy_dir / "policy.json"
    env = _write_policy(policy_path, project, work, edit_roots=[str(project)])
    env["MAGNOLIA_CONTROL_DIR"] = str(control)

    for target in (
        control_variant / "approvals" / "a.json",
        policy_variant / "replacement.json",
    ):
        event = _event(
            project,
            "native_edit",
            ToolTarget(str(target), "edit"),
            raw_name="apply_patch",
        )
        assert decide(event, env) is not None


@pytest.mark.parametrize("control_dir", (None, "relative/control"))
def test_required_policy_denies_missing_or_relative_control_dir(
    tmp_path, control_dir
):
    project = tmp_path / "project"
    work = project / "work"
    work.mkdir(parents=True)
    env = _write_policy(tmp_path / "policy.json", project, work)
    if control_dir is None:
        env.pop("MAGNOLIA_CONTROL_DIR")
    else:
        env["MAGNOLIA_CONTROL_DIR"] = control_dir
    event = _event(
        project,
        "native_edit",
        ToolTarget(str(work / "allowed.txt"), "edit"),
        raw_name="apply_patch",
    )

    assert decide(event, env) is not None


def test_policy_escalation_chain_stops_at_policy_rewrite(tmp_path):
    apply_patch = _offline_apply_patch_binary()
    project = tmp_path / "project"
    run = project / "runs" / "r1"
    work = run / "work"
    control = run / "control"
    policy_path = control / "executors" / "inv" / "policy.json"
    work.mkdir(parents=True)
    policy_path.parent.mkdir(parents=True)
    source = project / "src.py"
    source.write_text("orig\n", encoding="utf-8")
    env = _write_policy(policy_path, project, work)
    original_policy = policy_path.read_bytes()

    def decision(command: str):
        event = codex.parse({
            "hook_event_name": "PreToolUse",
            "session_id": "session",
            "cwd": str(project),
            "tool_name": "apply_patch",
            "tool_input": {"command": command},
        }, "pre-tool")
        assert event is not None
        return decide(event, env)

    direct = "*** Begin Patch\n*** Update File: src.py\n@@\n-orig\n+owned\n*** End Patch\n"
    malicious = (
        "*** Begin Patch\n"
        "*** Add File: runs/r1/work/x\r../../../control/executors/inv/policy.json\n"
        "+{\"edit_roots\": [\".\"]}\n"
        "*** End Patch\n"
    )
    allowed = (
        "*** Begin Patch\n"
        "*** Add File: runs/r1/work/probe.txt\n"
        "+applied\n"
        "*** End Patch\n"
    )

    assert decision(allowed) is None
    subprocess.run(
        [apply_patch, allowed],
        cwd=project,
        capture_output=True,
        text=True,
        timeout=20,
        check=True,
        env={
            "PATH": os.environ.get("PATH", ""),
            "HOME": str(tmp_path),
            "CODEX_HOME": str(tmp_path / "codex-home"),
        },
    )
    assert (work / "probe.txt").read_bytes() == b"applied\n"
    assert decision(direct) is not None
    assert decision(malicious) is not None
    policy_after = policy_path.read_bytes()
    assert policy_after == original_policy
    assert decision(direct) is not None
    assert source.read_text(encoding="utf-8") == "orig\n"


def _snapshot_files(root: Path) -> dict[str, bytes]:
    return {
        str(path.relative_to(root)): path.read_bytes()
        for path in root.rglob("*")
        if path.is_file()
    }


def test_offline_apply_patch_variants_are_denied_or_match_decoded_targets(tmp_path):
    """Differentially check every re-review separator/path exploit class."""
    apply_patch = _offline_apply_patch_binary()
    separators = ("\r", "\v", "\f", "\x1c", "\x1d", "\x1e", "\x85", "\u2028", "\u2029")
    patches = [
        f"*** Begin Patch\n*** Add File: work/x{sep}../../../keep.txt\n+pwned\n*** End Patch\n"
        for sep in separators
    ]
    patches.extend((
        (
            "*** Begin Patch\n"
            "*** Add File: work/x\r../../../control/executors/inv/evil.json\n"
            "+{}\n*** End Patch\n"
        ),
        (
            "*** Begin Patch\n*** Add File: work/x\r../seed.txt\n+s\n"
            "*** Update File: work/x\r../../../control/executors/inv/state.json\n"
            "@@\n-{}\n+{\"changed\": true}\n*** End Patch\n"
        ),
        (
            "*** Begin Patch\n*** Add File: work/x\r../seed.txt\n+s\n"
            "*** Add File: work/x\r../../../control/executors/inv/policy.json\n"
            "+{}\n*** End Patch\n"
        ),
        (
            "*** Begin Patch\n*** Add File: work/x\r../seed.txt\n+s\n"
            "*** Delete File: work/x\r../../../control/executors/inv/policy.json\n"
            "*** End Patch\n"
        ),
        (
            "*** Begin Patch\n*** Add File: work/y\r../seed.txt\n+s\n"
            "*** Update File: work/a.txt\n"
            "*** Move to: work/y\r../../../moved.txt\n"
            "@@\n-a\n+b\n*** End Patch\n"
        ),
        "*** Begin Patch\n*** Add File:  work/leading.txt\n+x\n*** End Patch\n",
        "*** Begin Patch\n*** Add File: \twork/tab.txt\n+x\n*** End Patch\n",
        "*** Begin Patch\n*** Add File: work/trailing.txt \n+x\n*** End Patch\n",
    ))
    many = "".join(
        f"*** Add File: work/f{index}.txt\n+{index}\n" for index in range(300)
    )
    patches.append(
        f"*** Begin Patch\n{many}*** Add File: work/z\r../../../keep.txt\n+pwned\n*** End Patch\n"
    )
    escaped_writes = 0

    for index, patch in enumerate(patches):
        case = tmp_path / f"case-{index}"
        project = case / "project"
        work = project / "work"
        policy_path = project / "control" / "executors" / "inv" / "policy.json"
        work.mkdir(parents=True)
        policy_path.parent.mkdir(parents=True)
        (work / "a.txt").write_text("a\n", encoding="utf-8")
        (policy_path.parent / "state.json").write_text("{}\n", encoding="utf-8")
        (project / "keep.txt").write_text("keep\n", encoding="utf-8")
        env = _write_policy(policy_path, project, work)
        event = codex.parse({
            "hook_event_name": "PreToolUse",
            "session_id": "session",
            "cwd": str(project),
            "tool_name": "apply_patch",
            "tool_input": {"command": patch},
        }, "pre-tool")
        assert event is not None and event.tool is not None
        blocked = decide(event, env) is not None
        before = _snapshot_files(project)
        subprocess.run(
            [apply_patch, patch],
            cwd=project,
            capture_output=True,
            text=True,
            timeout=20,
            check=False,
            env={
                "PATH": os.environ.get("PATH", ""),
                "HOME": str(case),
                "CODEX_HOME": str(case / "codex-home"),
            },
        )
        after = _snapshot_files(project)
        changed = {
            name
            for name in before.keys() | after.keys()
            if before.get(name) != after.get(name)
        }
        decoded = {
            str((project / target.path).resolve().relative_to(project))
            for target in event.tool.targets
            if not Path(target.path).is_absolute()
        }
        escaped_writes += sum(not name.startswith("work/") for name in changed)
        assert blocked or changed <= decoded

    assert escaped_writes > 0


def _offline_apply_patch_binary() -> str:
    configured = os.environ.get("MAGNOLIA_APPLY_PATCH_BINARY")
    if not configured:
        pytest.skip(
            "offline differential unavailable: set MAGNOLIA_APPLY_PATCH_BINARY "
            "to an absolute Codex apply_patch binary"
        )
    path = Path(configured)
    if not path.is_absolute() or not path.is_file() or not os.access(path, os.X_OK):
        pytest.fail(
            "MAGNOLIA_APPLY_PATCH_BINARY must name an absolute executable file"
        )
    return str(path)
