"""Contract tests for the confined ``magnolia-read`` stdio server."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from compchem_memory.harness.read_server import ReadRefused, ReadServer


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


def _server(tmp_path: Path) -> tuple[ReadServer, Path, Path]:
    project = tmp_path / "project"
    outside = tmp_path / "outside"
    project.mkdir()
    outside.mkdir()
    policy = _policy(tmp_path / "policy.json", project)
    return ReadServer.from_policy_file(policy, cwd=project), project, outside


def test_read_server_confines_reads_to_policy_roots(tmp_path):
    server, project, outside = _server(tmp_path)
    (project / "notes.txt").write_text("allowed\n", encoding="utf-8")
    (project / "Case.ENV").write_text("denied\n", encoding="utf-8")
    (outside / "outside.txt").write_text("outside\n", encoding="utf-8")
    (project / "alias").symlink_to(outside, target_is_directory=True)

    assert server.read_file("notes.txt")["content"] == "allowed\n"
    assert server.list_dir(".")["entries"] == ["notes.txt"]
    for path in (
        "../outside/outside.txt",
        "alias/outside.txt",
        "Case.ENV",
        "~/.ssh/id_rsa",
        "$HOME/.ssh/id_rsa",
    ):
        with pytest.raises(ReadRefused):
            server.read_file(path)


def test_read_server_refuses_without_policy(tmp_path):
    with pytest.raises(ReadRefused, match="MAGNOLIA_HOOK_POLICY"):
        ReadServer.from_environment({}, cwd=tmp_path)
    with pytest.raises(ReadRefused, match="invalid"):
        ReadServer.from_policy_file(tmp_path / "missing.json", cwd=tmp_path)


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
