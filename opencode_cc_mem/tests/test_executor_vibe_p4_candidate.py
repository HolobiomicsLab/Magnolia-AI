"""Integrated, offline-only Vibe candidate construction and identity proof."""

from __future__ import annotations

import copy
import hashlib
import json
import os
import sys
import tomllib
from dataclasses import replace
from pathlib import Path

from campaign_runner.executors import ExecutorRefused
from campaign_runner.executors.vibe import VibeExecutor
from campaign_runner.launch import ROLE_CONFIG_NAMES
from campaign_runner.roles import load_role
from campaign_runner.executors.capabilities import (
    load_capability_table,
    project_role_capabilities,
)
from campaign_runner.launch import (
    VIBE_BASE_ENV,
    _identity_replacements,
    _normalized_launch_identity,
    _replace_identity_text,
    configuration_identity,
    prepare_launch,
)
from compchem_memory.harness.render_vibe import READ_ONLY_DISABLED_TOOLS, disabled_tools_for_role
from compchem_memory.harness.memory_service import ServiceHandle
from test_executor_launch import _candidate_role_capabilities, _fake_vibe_environment


ROLES = ("worker", "reviewer", "helper", "evaluator")
ROOT = Path(__file__).parents[1]
OLD_CANDIDATE_IDENTITIES = (
    ROOT / "tests/fixtures/executors/vibe/live-4-candidate-identities.json"
)
MEMORY_TOOLS = (
    "mcp:compchem-memory.memory_get_context",
    "mcp:compchem-memory.memory_search",
    "mcp:compchem-memory.memory_get_run_history",
)
EXPLICIT_ENV = {
    "MAGNOLIA_CLIENT",
    "MAGNOLIA_ROLE",
    "MAGNOLIA_MODEL",
    "MAGNOLIA_PROJECT_DIR",
    "MAGNOLIA_PROJECT_IDENTITY",
    "MAGNOLIA_INSTALLATION_ROOT",
    "MAGNOLIA_WORKSPACE_ROOT",
    "MAGNOLIA_RUN_DIR",
    "MAGNOLIA_CONTROL_DIR",
    "MAGNOLIA_WORK_DIR",
    "MAGNOLIA_ROOT",
    "MAGNOLIA_PYTHON",
    "MAGNOLIA_HOOK_POLICY",
    "MAGNOLIA_HOOK_POLICY_REQUIRED",
    "MAGNOLIA_HOOK_POLICY_SHA256",
    "COMPCHEM_TOOLS_PORT",
    "PYTHONPYCACHEPREFIX",
    "VIBE_ENABLE_TELEMETRY",
}


def _candidate_rows():
    """Project Vibe candidates in memory; never mutate the production table."""
    candidates = _candidate_role_capabilities()
    table = load_capability_table()
    vibe_rows = [row for row in table["rows"] if row["client"] == "vibe"]
    assert {row["role"] for row in vibe_rows} == set(ROLES)
    for row in vibe_rows:
        for name in (
            "argv", "model_delivery", "discovery_dir", "trust",
            "hook_policy_digest", "native_schemas", "allowed_actions",
            "denied_actions", "resume", "caps", "usage_semantics",
        ):
            row["cells"][name]["status"] = "observed"
        row["cells"]["trust"]["value"] = True
        row["cells"]["hook_policy_digest"]["value"] = "computed-per-candidate-context"
        row["cells"]["discovery_dir"]["value"] = {"scope": "launch-project"}
        row["cells"]["model_delivery"]["value"] = {
            "accepted": True,
            "model": "selected-model",
            "identity": "offline-candidate-model-binding",
        }
        row["cells"]["allowed_actions"]["value"] = (
            ["native_read", "native_glob", "native_grep", "native_edit", *MEMORY_TOOLS]
            if row["role"] == "worker"
            else ["native_read", "native_glob", "native_grep"]
        )
        row["gate"]["status"] = "observed"
        row["stale"] = False
    candidates["vibe"] = project_role_capabilities(table)["vibe"]
    return vibe_rows, candidates["vibe"]


