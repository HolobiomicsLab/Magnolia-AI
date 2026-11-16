"""Required-mode native hook failures use each client's denial channel."""

from __future__ import annotations

import io
import json
import os
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

from compchem_memory.hook.adapters import ADAPTERS, Rendered
from compchem_memory.hook.adapters import claude, codex, vibe


FIXTURES = Path(__file__).parents[4] / "tests" / "fixtures" / "executors"


def _policy(path: Path, project: Path) -> Path:
    payload = {
        "schema_version": "magnolia-role-v1",
        "role": "reviewer",
        "policy_role": "campaign-reviewer",
        "canonical_read_roots": [str(project)],
        "read_deny_patterns": ["**/*.env*", "**/*.env*/**"],
        "deny_path_forms": ["requested", "resolved"],
        "edit_roots": [],
        "effective_capabilities": ["native_read", "native_glob", "native_grep"],
        "mcp_tool_allowlist": [],
        "delegation_policy": "deny",
        "required_capability_identity": "fixture-identity",
        "deny_unknown_tools": True,
        "deny_shell": True,
        "deny_unconfined_code_execution": True,
        "deny_tools": ["bash", "shell", "delegate", "code"],
    }
    path.write_text(json.dumps(payload), encoding="utf-8")
    return path


def _run(harness: str, payload: str, tmp_path: Path, policy: Path | None):
    env = dict(os.environ)
    env.update(
        HOME=str(tmp_path / "home"),
        XDG_STATE_HOME=str(tmp_path / "state"),
        MAGNOLIA_HOOK_POLICY_REQUIRED="1",
    )
    if policy is not None:
        env["MAGNOLIA_HOOK_POLICY"] = str(policy)
    else:
        env.pop("MAGNOLIA_HOOK_POLICY", None)
    return subprocess.run(
        [sys.executable, "-m", "compchem_memory.hook", harness, "pre-tool"],
        input=payload,
        capture_output=True,
        text=True,
        timeout=30,
        env=env,
    )


def _assert_native_denial(harness: str, result: subprocess.CompletedProcess[str]) -> None:
    payload = json.loads(result.stdout)
    if harness == "vibe":
        assert payload["decision"] == "deny"
    else:
        assert payload["hookSpecificOutput"]["permissionDecision"] == "deny"
    assert result.returncode == 0


@pytest.mark.parametrize("stdin", ("not json", "[]", "{}"))
@pytest.mark.parametrize("harness", ("claude", "codex", "vibe"))
def test_required_native_denial_on_malformed_json_or_input(
    tmp_path, harness, stdin
):
    result = _run(harness, stdin, tmp_path, tmp_path / "missing.json")
    _assert_native_denial(harness, result)


@pytest.mark.parametrize("policy_kind", ("missing", "invalid"))
@pytest.mark.parametrize("harness", ("claude", "codex", "vibe"))
def test_required_missing_invalid_policy_denies_reads_too(
    tmp_path, harness, policy_kind
):
    project = tmp_path / "project"
    project.mkdir()
    fixture = json.loads(
        (FIXTURES / harness / "synthetic" / "native-tools.json").read_text()
    )
    payload = fixture["read"]
    payload["cwd"] = str(project)
    payload["tool_input"][fixture["path_field"]] = str(project / "allowed.txt")
    policy = tmp_path / "policy.json"
    if policy_kind == "invalid":
        policy.write_text("{}", encoding="utf-8")
    result = _run(harness, json.dumps(payload), tmp_path, policy)
    _assert_native_denial(harness, result)


@pytest.mark.parametrize("harness", ("claude", "codex", "vibe"))
def test_required_unknown_tool_or_harness_refused(tmp_path, harness):
    project = tmp_path / "project"
    project.mkdir()
    policy = _policy(tmp_path / "policy.json", project)
    payload = {
        "session_id": "fixture",
        "cwd": str(project),
        "hook_event_name": "pre_tool" if harness == "vibe" else "PreToolUse",
        "tool_name": "vendor.future_tool",
        "tool_input": {},
    }
    result = _run(harness, json.dumps(payload), tmp_path, policy)
    _assert_native_denial(harness, result)

    unknown = _run("future-client", json.dumps(payload), tmp_path, policy)
    assert unknown.returncode != 0


@pytest.mark.parametrize("harness", ("claude", "codex", "vibe"))
def test_required_read_outside_roots_denied(tmp_path, harness):
    project = tmp_path / "project"
    outside = tmp_path / "outside"
    project.mkdir()
    outside.mkdir()
    side_effect = outside / "tool-ran.txt"
    policy = _policy(tmp_path / "policy.json", project)
    fixture = json.loads(
        (FIXTURES / harness / "synthetic" / "native-tools.json").read_text()
    )
    payload = fixture["read"]
    payload["cwd"] = str(project)
    payload["tool_input"][fixture["path_field"]] = str(side_effect)

    result = _run(harness, json.dumps(payload), tmp_path, policy)

    _assert_native_denial(harness, result)
    assert not side_effect.exists()


