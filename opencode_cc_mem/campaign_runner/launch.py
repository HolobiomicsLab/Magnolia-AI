"""Prepare run-owned launch artifacts for non-OpenCode executors."""

from __future__ import annotations

import hashlib
import json
import os
import re
import stat
import uuid
from dataclasses import dataclass
from pathlib import Path
from types import MappingProxyType
from typing import Any, Mapping

from compchem_memory.harness.neutral import NeutralConfig
from compchem_memory.harness.protocol import LaunchContext
from compchem_memory.harness.render_claude import (
    native_artifacts as render_claude_native_artifacts,
)
from compchem_memory.harness.render_codex import (
    native_artifacts as render_codex_native_artifacts,
)
from compchem_memory.harness.render_vibe import (
    native_artifacts as render_vibe_native_artifacts,
)
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
        "ANTHROPIC_AUTH_TOKEN",
        "OPENAI_API_KEY",
        "MISTRAL_API_KEY",
    }
)
FORBIDDEN_PATH_ENV = frozenset({"CLAUDE_CONFIG_DIR", "CODEX_HOME", "VIBE_HOME"})
FILTERED_CLIENT_ENV = FORBIDDEN_CLIENT_ENV | {"MAGNOLIA_MODEL"}
DEFAULT_GOVERNOR_CAP = 60
ROLE_CONFIG_NAMES = {
    "worker": "campaign-worker",
    "reviewer": "campaign-reviewer",
    "helper": "helper",
    "evaluator": "evaluator",
}
CLIENT_CONFIG_FILES = {
    "codex": (".codex/config.toml", ".codex/hooks.json"),
    "vibe": (".vibe/config.toml", ".vibe/hooks.toml"),
}
CODEX_READ_SERVER_TOOLS = (
    "mcp:magnolia-read.read_file",
    "mcp:magnolia-read.list_dir",
    "mcp:magnolia-read.search",
)


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
    mcp_payload = _mcp_payload(context, policy)
    mcp_body = _json_bytes(mcp_payload)
    native_bodies = _client_native_artifacts(context, invocation_dir, mcp_payload)
    policy_digest = _digest(policy_body)
    config_digest = _configuration_identity(
        context, settings_body, mcp_body, native_bodies
    )
    if evidence["identity"] != config_digest:
        raise ExecutorRefused(
            "capability configuration identity does not match discovered configuration"
        )
    env = _effective_environment(
        context, invocation_dir / "policy.json", policy_digest
    )
    if context.resume_from is not None:
        return _resume_launch(
            context,
            discovery_dir,
            invocation_dir,
            env,
            policy_digest,
            config_digest,
            capability_identity,
            {
                "policy.json": policy_body,
                "settings.json": settings_body,
                "mcp.json": mcp_body,
                **native_bodies,
            },
        )
    if invocation_dir.exists():
        raise ExecutorRefused(f"launch invocation already exists: {invocation_dir}")
    artifact_bodies = {
        invocation_dir / "policy.json": policy_body,
        invocation_dir / "settings.json": settings_body,
        invocation_dir / "mcp.json": mcp_body,
        **{invocation_dir / name: body for name, body in native_bodies.items()},
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
    _reject_forbidden_data(context, env, artifact_bodies.values())
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


def configuration_identity(context: ExecContext) -> str:
    """Return the digest of configuration this client launch will discover."""
    if context.route is None:
        raise ExecutorRefused("configuration identity requires a resolved route")
    policy = load_role(ROLE_CONFIG_NAMES[context.role])
    required = _required_capabilities(policy, context)
    settings = _json_bytes(_settings_payload(context, policy, required))
    mcp_payload = _mcp_payload(context, policy)
    mcp = _json_bytes(mcp_payload)
    invocation_name = context.invocation_id or "configuration-identity"
    native = _client_native_artifacts(context, Path(invocation_name), mcp_payload)
    return _configuration_identity(context, settings, mcp, native)


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
    if context.role_cap is not None and context.role_cap < 1:
        raise ExecutorRefused("configured role cap must be positive")
    if context.governor_cap is not None and context.governor_cap < 1:
        raise ExecutorRefused("governor cap must be positive")
    if _is_cancelled(context.cancellation):
        raise ExecutorRefused("launch cancelled before preparation")
    run_dir = context.run_dir.resolve()
    work_dir = context.work_dir.resolve()
    control_dir = context.control_dir.resolve()
    if work_dir != run_dir / "work":
        raise ExecutorRefused("work directory must be canonical run/work")
    if control_dir != run_dir / "control":
        raise ExecutorRefused("control directory must be canonical run/control")


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
    if evidence.get("stale") is not False:
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
    if context.route.model is not None:
        isolation = evidence.get("model_isolation")
        if not isinstance(isolation, Mapping) or isolation.get("accepted") is not True:
            raise ExecutorRefused("missing accepted model isolation evidence")
        if isolation.get("model") != context.route.model or not isolation.get("identity"):
            raise ExecutorRefused("model isolation evidence does not match selected model")
    return evidence


def _canonical_read_roots(
    policy: RolePolicy, context: ExecContext, required: tuple[str, ...]
) -> tuple[str, ...]:
    assert context.project_dir is not None
    shared_rules = _validated_shared_files(context, context.shared_rule_files)
    shared_skills = _validated_shared_files(context, context.shared_skill_files)
    values = {
        "{project_dir}": (context.project_dir.resolve(),),
        "{shared_rule_files}": shared_rules,
        "{shared_skill_files}": shared_skills,
        "{capability_read_roots}": tuple(
            path.resolve() for path in context.capability_read_roots
        )
        if "native_read" in required
        else (),
    }
    roots: list[str] = []
    for template in policy.canonical_read_roots:
        if template not in values:
            raise ExecutorRefused(f"unsupported canonical read-root field: {template}")
        for path in values[template]:
            resolved = str(path)
            if resolved not in roots:
                roots.append(resolved)
    return tuple(roots)


def _canonical_edit_roots(policy: RolePolicy, context: ExecContext) -> tuple[str, ...]:
    assert context.work_dir is not None
    assert context.control_dir is not None
    values = {"{work_dir}": context.work_dir.resolve()}
    roots: list[str] = []
    for template in policy.edit_roots:
        if template not in values:
            raise ExecutorRefused(f"unsupported canonical edit-root field: {template}")
        edit_root = values[template]
        if context.control_dir.resolve().is_relative_to(edit_root):
            raise ExecutorRefused("control directory must be outside every edit root")
        roots.append(str(edit_root))
    return tuple(roots)


def _validated_shared_files(
    context: ExecContext, paths: tuple[Path, ...]
) -> tuple[Path, ...]:
    """Resolve explicitly named shared files within approved source trees."""
    assert context.project_dir is not None
    assert context.installation_root is not None
    allowed = (
        context.project_dir.resolve(),
        (context.installation_root / "rules").resolve(),
        (context.installation_root / "skills").resolve(),
        (context.installation_root / ".opencode" / "skills").resolve(),
    )
    resolved: list[Path] = []
    for path in paths:
        candidate = path.resolve()
        if not candidate.is_file() or not any(
            candidate.is_relative_to(root) for root in allowed
        ):
            raise ExecutorRefused(
                f"shared rule/skill entry must be an approved file: {path}"
            )
        resolved.append(candidate)
    return tuple(resolved)


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
        "project_identity": context.project_identity,
        "evidence": evidence["identity"],
        "discovery_dir": str(discovery_dir),
        "required": list(required),
        "model_isolation": evidence.get("model_isolation"),
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
    mcp_tools = list(policy.mcp_tool_allowlist)
    if context.route is not None and (
        context.route.client == "codex" and context.role in {"worker", "reviewer"}
    ):
        mcp_tools.extend(CODEX_READ_SERVER_TOOLS)
    return {
        "schema_version": policy.version,
        "role": context.role,
        "policy_role": policy.role,
        "canonical_read_roots": list(read_roots),
        "read_deny_patterns": list(policy.read_deny_patterns),
        "deny_path_forms": ["requested", "resolved"],
        "edit_roots": list(edit_roots),
        "effective_capabilities": list(required),
        "mcp_tool_allowlist": list(dict.fromkeys(mcp_tools)),
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
    configured_cap = (
        context.role_cap if context.role_cap is not None else policy.default_cap
    )
    governor_cap = context.governor_cap or DEFAULT_GOVERNOR_CAP
    return {
        "schema_version": "magnolia-launch-settings-v1",
        "client": context.route.client,
        "model": context.route.model,
        "role": context.role,
        "max_turns": min(configured_cap, governor_cap),
        "no_tools": not required,
        "one_turn": context.role == "evaluator",
        "stdout_reply": context.role == "helper",
    }


def _mcp_payload(context: ExecContext, policy: RolePolicy) -> dict[str, Any]:
    service = None
    if policy.mcp_tool_allowlist:
        if not context.memory_service:
            raise ExecutorRefused(
                "role requires memory tools but no usable memory route was supplied"
            )
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
        _validate_memory_service(service)
    tool_allowlist = list(policy.mcp_tool_allowlist)
    if context.route is not None and (
        context.route.client == "codex" and context.role in {"worker", "reviewer"}
    ):
        tool_allowlist.extend(CODEX_READ_SERVER_TOOLS)
    return {
        "schema_version": "magnolia-launch-mcp-v1",
        "memory_service": service,
        "tool_allowlist": list(dict.fromkeys(tool_allowlist)),
    }


def _client_native_artifacts(
    context: ExecContext,
    invocation_dir: Path,
    mcp_payload: Mapping[str, Any],
) -> dict[str, bytes]:
    """Render client syntax while retaining neutral identity payloads."""
    assert context.route is not None
    assert context.installation_root is not None
    assert context.project_dir is not None
    assert context.control_dir is not None
    launch_context = LaunchContext(
        root=context.installation_root,
        project=context.project_identity or "",
        project_dir=context.project_dir,
        python=context.python_executable or "",
        tools_port=context.tools_port or 0,
        model=context.route.model,
        setup=dict(context.setup),
        run_dir=context.run_dir,
        control_dir=context.control_dir,
        work_dir=context.work_dir,
        role=context.role,
        invocation_id=invocation_dir.name,
        noninteractive=True,
    )
    if context.route.client == "claude-code":
        return render_claude_native_artifacts(
            launch_context, _claude_mcp_servers(mcp_payload)
        )
    if context.route.client == "codex":
        return render_codex_native_artifacts(
            launch_context,
            _codex_mcp_servers(context, mcp_payload),
        )
    if context.route.client == "vibe":
        return render_vibe_native_artifacts(
            launch_context, _vibe_mcp_servers(mcp_payload)
        )
    return {}


def _codex_mcp_servers(
    context: ExecContext,
    mcp_payload: Mapping[str, Any],
) -> dict[str, dict[str, Any]]:
    """Return only run-governed Codex servers; user servers are disabled later."""
    if context.role not in {"worker", "reviewer"}:
        return {}
    assert context.python_executable is not None
    assert context.project_dir is not None
    servers: dict[str, dict[str, Any]] = {}
    service = mcp_payload.get("memory_service")
    if isinstance(service, Mapping):
        if service.get("transport") == "http":
            servers["compchem-memory"] = {"url": service["url"]}
        else:
            args = service.get("args", ())
            if not isinstance(args, (list, tuple)):
                raise ExecutorRefused("memory stdio route args must be a list")
            entry: dict[str, Any] = {
                "command": service["command"],
                "args": [str(arg) for arg in args],
            }
            env_vars = service.get("env_vars", ())
            if not isinstance(env_vars, (list, tuple)):
                raise ExecutorRefused("memory stdio route env_vars must be a list")
            if env_vars:
                entry["env_vars"] = [str(name) for name in env_vars]
            servers["compchem-memory"] = entry
    servers["magnolia-read"] = {
        "command": context.python_executable,
        "args": ["-m", "compchem_memory.harness.read_server"],
        "cwd": str(context.project_dir.resolve()),
        "env_vars": [
            "MAGNOLIA_HOOK_POLICY",
            "MAGNOLIA_HOOK_POLICY_REQUIRED",
            "MAGNOLIA_HOOK_POLICY_SHA256",
        ],
    }
    return servers


def _vibe_mcp_servers(mcp_payload: Mapping[str, Any]) -> list[dict[str, Any]]:
    service = mcp_payload.get("memory_service")
    if not isinstance(service, Mapping):
        return []
    if service.get("transport") == "http":
        entry: dict[str, Any] = {
            "name": "compchem-memory",
            "transport": "http",
            "url": service["url"],
        }
    else:
        args = service.get("args", ())
        if not isinstance(args, (list, tuple)):
            raise ExecutorRefused("memory stdio route args must be a list")
        entry = {
            "name": "compchem-memory",
            "transport": "stdio",
            "command": service["command"],
        }
        if args:
            entry["args"] = [str(arg) for arg in args]
    return [entry]


def _claude_mcp_servers(
    mcp_payload: Mapping[str, Any],
) -> dict[str, dict[str, Any]]:
    service = mcp_payload.get("memory_service")
    if not isinstance(service, Mapping):
        return {}
    if service.get("transport") == "http":
        entry: dict[str, Any] = {"type": "http", "url": service["url"]}
        authorization = service.get("authorization_env")
        if isinstance(authorization, str):
            entry["headers"] = {
                "Authorization": f"Bearer ${{{authorization}}}"
            }
        return {"compchem-memory": entry}
    args = service.get("args", ())
    if not isinstance(args, (list, tuple)):
        raise ExecutorRefused("memory stdio route args must be a list")
    entry = {"command": service["command"], "args": [str(arg) for arg in args]}
    env_vars = service.get("env_vars", ())
    if not isinstance(env_vars, (list, tuple)):
        raise ExecutorRefused("memory stdio route env_vars must be a list")
    if env_vars:
        entry["env"] = {str(name): f"${{{name}}}" for name in env_vars}
    return {"compchem-memory": entry}


def _validate_memory_service(service: Mapping[str, Any]) -> None:
    transport = service.get("transport")
    dependencies = []
    authorization = service.get("authorization_env")
    if isinstance(authorization, str):
        dependencies.append(authorization)
    env_vars = service.get("env_vars", ())
    if isinstance(env_vars, (list, tuple)):
        dependencies.extend(str(value) for value in env_vars)
    filtered = sorted(set(dependencies) & FORBIDDEN_CLIENT_ENV)
    if filtered:
        raise ExecutorRefused(
            "memory route depends on filtered client variable(s): "
            + ", ".join(filtered)
        )
    if transport == "http":
        url = service.get("url")
        if not isinstance(url, str) or not re.match(
            r"^https?://(?:127\.0\.0\.1|localhost|\[::1\])(?::\d+)?(?:/|$)", url
        ):
            raise ExecutorRefused("memory HTTP route must be a project-bound loopback URL")
        return
    if transport == "stdio":
        if not isinstance(service.get("command"), str) or not service["command"]:
            raise ExecutorRefused("memory stdio route requires a command")
        return
    raise ExecutorRefused("memory route must use proved HTTP or stdio transport")


def _invocation_dir(context: ExecContext) -> Path:
    assert context.control_dir is not None
    control = context.control_dir.resolve()
    executors = (control / "executors").resolve()
    if not executors.is_relative_to(control):
        raise ExecutorRefused("canonical control/executors escapes control directory")
    if context.resume_from is not None:
        path = context.resume_from
        invocation = path.parent.resolve() if path.name == "launch.json" else path.resolve()
        if not invocation.is_relative_to(executors):
            raise ExecutorRefused(
                "resume invocation must be under canonical control/executors"
            )
        return invocation
    invocation = context.invocation_id or uuid.uuid4().hex
    if not invocation or invocation in {".", ".."} or Path(invocation).name != invocation:
        raise ExecutorRefused("invalid launch invocation id")
    path = (executors / invocation).resolve()
    if not path.is_relative_to(executors):
        raise ExecutorRefused("launch invocation escaped canonical control/executors")
    return path


def _effective_environment(
    context: ExecContext, policy_path: Path, policy_digest: str
) -> dict[str, str]:
    inherited = os.environ if context.environment is None else context.environment
    env = {
        str(key): str(value)
        for key, value in inherited.items()
        if key not in FILTERED_CLIENT_ENV
    }
    env.update(
        {
            str(key): str(value)
            for key, value in context.renderer_env.items()
            if key not in FILTERED_CLIENT_ENV
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
        "MAGNOLIA_HOOK_POLICY_SHA256": policy_digest,
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
    if context.route.client == "vibe":
        # VIBE_DISABLE_TELEMETRY is ignored by Vibe 3.1.2.
        explicit["VIBE_ENABLE_TELEMETRY"] = "false"
    env.update({key: str(value) for key, value in explicit.items()})
    return env


def _configuration_identity(
    context: ExecContext,
    settings_body: bytes,
    mcp_body: bytes,
    native_bodies: Mapping[str, bytes],
) -> str:
    assert context.route is not None
    assert context.project_dir is not None
    if context.route.client in {"claude-code", "codex", "vibe"}:
        bodies = [
            ("settings.json", settings_body),
            ("mcp.json", mcp_body),
            *sorted(native_bodies.items()),
        ]
        if context.route.client in CLIENT_CONFIG_FILES:
            for relative in CLIENT_CONFIG_FILES[context.route.client]:
                path = context.project_dir / relative
                try:
                    bodies.append((f"discovered:{relative}", path.read_bytes()))
                except OSError as exc:
                    raise ExecutorRefused(
                        f"missing discovered client configuration: {relative}"
                    ) from exc
        if context.route.client == "codex":
            user_config = Path(
                str(
                    context.setup.get(
                        "codex_user_config_path",
                        Path.home() / ".codex" / "config.toml",
                    )
                )
            )
            try:
                bodies.append(("discovered:user-config.toml", user_config.read_bytes()))
            except OSError as exc:
                raise ExecutorRefused(
                    "missing Codex user configuration used for trust and tool isolation"
                ) from exc
        return _digest_named_bodies(bodies)
    raise ExecutorRefused(
        f"no discovered configuration identity for {context.route.client}"
    )


def _digest_named_bodies(bodies: Any) -> str:
    digest = hashlib.sha256()
    for name, body in bodies:
        digest.update(str(name).encode("utf-8"))
        digest.update(b"\0")
        digest.update(body)
        digest.update(b"\0")
    return digest.hexdigest()


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
            path.relative_to(invocation_dir).as_posix(): _digest(body)
            for path, body in sorted(artifacts.items())
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
    expected_artifacts: Mapping[str, bytes],
) -> PreparedLaunch:
    metadata_path = invocation_dir / "launch.json"
    try:
        metadata = json.loads(metadata_path.read_text(encoding="utf-8"))
    except (OSError, ValueError, TypeError) as exc:
        raise ExecutorRefused("missing or invalid resume launch reference") from exc
    expected = {
        "trusted": True,
        "project_identity": context.project_identity,
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
    for name in (*expected_artifacts, "launch.json"):
        path = invocation_dir / name
        try:
            artifact_stat = path.lstat()
        except OSError as exc:
            raise ExecutorRefused(f"missing resume launch artifact: {name}") from exc
        if not stat.S_ISREG(artifact_stat.st_mode):
            raise ExecutorRefused(
                f"resume launch artifact is not a regular file: {name}"
            )
        try:
            body = path.read_bytes()
        except OSError as exc:
            raise ExecutorRefused(f"missing resume launch artifact: {name}") from exc
        if artifact_stat.st_mode & 0o222:
            raise ExecutorRefused(f"untrusted writable resume launch artifact: {name}")
        if name != "launch.json":
            if body != expected_artifacts[name]:
                raise ExecutorRefused(
                    f"stale resume launch artifact differs from recomputed {name}"
                )
            if recorded.get(name) != _digest(body):
                raise ExecutorRefused(f"stale resume launch artifact manifest: {name}")
        files[path] = body
    _reject_forbidden_data(context, env, files.values())
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
    invocation_dir.parent.mkdir(parents=True, exist_ok=True)
    try:
        invocation_dir.mkdir(mode=0o700)
    except FileExistsError as exc:
        raise ExecutorRefused(
            f"launch invocation already exists: {invocation_dir}"
        ) from exc
    for path, body in artifacts.items():
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(body)
        path.chmod(0o444)


def _reject_forbidden_data(
    context: ExecContext, env: Mapping[str, str], bodies: Any
) -> None:
    """Refuse filtered names or whole credential tokens in launch outputs."""
    inherited = os.environ if context.environment is None else context.environment
    values = {
        (name, str(inherited[name]))
        for name in FORBIDDEN_CLIENT_ENV
        if inherited.get(name)
    }
    destinations = [*context.launch_argv, *env.keys(), *env.values()]
    destinations.extend(body.decode("utf-8", errors="replace") for body in bodies)
    if any(_contains_token(destination, name) for destination in destinations for name in FORBIDDEN_CLIENT_ENV):
        raise ExecutorRefused("forbidden client environment name in launch contract")
    if any(
        _contains_value(destination, value, is_path=name in FORBIDDEN_PATH_ENV)
        for destination in destinations
        for name, value in values
    ):
        raise ExecutorRefused("forbidden client environment value in launch contract")


def _contains_token(destination: str, token: str) -> bool:
    return re.search(
        rf"(?<![A-Za-z0-9_]){re.escape(token)}(?![A-Za-z0-9_])", destination
    ) is not None


def _contains_value(destination: str, value: str, *, is_path: bool) -> bool:
    """Find a complete secret token without mistaking path prefixes for values."""
    punctuation = set("./-") if is_path else {
        character
        for character in value
        if not character.isalnum() and not character.isspace()
    }

    def continues_token(character: str) -> bool:
        return character.isalnum() or character == "_" or character in punctuation

    start = 0
    while (index := destination.find(value, start)) >= 0:
        end = index + len(value)
        has_left_boundary = index == 0 or not continues_token(destination[index - 1])
        has_right_boundary = end == len(destination) or not continues_token(destination[end])
        if has_left_boundary and has_right_boundary:
            return True
        start = index + 1
    return False


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
