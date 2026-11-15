"""Prepare run-owned launch artifacts for non-OpenCode executors."""

from __future__ import annotations

import hashlib
import json
import os
import uuid
from dataclasses import dataclass
from pathlib import Path
from types import MappingProxyType
from typing import Any, Mapping

from compchem_memory.harness.neutral import NeutralConfig
from compchem_memory.harness.protocol import LaunchContext
from compchem_memory.harness.render_common import policy_files

from .executors.contracts import ExecContext
from .executors.registry import ExecutorRefused
from .roles import RolePolicy, load_role


FORBIDDEN_CLIENT_ENV = frozenset(
    {
        "CLAUDE_CONFIG_DIR",
        "CODEX_HOME",
        "VIBE_HOME",
        "CODEX_API_KEY",
        "ANTHROPIC_API_KEY",
    }
)
ROLE_CONFIG_NAMES = {
    "worker": "campaign-worker",
    "reviewer": "campaign-reviewer",
    "helper": "helper",
    "evaluator": "evaluator",
}
ARTIFACT_NAMES = ("policy.json", "settings.json", "mcp.json")


@dataclass(frozen=True)
class PreparedLaunch:
    """An immutable noninteractive client launch and its run-owned identity."""

    discovery_dir: Path
    invocation_dir: Path
    argv: tuple[str, ...]
    env: Mapping[str, str]
    files: Mapping[Path, bytes]
    policy_digest: str
    config_digest: str
    required_capability_identity: str


def prepare_launch(context: ExecContext) -> PreparedLaunch:
    """Validate capability evidence and materialize one isolated launch contract."""
    _require_context(context)
    route = context.route
    assert route is not None
    policy = load_role(ROLE_CONFIG_NAMES[context.role])
    required = _required_capabilities(policy, context)
    evidence = _capability_evidence(context, required)
    discovery_dir = Path(str(evidence["discovery_dir"])).resolve()
    project_dir = context.project_dir.resolve()
    if discovery_dir != project_dir:
        raise ExecutorRefused("capability discovery directory does not match the project")
    read_roots = _canonical_read_roots(policy, context, required)
    edit_roots = _canonical_edit_roots(policy, context)
    invocation_dir = _invocation_dir(context)
    capability_identity = _capability_identity(context, evidence, required, discovery_dir)
    policy_payload = _policy_payload(
        context, policy, required, read_roots, edit_roots, capability_identity
    )
    policy_body = _policy_body(context, invocation_dir, policy_payload)
    settings_body = _json_bytes(_settings_payload(context, policy, required))
    mcp_body = _json_bytes(_mcp_payload(context, policy))
    _reject_forbidden_values(context, policy_body, settings_body, mcp_body)
    policy_digest = _digest(policy_body)
    config_digest = _digest(settings_body + b"\0" + mcp_body)
    env = _effective_environment(context, invocation_dir / "policy.json")
    if context.resume_from is not None:
        return _resume_launch(
            context,
            discovery_dir,
            invocation_dir,
            env,
            policy_digest,
            config_digest,
            capability_identity,
        )
    if invocation_dir.exists():
        raise ExecutorRefused(f"launch invocation already exists: {invocation_dir}")
    artifact_bodies = {
        invocation_dir / "policy.json": policy_body,
        invocation_dir / "settings.json": settings_body,
        invocation_dir / "mcp.json": mcp_body,
    }
    metadata = _launch_metadata(
        context,
        discovery_dir,
        invocation_dir,
        policy_digest,
        config_digest,
        capability_identity,
        artifact_bodies,
    )
    artifact_bodies[invocation_dir / "launch.json"] = _json_bytes(metadata)
    _materialize(invocation_dir, artifact_bodies)
    return _prepared(
        context,
        discovery_dir,
        invocation_dir,
        env,
        artifact_bodies,
        policy_digest,
        config_digest,
        capability_identity,
    )


def _require_context(context: ExecContext) -> None:
    if context.route is None or context.route.client == "opencode":
        raise ExecutorRefused("run-owned launch preparation requires a new-client route")
    if context.role not in ROLE_CONFIG_NAMES:
        raise ExecutorRefused(f"launch policy does not provide the {context.role} role")
    required_paths = {
        "installation root": context.installation_root,
        "workspace root": context.workspace_root,
        "project directory": context.project_dir,
        "run directory": context.run_dir,
        "control directory": context.control_dir,
        "work directory": context.work_dir,
    }
    missing = [name for name, value in required_paths.items() if value is None]
    if missing:
        raise ExecutorRefused("missing launch context: " + ", ".join(missing))
    if not context.project_identity:
        raise ExecutorRefused("missing project identity")
    if not context.python_executable or context.tools_port is None:
        raise ExecutorRefused("missing Python executable or tools port")
    if not context.launch_argv:
        raise ExecutorRefused("missing proved client launch argv")
    if _is_cancelled(context.cancellation):
        raise ExecutorRefused("launch cancelled before preparation")