@pytest.mark.parametrize(
    ("harness", "adapter", "actions"),
    (
        (
            "claude",
            claude,
            {
                "read": "native_read",
                "glob": "native_glob",
                "grep": "native_grep",
                "edit": "native_edit",
                "write": "native_edit",
            },
        ),
        ("codex", codex, {"read": "native_read"}),
        (
            "vibe",
            vibe,
            {"read": "native_read", "write": "native_edit", "edit": "native_edit"},
        ),
    ),
)
def test_synthetic_native_target_shapes_decode_only_cited_fields(
    harness, adapter, actions
):
    fixture = json.loads(
        (FIXTURES / harness / "synthetic" / "native-tools.json").read_text()
    )

    for name, action in actions.items():
        payload = fixture[name]
        event = adapter.parse(payload, "pre-tool")
        assert event is not None and event.tool is not None
        assert event.tool.policy_action == action
        assert event.tool.targets

    unknown = dict(fixture["read"])
    unknown["tool_name"] = "NotebookEdit"
    event = adapter.parse(unknown, "pre-tool")
    assert event is not None and event.tool is not None
    assert event.tool.policy_action is None
    assert event.tool.targets == ()


def _invoke_main(monkeypatch, harness: str, adapter: object) -> int:
    from compchem_memory.hook import __main__ as cli

    monkeypatch.setenv("MAGNOLIA_HOOK_POLICY_REQUIRED", "1")
    monkeypatch.setenv("MAGNOLIA_HOOK_POLICY", "/missing-policy.json")
    monkeypatch.setitem(ADAPTERS, harness, adapter)
    monkeypatch.setattr(sys, "argv", ["hook", harness, "pre-tool"])
    monkeypatch.setattr(sys, "stdin", io.StringIO("not json"))
    monkeypatch.setattr(os, "_exit", lambda code: (_ for _ in ()).throw(SystemExit(code)))
    with pytest.raises(SystemExit) as stopped:
        cli.main()
    return stopped.value.code


def test_required_adapter_policy_render_stdout_failure_blocks(tmp_path, monkeypatch):
    broken_render = SimpleNamespace(
        parse=lambda raw, event: None,
        render=lambda response, event: (_ for _ in ()).throw(RuntimeError("render")),
    )
    assert _invoke_main(monkeypatch, "broken-render", broken_render) != 0

    from compchem_memory.hook import __main__ as cli

    empty_render = SimpleNamespace(
        parse=lambda raw, event: None,
        render=lambda response, event: Rendered("", "", 0, frozenset()),
    )
    assert _invoke_main(monkeypatch, "empty-render", empty_render) != 0

    monkeypatch.setattr(
        cli, "_write_rendered", lambda *args: (_ for _ in ()).throw(OSError("pipe"))
    )
    native = SimpleNamespace(
        parse=lambda raw, event: None,
        render=lambda response, event: Rendered(
            '{"decision":"deny"}\n', "", 0, frozenset()
        ),
    )
    assert _invoke_main(monkeypatch, "write-failure", native) != 0


def test_optional_interactive_shell_policy_unchanged(tmp_path):
    env = dict(os.environ)
    env.update(HOME=str(tmp_path / "home"), XDG_STATE_HOME=str(tmp_path / "state"))
    env.pop("MAGNOLIA_HOOK_POLICY_REQUIRED", None)
    policy = tmp_path / "policy.json"
    policy.write_text('{"deny_tools":["bash"]}', encoding="utf-8")
    env["MAGNOLIA_HOOK_POLICY"] = str(policy)

    malformed = subprocess.run(
        [sys.executable, "-m", "compchem_memory.hook", "vibe", "pre-tool"],
        input="not json", capture_output=True, text=True, timeout=30, env=env,
    )
    assert malformed.returncode == 0 and malformed.stdout == ""

    shell = {
        "session_id": "fixture", "cwd": str(tmp_path),
        "hook_event_name": "pre_tool", "tool_name": "file_system.bash",
        "tool_input": {"command": "pwd"},
    }
    denied = subprocess.run(
        [sys.executable, "-m", "compchem_memory.hook", "vibe", "pre-tool"],
        input=json.dumps(shell), capture_output=True, text=True, timeout=30, env=env,
    )
    assert json.loads(denied.stdout)["decision"] == "deny"


@pytest.mark.parametrize("event_name", ("turn-end", "agent-end", "session-end"))
@pytest.mark.parametrize("harness", ("claude", "codex", "vibe"))
def test_required_non_tool_parse_failure_is_silent(
    tmp_path, harness, event_name
):
    env = dict(os.environ)
    env.update(
        HOME=str(tmp_path / "home"),
        XDG_STATE_HOME=str(tmp_path / "state"),
        MAGNOLIA_HOOK_POLICY_REQUIRED="1",
    )
    result = subprocess.run(
        [sys.executable, "-m", "compchem_memory.hook", harness, event_name],
        input="not json", capture_output=True, text=True, timeout=30, env=env,
    )
    assert result.returncode == 0
    assert result.stdout == ""


def test_required_toolsearch_is_silent(tmp_path):
    project = tmp_path / "project"
    project.mkdir()
    policy_path = _policy(tmp_path / "policy.json", project)
    payload = {
        "session_id": "fixture",
        "cwd": str(project),
        "hook_event_name": "PreToolUse",
        "tool_name": "ToolSearch",
        "tool_input": {"query": "memory", "max_results": 5},
    }

    result = _run("claude", json.dumps(payload), tmp_path, policy_path)

    assert result.returncode == 0
    assert result.stdout == ""
