"""Required campaign-role policy checks across neutral tool targets."""

from __future__ import annotations

import json
from pathlib import Path

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