def _candidate_context(tmp_path, candidate, role, *, memory=False):
    from test_executor_vibe import _context

    binary, sentinel, _ = _fake_vibe_environment(tmp_path)
    binary.write_bytes(b"#!/bin/sh\nexit 0\n")
    binary.chmod(0o755)
    context = _context(tmp_path, role=role, binary=binary)
    evidence = copy.deepcopy(candidate[role])
    if memory:
        evidence["vibe_memory"] = True
    context = replace(
        context,
        role_capabilities={"vibe": {role: evidence}},
        python_executable=sys.executable,
        environment={
            "PATH": os.environ.get("PATH", ""),
            "MAGNOLIA_PYTHON": sys.executable,
            "VIBE_HOME": "must-not-reach-child",
            "ANTHROPIC_API_KEY": "must-not-reach-child",
            "CODEX_API_KEY": "must-not-reach-child",
            "OPENAI_API_KEY": "must-not-reach-child",
            "MISTRAL_API_KEY": "must-not-reach-child",
            "MAGNOLIA_LLM_API_KEY": "memory-key-sentinel",
            "MAGNOLIA_MEMORY_SERVICE_TOKEN": "untrusted-parent-token",
        },
        setup={
            **context.setup,
            "memory_provider": "openai",
            "memory_model": "offline-memory-model",
        },
        memory_service={"transport": "managed"} if memory else context.memory_service,
    )
    context.role_capabilities["vibe"][role]["identity"] = configuration_identity(context)
    return context, sentinel


def _identity(context, launch):
    native = {
        str(path.relative_to(launch.invocation_dir)): body
        for path, body in launch.files.items()
        if str(path.relative_to(launch.invocation_dir)).startswith(".vibe/")
    }
    return _normalized_launch_identity(
        context,
        launch.invocation_dir,
        launch.env,
        launch.files[launch.invocation_dir / "settings.json"],
        launch.files[launch.invocation_dir / "mcp.json"],
        native,
        launch.files[launch.invocation_dir / "policy.json"],
    )


def _public_identity(identity):
    """The executable itself is a live Task 6 binding, not a relocatable artifact."""
    return {
        "schema_version": identity["schema_version"],
        "components": identity["components"],
    }


def _prepare_record(context, role):
    launch = prepare_launch(context)
    identity = _identity(context, launch)
    for relative in (".vibe/config.toml", ".vibe/hooks.toml"):
        (context.project_dir / relative).write_bytes(
            launch.files[launch.invocation_dir / relative]
        )
    executor = VibeExecutor(
        prepared=launch,
        model=context.route.model,
        role=role,
        max_turns=load_role(ROLE_CONFIG_NAMES[role]).default_cap,
        timeout_seconds=context.timeout_seconds,
    )
    argv = executor._argv("offline candidate shape check", None, executor.max_turns)
    config = tomllib.loads(
        (launch.invocation_dir / ".vibe/config.toml").read_text(encoding="utf-8")
    )
    hooks = tomllib.loads(
        (launch.invocation_dir / ".vibe/hooks.toml").read_text(encoding="utf-8")
    )
    mcp = json.loads((launch.invocation_dir / "mcp.json").read_text(encoding="utf-8"))
    policy = json.loads((launch.invocation_dir / "policy.json").read_text(encoding="utf-8"))
    for relative in (".vibe/config.toml", ".vibe/hooks.toml"):
        assert (context.project_dir / relative).read_bytes() == launch.files[
            launch.invocation_dir / relative
        ]
    return {
        "context": context,
        "launch": launch,
        "executor": executor,
        "identity": identity,
        "config": config,
        "hooks": hooks["hooks"],
        "mcp": mcp,
        "policy": policy,
        "argv": argv,
    }