def _is_cancelled(cancellation: Any) -> bool:
    if cancellation is None:
        return False
    is_set = getattr(cancellation, "is_set", None)
    return bool(is_set()) if callable(is_set) else bool(cancellation)


def _required_capabilities(
    policy: RolePolicy, context: ExecContext
) -> tuple[str, ...]:
    requested = tuple(dict.fromkeys(context.required_capabilities))
    allowed = set(policy.allowed_native_capabilities) | set(policy.mcp_tool_allowlist)
    unsupported = sorted(set(requested) - allowed)
    if unsupported:
        raise ExecutorRefused("unsupported required capabilities: " + ", ".join(unsupported))
    if context.role in {"worker", "reviewer"}:
        baseline = (*policy.allowed_native_capabilities, *policy.mcp_tool_allowlist)
        requested = tuple(dict.fromkeys((*baseline, *requested)))
    if context.role == "helper" and "native_read" in requested:
        if not context.capability_read_roots:
            raise ExecutorRefused("helper native_read requires a proved lane read root")
    return requested


def _capability_evidence(
    context: ExecContext, required: tuple[str, ...]
) -> Mapping[str, Any]:
    assert context.route is not None
    client = context.role_capabilities.get(context.route.client)
    evidence = client.get(context.role) if isinstance(client, Mapping) else None
    if not isinstance(evidence, Mapping) or evidence.get("accepted") is not True:
        raise ExecutorRefused("missing accepted launch capability evidence")
    if evidence.get("stale") is True:
        raise ExecutorRefused("stale launch capability evidence")
    if evidence.get("trusted") is not True:
        raise ExecutorRefused("untrusted launch capability evidence")
    if not evidence.get("identity") or not evidence.get("discovery_dir"):
        raise ExecutorRefused("missing capability configuration identity")
    supported = evidence.get("capabilities", ())
    if not isinstance(supported, (list, tuple, set, frozenset)):
        raise ExecutorRefused("invalid capability evidence")
    missing = sorted(set(required) - set(supported))
    if missing:
        raise ExecutorRefused("unsupported required capabilities: " + ", ".join(missing))
    return evidence


def _canonical_read_roots(
    policy: RolePolicy, context: ExecContext, required: tuple[str, ...]
) -> tuple[str, ...]:
    assert context.project_dir is not None
    roots: list[Path] = []
    if policy.role in {"campaign-worker", "campaign-reviewer"}:
        roots.append(context.project_dir)
        roots.extend(context.shared_rule_files)
        roots.extend(context.shared_skill_files)
    if context.role == "helper" and "native_read" in required:
        roots.extend(context.capability_read_roots)
    return tuple(str(path.resolve()) for path in roots)


def _canonical_edit_roots(policy: RolePolicy, context: ExecContext) -> tuple[str, ...]:
    if not policy.edit_roots:
        return ()
    assert context.work_dir is not None
    return (str(context.work_dir.resolve()),)


def _capability_identity(
    context: ExecContext,
    evidence: Mapping[str, Any],
    required: tuple[str, ...],
    discovery_dir: Path,
) -> str:
    assert context.route is not None
    value = {
        "client": context.route.client,
        "role": context.role,
        "evidence": evidence["identity"],
        "discovery_dir": str(discovery_dir),
        "required": list(required),
    }
    return _digest(_json_bytes(value))


def _policy_payload(
    context: ExecContext,
    policy: RolePolicy,
    required: tuple[str, ...],
    read_roots: tuple[str, ...],
    edit_roots: tuple[str, ...],
    capability_identity: str,
) -> dict[str, Any]:
    return {
        "schema_version": policy.version,
        "role": context.role,
        "policy_role": policy.role,
        "canonical_read_roots": list(read_roots),
        "read_deny_patterns": list(policy.read_deny_patterns),
        "deny_path_forms": ["requested", "resolved"],
        "edit_roots": list(edit_roots),
        "effective_capabilities": list(required),
        "mcp_tool_allowlist": list(policy.mcp_tool_allowlist),
        "delegation_policy": policy.delegation_policy,
        "required_capability_identity": capability_identity,
        "deny_unknown_tools": True,
        "deny_shell": True,
        "deny_unconfined_code_execution": True,
    }


def _policy_body(
    context: ExecContext, invocation_dir: Path, payload: dict[str, Any]
) -> bytes:
    assert context.route is not None
    assert context.project_dir is not None
    assert context.run_dir is not None
    assert context.control_dir is not None
    assert context.work_dir is not None
    launch_context = LaunchContext(
        root=context.installation_root,
        project=context.project_identity,
        project_dir=context.project_dir,
        python=context.python_executable,
        tools_port=context.tools_port,
        model=context.route.model,
        setup=dict(context.setup),
        run_dir=context.run_dir,
        control_dir=context.control_dir,
        work_dir=context.work_dir,
        role=context.role,
        invocation_id=invocation_dir.name,
        noninteractive=True,
        role_policy=payload,
    )
    files, _ = policy_files(
        NeutralConfig([], frozenset({"bash", "shell", "delegate", "code"})),
        launch_context,
        context.route.hook_harness,
    )
    return next(iter(files.values()))


def _settings_payload(
    context: ExecContext, policy: RolePolicy, required: tuple[str, ...]
) -> dict[str, Any]:
    assert context.route is not None
    cap = context.role_cap if context.role_cap is not None else policy.default_cap
    return {
        "schema_version": "magnolia-launch-settings-v1",
        "client": context.route.client,
        "model": context.route.model,
        "role": context.role,
        "max_turns": min(cap, policy.default_cap),
        "no_tools": not required,
        "one_turn": context.role == "evaluator",
        "stdout_reply": context.role == "helper",
    }


def _mcp_payload(context: ExecContext, policy: RolePolicy) -> dict[str, Any]:
    service = None
    if policy.mcp_tool_allowlist and context.memory_service:
        allowed = {
            "transport",
            "url",
            "authorization_env",
            "command",
            "args",
            "env_vars",
        }
        service = {
            key: value for key, value in context.memory_service.items() if key in allowed
        }
    return {
        "schema_version": "magnolia-launch-mcp-v1",
        "memory_service": service,
        "tool_allowlist": list(policy.mcp_tool_allowlist),
    }


def _invocation_dir(context: ExecContext) -> Path:
    assert context.control_dir is not None
    if context.resume_from is not None:
        path = context.resume_from
        return path.parent.resolve() if path.name == "launch.json" else path.resolve()
    invocation = context.invocation_id or uuid.uuid4().hex
    if not invocation or invocation in {".", ".."} or Path(invocation).name != invocation:
        raise ExecutorRefused("invalid launch invocation id")
    return (context.control_dir / "executors" / invocation).resolve()


def _effective_environment(context: ExecContext, policy_path: Path) -> dict[str, str]:
    inherited = context.environment or os.environ
    env = {
        str(key): str(value)
        for key, value in inherited.items()
        if key not in FORBIDDEN_CLIENT_ENV
    }
    env.update(
        {
            str(key): str(value)
            for key, value in context.renderer_env.items()
            if key not in FORBIDDEN_CLIENT_ENV
        }
    )
    assert context.route is not None
    assert context.installation_root is not None
    assert context.workspace_root is not None
    assert context.run_dir is not None
    assert context.control_dir is not None
    assert context.work_dir is not None
    explicit = {
        "MAGNOLIA_HOOK_POLICY": str(policy_path.resolve()),
        "MAGNOLIA_HOOK_POLICY_REQUIRED": "1",
        "MAGNOLIA_DOORBELL": "0",
        "MAGNOLIA_CLIENT": context.route.client,
        "MAGNOLIA_ROLE": context.role,
        "MAGNOLIA_PROJECT_IDENTITY": context.project_identity,
        "MAGNOLIA_INSTALLATION_ROOT": str(context.installation_root.resolve()),
        "MAGNOLIA_WORKSPACE_ROOT": str(context.workspace_root.resolve()),
        "MAGNOLIA_RUN_DIR": str(context.run_dir.resolve()),
        "MAGNOLIA_CONTROL_DIR": str(context.control_dir.resolve()),
        "MAGNOLIA_WORK_DIR": str(context.work_dir.resolve()),
        "MAGNOLIA_PYTHON": context.python_executable,
        "COMPCHEM_TOOLS_PORT": str(context.tools_port),
    }
    if context.route.model:
        explicit["MAGNOLIA_MODEL"] = context.route.model
    env.update({key: str(value) for key, value in explicit.items()})
    return env