def _managed_handle(token_file):
    return ServiceHandle(
        url="http://127.0.0.1:43101/mcp",
        project_digest="offline-project-digest",
        process_identity="offline-process",
        project_dir=token_file.parent,
        holder_id="offline-holder",
        service_identity="offline-service",
        token_file=token_file,
    )


def _public_preparation(context, launch, mcp):
    replacements = _identity_replacements(context, launch.invocation_dir)
    if context.installation_root is not None:
        replacements = (
            *replacements,
            (str(context.installation_root.parent.resolve()), "{magnolia_root}"),
        )
    service_url = (mcp.get("memory_service") or {}).get("url")
    if service_url:
        replacements = (*replacements, (service_url, "{memory_service_url}"))

    def normalize(value):
        if isinstance(value, dict):
            return {key: normalize(item) for key, item in value.items()}
        if isinstance(value, list):
            return [normalize(item) for item in value]
        if isinstance(value, str):
            return _replace_identity_text(value, replacements)
        return value

    environment = {
        key: normalize(value)
        for key, value in sorted(launch.env.items())
        if key in EXPLICIT_ENV | VIBE_BASE_ENV | {
            "MAGNOLIA_MEMORY_PROVIDER", "MAGNOLIA_MEMORY_MODEL",
        }
        and key not in {"MAGNOLIA_MEMORY_SERVICE_TOKEN", "PYTHONPYCACHEPREFIX"}
    }
    if "PATH" in environment:
        environment["PATH"] = "{inherited_path}"
    return environment, normalize(mcp), normalize


def _diff(old, new):
    return {
        component: {"old": old.get(component), "new": new.get(component)}
        for component in sorted(set(old) | set(new))
        if old.get(component) != new.get(component)
    }


def test_vibe_p4_candidate_worker_memory_dry_run(tmp_path, monkeypatch):
    _, candidate = _candidate_rows()
    assert all(candidate[role]["accepted"] for role in ROLES)
    context, sentinel = _candidate_context(tmp_path, candidate, "worker", memory=True)
    token_file = tmp_path / "managed-token"
    token_file.write_text("offline-memory-bearer-sentinel\n", encoding="utf-8")
    token_file.chmod(0o600)
    handle = _managed_handle(token_file)
    acquired = []
    monkeypatch.setattr(
        "compchem_memory.harness.memory_service.ensure_memory_service",
        lambda context, *, tool_allowlist: acquired.append(tuple(tool_allowlist)) or handle,
    )
    monkeypatch.setattr(
        "compchem_memory.harness.memory_service.release_memory_service",
        lambda _handle: True,
    )

    record = _prepare_record(context, "worker")

    assert acquired == [MEMORY_TOOLS]
    assert record["launch"].memory_service_handle is handle
    assert record["executor"].prepared.memory_service_handle is handle
    assert record["config"]["active_model"] == "selected-model"
    assert set(record["config"]["disabled_tools"]) == set(disabled_tools_for_role("worker"))
    assert set(record["config"]["disabled_tools"]) == {"bash"}
    assert record["config"]["enable_subagents"] is False
    assert record["config"]["enable_background_processes"] is False
    assert record["mcp"]["memory_service"]["url"] == "http://127.0.0.1:43101/mcp"
    assert record["mcp"]["memory_service"]["authorization_env"] == "MAGNOLIA_MEMORY_SERVICE_TOKEN"
    assert record["mcp"]["tool_allowlist"] == list(MEMORY_TOOLS)
    assert record["policy"]["mcp_tool_allowlist"] == list(MEMORY_TOOLS)
    assert record["launch"].env["MAGNOLIA_MEMORY_SERVICE_TOKEN"] == "offline-memory-bearer-sentinel"
    assert record["launch"].env["MAGNOLIA_MODEL"] == "selected-model"
    assert record["launch"].env["VIBE_ENABLE_TELEMETRY"] == "false"
    assert "offline-memory-bearer-sentinel" not in "\n".join(
        body.decode() for body in record["launch"].files.values()
    )
    assert any(
        hook.get("type") == "pre_tool" and hook.get("strict") is True
        for hook in record["hooks"]
    )
    assert record["argv"][0] == str(context.launch_argv[0])
    assert "--auto-approve" in record["argv"]
    assert not any(flag in record["argv"] for flag in ("--trust", "--yolo"))
    assert record["identity"]["components"]["client_version"] == "3.1.2"
    assert record["identity"]["components"]["client_sha256"] == hashlib.sha256(
        context.launch_argv[0] and Path(context.launch_argv[0]).read_bytes()
    ).hexdigest()
    assert not sentinel.exists(), "the integrated dry run must never execute Vibe"


def test_vibe_p4_candidate_readonly_roles_dry_run(tmp_path, monkeypatch):
    _, candidate = _candidate_rows()
    acquired = []
    monkeypatch.setattr(
        "compchem_memory.harness.memory_service.ensure_memory_service",
        lambda context, **kwargs: acquired.append((context, kwargs)),
    )

    for role in ("reviewer", "helper", "evaluator"):
        context, sentinel = _candidate_context(tmp_path / role, candidate, role)
        record = _prepare_record(context, role)

        assert record["launch"].memory_service_handle is None
        assert record["executor"].prepared.memory_service_handle is None
        assert record["mcp"]["memory_service"] is None
        assert record["mcp"]["tool_allowlist"] == []
        assert record["policy"]["mcp_tool_allowlist"] == []
        assert "MAGNOLIA_MEMORY_SERVICE_TOKEN" not in record["launch"].env
        assert record["config"]["active_model"] == "selected-model"
        assert set(record["config"]["disabled_tools"]) == set(READ_ONLY_DISABLED_TOOLS)
        assert set(record["config"]["disabled_tools"]) == {"bash", "edit", "write_file"}
        assert record["config"]["enable_subagents"] is False
        assert record["config"]["enable_background_processes"] is False
        assert record["argv"][0] == str(context.launch_argv[0])
        assert record["launch"].env["MAGNOLIA_MODEL"] == "selected-model"
        assert record["launch"].env["VIBE_ENABLE_TELEMETRY"] == "false"
        assert set(record["launch"].env) >= EXPLICIT_ENV
        assert {
            key for key in record["launch"].env
            if not key.startswith("MAGNOLIA_") and key not in EXPLICIT_ENV
        } <= VIBE_BASE_ENV
        assert "VIBE_HOME" not in record["launch"].env
        assert not {
            "ANTHROPIC_API_KEY", "CODEX_API_KEY", "OPENAI_API_KEY", "MISTRAL_API_KEY"
        } & set(record["launch"].env)
        assert record["identity"]["components"]["client_version"] == "3.1.2"
        assert not sentinel.exists(), "role construction must never execute Vibe"
    assert acquired == []


def test_vibe_p4_production_rows_remain_refused_and_unobserved(tmp_path):
    table = load_capability_table()
    vibe_rows = [row for row in table["rows"] if row["client"] == "vibe"]
    production = project_role_capabilities(table)["vibe"]
    _, candidate = _candidate_rows()

    assert all(candidate[role]["accepted"] for role in ROLES)
    assert all(not production[role]["accepted"] for role in ROLES)
    assert all(row["cells"]["model_delivery"]["status"] != "observed" for row in vibe_rows)
    assert all(row["cells"]["vibe_memory"]["status"] != "observed" for row in vibe_rows)
    for role in ROLES:
        from test_executor_vibe import _context

        context = _context(tmp_path / role, role=role)
        context = replace(
            context, role_capabilities={"vibe": {role: production[role]}}
        )
        try:
            prepare_launch(context)
        except ExecutorRefused as exc:
            assert "missing accepted launch capability evidence" in str(exc)
        else:
            raise AssertionError("a production Vibe row must not be accepted offline")