def _launch_metadata(
    context: ExecContext,
    discovery_dir: Path,
    invocation_dir: Path,
    policy_digest: str,
    config_digest: str,
    capability_identity: str,
    artifacts: Mapping[Path, bytes],
) -> dict[str, Any]:
    assert context.route is not None
    return {
        "schema_version": "magnolia-prepared-launch-v1",
        "trusted": True,
        "client": context.route.client,
        "model": context.route.model,
        "role": context.role,
        "project_identity": context.project_identity,
        "discovery_dir": str(discovery_dir),
        "invocation_dir": str(invocation_dir),
        "argv": list(context.launch_argv),
        "policy_digest": policy_digest,
        "config_digest": config_digest,
        "required_capability_identity": capability_identity,
        "artifacts": {
            path.name: _digest(body) for path, body in sorted(artifacts.items())
        },
    }


def _resume_launch(
    context: ExecContext,
    discovery_dir: Path,
    invocation_dir: Path,
    env: dict[str, str],
    policy_digest: str,
    config_digest: str,
    capability_identity: str,
) -> PreparedLaunch:
    metadata_path = invocation_dir / "launch.json"
    try:
        metadata = json.loads(metadata_path.read_text(encoding="utf-8"))
    except (OSError, ValueError, TypeError) as exc:
        raise ExecutorRefused("missing or invalid resume launch reference") from exc
    expected = {
        "trusted": True,
        "policy_digest": policy_digest,
        "config_digest": config_digest,
        "required_capability_identity": capability_identity,
        "discovery_dir": str(discovery_dir),
        "argv": list(context.launch_argv),
    }
    for key, value in expected.items():
        if metadata.get(key) != value:
            raise ExecutorRefused(f"stale or untrusted resume launch reference: {key}")
    files: dict[Path, bytes] = {}
    recorded = metadata.get("artifacts")
    if not isinstance(recorded, Mapping):
        raise ExecutorRefused("invalid resume artifact manifest")
    for name in (*ARTIFACT_NAMES, "launch.json"):
        path = invocation_dir / name
        try:
            body = path.read_bytes()
        except OSError as exc:
            raise ExecutorRefused(f"missing resume launch artifact: {name}") from exc
        if path.stat().st_mode & 0o222:
            raise ExecutorRefused(f"untrusted writable resume launch artifact: {name}")
        if name != "launch.json" and recorded.get(name) != _digest(body):
            raise ExecutorRefused(f"stale resume launch artifact: {name}")
        files[path] = body
    return _prepared(
        context,
        discovery_dir,
        invocation_dir,
        env,
        files,
        policy_digest,
        config_digest,
        capability_identity,
    )


def _materialize(invocation_dir: Path, artifacts: Mapping[Path, bytes]) -> None:
    invocation_dir.mkdir(parents=True, mode=0o700)
    for path, body in artifacts.items():
        path.write_bytes(body)
        path.chmod(0o444)


def _reject_forbidden_values(context: ExecContext, *bodies: bytes) -> None:
    """Refuse a filtered client credential copied into argv or an artifact."""
    values = {
        str(context.environment[name])
        for name in FORBIDDEN_CLIENT_ENV
        if context.environment.get(name)
    }
    destinations = [*context.launch_argv, *(body.decode("utf-8") for body in bodies)]
    if any(value in destination for value in values for destination in destinations):
        raise ExecutorRefused("forbidden client environment value in launch contract")


def _prepared(
    context: ExecContext,
    discovery_dir: Path,
    invocation_dir: Path,
    env: Mapping[str, str],
    files: Mapping[Path, bytes],
    policy_digest: str,
    config_digest: str,
    capability_identity: str,
) -> PreparedLaunch:
    return PreparedLaunch(
        discovery_dir=discovery_dir,
        invocation_dir=invocation_dir,
        argv=tuple(context.launch_argv),
        env=MappingProxyType(dict(env)),
        files=MappingProxyType(dict(files)),
        policy_digest=policy_digest,
        config_digest=config_digest,
        required_capability_identity=capability_identity,
    )


def _json_bytes(value: Mapping[str, Any]) -> bytes:
    return (json.dumps(value, sort_keys=True, separators=(",", ":")) + "\n").encode()


def _digest(body: bytes) -> str:
    return hashlib.sha256(body).hexdigest()