def test_vibe_p4_identity_diff_is_explicit_and_relocation_stable(tmp_path, monkeypatch):
    _, candidate = _candidate_rows()
    monkeypatch.setattr(
        "campaign_runner.launch._hook_interpreter_digest",
        lambda _context, _env: "offline-fixed-hook-interpreter",
    )
    monkeypatch.setattr(
        "compchem_memory.harness.memory_service.release_memory_service",
        lambda _handle: True,
    )
    old = json.loads(OLD_CANDIDATE_IDENTITIES.read_text(encoding="utf-8"))
    identities = {}
    diffs = {}
    preparations = {}
    for role in ROLES:
        token_file = tmp_path / role / "managed-token"
        token_file.parent.mkdir()
        token_file.write_text("offline-memory-bearer-sentinel\n", encoding="utf-8")
        token_file.chmod(0o600)
        handle = _managed_handle(token_file)
        monkeypatch.setattr(
            "compchem_memory.harness.memory_service.ensure_memory_service",
            lambda _context, *, tool_allowlist, handle=handle: handle,
        )
        context, sentinel = _candidate_context(
            tmp_path / role, candidate, role, memory=role == "worker"
        )
        record = _prepare_record(context, role)
        identity = _public_identity(record["identity"])
        relocated_context, relocated_sentinel = _candidate_context(
            tmp_path / "relocated" / role, candidate, role, memory=role == "worker"
        )
        relocated = _prepare_record(relocated_context, role)
        assert _public_identity(relocated["identity"]) == identity
        assert not sentinel.exists() and not relocated_sentinel.exists()
        identities[role] = identity
        diffs[role] = _diff(old[role]["components_without_client_version"], identity["components"])
        public_environment, public_mcp, normalize = _public_preparation(
            context, record["launch"], record["mcp"]
        )
        preparations[role] = {
            "accepted_candidate_only": True,
            "candidate_evidence_accepted": candidate[role]["accepted"],
            "production_row_accepted": False,
            "model": record["config"]["active_model"],
            "argv": ["{executable}", *normalize(record["argv"][1:])],
            "environment": public_environment,
            "memory_bearer_env_present": role == "worker",
            "disabled_tools": record["config"]["disabled_tools"],
            "mcp": public_mcp,
            "strict_pre_tool_hook": any(
                hook.get("type") == "pre_tool" and hook.get("strict") is True
                for hook in record["hooks"]
            ),
            "configuration_identity_sha256": record["launch"].config_digest,
            "vibe_executable_run": False,
        }

    assert diffs["worker"]["native_files"]["old"] != diffs["worker"]["native_files"]["new"]
    assert all(diff["client_version"] == {"old": None, "new": "3.1.2"} for diff in diffs.values())
    artifact = {
        "schema_version": "magnolia-vibe-p4-candidate-identities-v1",
        "evidence_status": "offline-in-memory-candidates-only; no production acceptance",
        "production_rows_unchanged": True,
        "model": "selected-model",
        "identity_comparison": "Plan 3 live-4 components_without_client_version -> Plan 4 normalized components",
        "normalization": "installation roots, run paths and managed-memory URL replaced by production placeholders; executable path omitted",
        "roles": {
            role: {
                "identity": identities[role],
                "component_diff": diffs[role],
                "dry_run": preparations[role],
            }
            for role in ROLES
        },
    }
    rendered = json.dumps(artifact, indent=2, sort_keys=True) + "\n"
    # Tests never write into the source tree; set MAGNOLIA_P4_IDENTITY_RECORD to keep a copy.
    record = Path(os.environ.get("MAGNOLIA_P4_IDENTITY_RECORD") or tmp_path / "candidate-identities.json")
    record.parent.mkdir(parents=True, exist_ok=True)
    record.write_text(rendered, encoding="utf-8")
    assert "/Users/" not in rendered and str(tmp_path) not in rendered
    assert "offline-memory-bearer-sentinel" not in rendered
    assert json.loads(record.read_text(encoding="utf-8")) == artifact
