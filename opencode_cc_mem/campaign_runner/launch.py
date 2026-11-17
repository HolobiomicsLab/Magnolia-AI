"""Prepare run-owned launch artifacts for non-OpenCode executors."""

from __future__ import annotations

import ast
import hashlib
import json
import os
import re
import shlex
import stat
import uuid
from dataclasses import dataclass, replace
from pathlib import Path
from types import MappingProxyType
from types import SimpleNamespace
from typing import Any, Mapping

from compchem_memory.harness.neutral import NeutralConfig
from compchem_memory.harness.ownership import is_magnolia_hook_command, parse_hook_command
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
from compchem_memory.harness.render_common import (
    REQUIRED_HOOK_TIMEOUT_S,
    _required_pre_tool_command,
    policy_files,
)
from compchem_memory.tool_catalog import (
    MEMORY_TOOL_ALLOWLIST_ARG,
    MEMORY_TOOL_ALLOWLIST_ENV,
    MEMORY_TOOL_ALLOWLIST_REQUIRED_ENV,
    ResolvedMemoryToolAllowlist,
    resolve_memory_tool_allowlist,
)

from .executors.contracts import ExecContext
from .executors.registry import ExecutorRefused
from .process import _resolve_executable
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
        "CODE_MODE_HOST",
        "CODEX_EXEC_SERVER_URL",
    }
)
FORBIDDEN_PATH_ENV = frozenset({"CLAUDE_CONFIG_DIR", "CODEX_HOME", "VIBE_HOME"})
FILTERED_CLIENT_ENV = FORBIDDEN_CLIENT_ENV | {"MAGNOLIA_MODEL"}
CODEX_REMOTE_ENV = frozenset({"CODE_MODE_HOST", "CODEX_EXEC_SERVER_URL"})
CLAUDE_BASE_ENV = frozenset(
    {
        "HOME",
        "USER",
        "LOGNAME",
        "SHELL",
        "TMPDIR",
        "LANG",
        "TERM",
        "PATH",
        # Only the state location the gate observed and Magnolia's hook reads; XDG_CONFIG_HOME
        # would also relocate Claude's profile store (API base URL, credentials), so it is dropped.
        "XDG_STATE_HOME",
        "CLAUDE_CODE_OAUTH_TOKEN",
        "HTTP_PROXY",
        "HTTPS_PROXY",
        "NO_PROXY",
        "http_proxy",
        "https_proxy",
        "no_proxy",
        "SSL_CERT_FILE",
        "NODE_EXTRA_CA_CERTS",
    }
)
CLAUDE_REDIRECT_ENV = frozenset(
    {
        "CLAUDE_CODE_USE_BEDROCK",
        "CLAUDE_CODE_USE_VERTEX",
        "NODE_OPTIONS",
    }
)
MEMORY_PROVIDER_ENV = frozenset(
    {
        "MAGNOLIA_LLM_API_KEY",
        "MAGNOLIA_CRITIC_API_KEY",
        "DEEPSEEK_API_KEY",
        "ANTHROPIC_API_KEY",
        "MAGNOLIA_OPENAI_API_KEY",
        "OPENAI_API_KEY",
        "KIMI_PLAN_MAGNOLIA_API_KEY",
        "KIMI_API_KEY",
        "OPENROUTER_API_KEY",
        "MAGNOLIA_MISTRAL_API_KEY",
        "MISTRAL_API_KEY",
        "ZHIPU_API_KEY",
        "ZAI_API_KEY",
    }
)
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
LAUNCH_IDENTITY_COMPONENTS = (
    "client_version",
    "client_sha256",
    "code_mode_host",
    "codex_package",
    "settings",
    "mcp",
    "native_files",
    "policy",
    "hook_command",
    "hook_interpreter",
    "hook_runner",
    "hook_source",
    "required_env",
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
    protected_env_names: frozenset[str]
    client_sha256: str | None = None
    code_mode_host_path: Path | None = None
    code_mode_host_sha256: str | None = None
    code_mode_host_version: str | None = None
    codex_package_identity: Mapping[str, Any] | None = None
    memory_service_handle: Any = None


def prepare_launch(context: ExecContext) -> PreparedLaunch:
    """Validate capability evidence and materialize one isolated launch contract."""
    evidence_context = context
    context, handle = _acquire_managed_memory_service(context)
    try:
        return _prepare_launch(context, evidence_context=evidence_context if handle else None)
    except Exception:
        if handle is not None:
            from compchem_memory.harness.memory_service import release_memory_service

            release_memory_service(handle)
        raise


def _prepare_launch(
    context: ExecContext, *, evidence_context: ExecContext | None = None
) -> PreparedLaunch:
    """Prepare after any managed service has been acquired."""
    _require_context(context)
    route = context.route
    assert route is not None
    policy = load_role(ROLE_CONFIG_NAMES[context.role])
    required = _required_capabilities(policy, context)
    evidence = _capability_evidence(context, required)
    project_dir = context.project_dir.resolve()
    discovery_dir = _launch_project_discovery(evidence, project_dir)
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
        context,
        settings_body,
        mcp_body,
        native_bodies,
        policy_body=policy_body,
        invocation_dir=invocation_dir,
    )
    protected_env_names: set[str] = set()
    env = _effective_environment(
        context,
        invocation_dir / "policy.json",
        policy_digest,
        protected_names=protected_env_names,
    )
    client_sha256 = None
    code_mode_host_path = None
    code_mode_host_sha256 = None
    code_mode_host_version = None
    codex_package_identity = None
    if "launch_identity" in evidence:
        launch_identity = _normalized_launch_identity(
            context,
            invocation_dir,
            env,
            settings_body,
            mcp_body,
            native_bodies,
            policy_body,
        )
        _verify_bound_launch_identity(evidence, launch_identity)
        context = replace(
            context, launch_argv=(str(launch_identity["executable"]),)
        )
        client_sha256 = str(launch_identity["components"]["client_sha256"])
        if route.client == "codex" and context.role in {"worker", "reviewer"}:
            executable = Path(launch_identity["executable"])
            package_root = executable.parent.parent.resolve()
            codex_package_identity = dict(
                launch_identity["components"]["codex_package"]
            )
            code_mode_host_path = _codex_package_executable_path(
                package_root, codex_package_identity, "code_mode_host"
            )
            code_mode_host_version = str(codex_package_identity["version"])
            code_mode_host_sha256 = str(
                launch_identity["components"]["code_mode_host"]["sha256"]
            )
    else:
        expected_evidence_identity = (
            configuration_identity(evidence_context)
            if evidence_context is not None
            else config_digest
        )
        if evidence["identity"] != expected_evidence_identity:
            raise ExecutorRefused(
                "capability configuration identity does not match discovered configuration"
            )
        if route.client in {"claude-code", "codex"}:
            try:
                executable = _resolved_client_executable(context, env)
            except ExecutorRefused:
                executable = None
            if executable is not None:
                if route.client == "codex":
                    _installed_client_version(
                        executable,
                        invocation_dir=invocation_dir,
                        launch_env=env,
                    )
                client_sha256 = _file_digest(executable)
                if route.client == "codex" and context.role in {"worker", "reviewer"}:
                    package_root, codex_package_identity = _resolved_codex_package(
                        executable
                    )
                    code_mode_host_path = _codex_package_executable_path(
                        package_root, codex_package_identity, "code_mode_host"
                    )
                    code_mode_host_version = str(codex_package_identity["version"])
                    code_mode_host_sha256 = _file_digest(code_mode_host_path)
                context = replace(context, launch_argv=(str(executable),))
    if context.resume_from is not None:
        return _resume_launch(
            context,
            discovery_dir,
            invocation_dir,
            env,
            policy_digest,
            config_digest,
            capability_identity,
            client_sha256,
            code_mode_host_path,
            code_mode_host_sha256,
            code_mode_host_version,
            codex_package_identity,
            frozenset(protected_env_names),
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
        client_sha256,
        code_mode_host_path,
        code_mode_host_sha256,
        code_mode_host_version,
        codex_package_identity,
        frozenset(protected_env_names),
    )


def configuration_identity(context: ExecContext) -> str:
    """Return the digest of configuration this client launch will discover."""
    _require_context(context)
    if context.route is None:
        raise ExecutorRefused("configuration identity requires a resolved route")
    if context.memory_service.get("transport") == "managed":
        context = replace(
            context,
            memory_service={
                "transport": "http",
                "url": "http://127.0.0.1:0/mcp",
                "authorization_env": "MAGNOLIA_MEMORY_SERVICE_TOKEN",
            },
        )
    policy = load_role(ROLE_CONFIG_NAMES[context.role])
    required = _required_capabilities(policy, context)
    settings = _json_bytes(_settings_payload(context, policy, required))
    mcp_payload = _mcp_payload(context, policy)
    mcp = _json_bytes(mcp_payload)
    invocation_name = context.invocation_id or "configuration-identity"
    invocation_dir = _invocation_dir(replace(context, invocation_id=invocation_name))
    native = _client_native_artifacts(context, invocation_dir, mcp_payload)
    read_roots = _canonical_read_roots(policy, context, required)
    edit_roots = _canonical_edit_roots(policy, context)
    policy_payload = _policy_payload(
        context,
        policy,
        required,
        read_roots,
        edit_roots,
        "{required_capability_identity}",
    )
    policy_body = _policy_body(context, invocation_dir, policy_payload)
    return _configuration_identity(
        context,
        settings,
        mcp,
        native,
        policy_body=policy_body,
        invocation_dir=invocation_dir,
    )


def _acquire_managed_memory_service(
    context: ExecContext,
) -> tuple[ExecContext, Any | None]:
    """Acquire a per-project service for any role that uses memory tools."""
    if (
        context.route is None
        or context.memory_service.get("transport") != "managed"
    ):
        return context, None
    policy = load_role(ROLE_CONFIG_NAMES[context.role])
    tools = _resolved_memory_tools(context, policy)
    if not tools.neutral_names:
        return context, None
    if context.project_dir is None or not context.python_executable:
        raise ExecutorRefused("managed memory requires a project and Python")
    from compchem_memory.harness.memory_service import ensure_memory_service

    handle = ensure_memory_service(
        SimpleNamespace(
            project_dir=context.project_dir,
            python=context.python_executable,
            setup=dict(context.setup),
            environment={
                **os.environ,
                **({} if context.environment is None else dict(context.environment)),
            },
            holder_pid=os.getpid(),
        ),
        tool_allowlist=tools.neutral_names,
    )
    route = {**handle.route(), "_handle": handle}
    return replace(context, memory_service=route), handle


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
    if context.route.client == "claude-code":
        _refuse_claude_project_settings(context)


def _refuse_claude_project_settings(context: ExecContext) -> None:
    """Bind the absence of settings loaded from Claude's project source."""
    roots = (context.project_dir, context.run_dir, context.work_dir)
    for root in roots:
        assert root is not None
        for name in ("settings.json", "settings.local.json"):
            candidate = root / ".claude" / name
            if candidate.exists() or candidate.is_symlink():
                raise ExecutorRefused(
                    f"Claude project settings are not capability-bound: {name}"
                )


def _is_cancelled(cancellation: Any) -> bool:
    if cancellation is None:
        return False
    is_set = getattr(cancellation, "is_set", None)
    return bool(is_set()) if callable(is_set) else bool(cancellation)


def _required_capabilities(
    policy: RolePolicy, context: ExecContext
) -> tuple[str, ...]:
    requested = tuple(dict.fromkeys(context.required_capabilities))
    memory_tools = _resolved_memory_tools(context, policy).neutral_names
    allowed = set(policy.allowed_native_capabilities) | set(memory_tools)
    unsupported = sorted(set(requested) - allowed)
    if unsupported:
        raise ExecutorRefused("unsupported required capabilities: " + ", ".join(unsupported))
    if context.role in {"worker", "reviewer"}:
        baseline = (*policy.allowed_native_capabilities, *memory_tools)
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


def _launch_project_discovery(
    evidence: Mapping[str, Any], project_dir: Path
) -> Path:
    """Resolve scope evidence to this launch's canonical project directory."""
    discovery = evidence.get("discovery_dir")
    if not isinstance(discovery, Mapping) or set(discovery) - {
        "scope",
        "configuration",
    }:
        raise ExecutorRefused(
            "capability discovery evidence must use launch-project scope"
        )
    if discovery.get("scope") != "launch-project":
        raise ExecutorRefused(
            "capability discovery evidence must use launch-project scope"
        )
    return project_dir.resolve()


def _normalized_launch_identity(
    context: ExecContext,
    invocation_dir: Path,
    launch_env: Mapping[str, str],
    settings_body: bytes,
    mcp_body: bytes,
    native_bodies: Mapping[str, bytes],
    policy_body: bytes,
) -> dict[str, Any]:
    """Bind executable bytes and path-independent rendered launch components."""
    executable = _resolved_client_executable(context, launch_env)
    replacements = list(_identity_replacements(context, invocation_dir))
    if context.memory_service.get("transport") == "http":
        service_url = context.memory_service.get("url")
        if isinstance(service_url, str) and service_url:
            replacements.append((service_url, "{memory_service_url}"))
    normalized_replacements = tuple(
        sorted(set(replacements), key=lambda item: len(item[0]), reverse=True)
    )
    normalized_native = {
        name: _normalize_identity_body(body, normalized_replacements)
        for name, body in native_bodies.items()
    }
    client_sha256 = _file_digest(executable)
    components = {
        "client_sha256": client_sha256,
        "client_version": _installed_client_version(
            executable,
            invocation_dir=invocation_dir,
            launch_env=launch_env,
        ),
        "settings": _settings_rule_digest(
            context, settings_body, normalized_replacements
        ),
        "mcp": _digest(
            _normalize_identity_body(mcp_body, normalized_replacements)
        ),
        "native_files": _digest_named_bodies(sorted(normalized_native.items())),
        "policy": _digest(
            _normalize_identity_body(policy_body, normalized_replacements)
        ),
        "hook_command": _hook_commands_digest(
            native_bodies, normalized_replacements
        ),
        "hook_interpreter": _hook_interpreter_digest(context, launch_env),
        "hook_runner": _hook_runner_digest(context),
        "hook_source": _hook_source_digest(context),
        "required_env": _digest(
            _canonical_bytes(_required_policy_env_names(launch_env))
        ),
    }
    if context.route is not None and context.route.client == "codex":
        components["model"] = context.route.model
        if context.role in {"worker", "reviewer"}:
            package_root, package_identity = _resolved_codex_package(executable)
            host_path = _codex_package_executable_path(
                package_root, package_identity, "code_mode_host"
            )
            components["code_mode_host"] = {
                "version": package_identity["version"],
                "sha256": _file_digest(host_path),
            }
            components["codex_package"] = package_identity
    return {
        "schema_version": "magnolia-normalized-launch-identity-v1",
        "executable": str(executable),
        "components": components,
    }


def _verify_bound_launch_identity(
    evidence: Mapping[str, Any], actual: Mapping[str, Any]
) -> None:
    """Refuse the first named normalized component that differs from evidence."""
    bound = evidence.get("launch_identity")
    if bound is None:
        return
    if not isinstance(bound, Mapping) or bound.get("schema_version") != actual.get(
        "schema_version"
    ):
        raise ExecutorRefused("capability launch identity mismatch: schema_version")
    expected = bound.get("components")
    observed = actual.get("components")
    if not isinstance(expected, Mapping) or not isinstance(observed, Mapping):
        raise ExecutorRefused("capability launch identity mismatch: components")
    names = (*LAUNCH_IDENTITY_COMPONENTS, *(("model",) if "model" in observed else ()))
    for name in names:
        if expected.get(name) != observed.get(name):
            raise ExecutorRefused(f"capability launch identity mismatch: {name}")


def _resolved_client_executable(
    context: ExecContext, launch_env: Mapping[str, str]
) -> Path:
    assert context.project_dir is not None
    try:
        resolved = _resolve_executable(
            context.launch_argv[0], context.project_dir.resolve(), launch_env
        )
        executable = Path(resolved).resolve(strict=True)
    except (OSError, RuntimeError) as exc:
        raise ExecutorRefused("launched client executable is unavailable") from exc
    if not executable.is_file():
        raise ExecutorRefused("launched client executable is not a regular file")
    return executable


def _resolved_codex_package(executable: Path) -> tuple[Path, dict[str, Any]]:
    """Resolve and hash every executable exposed by a standalone Codex package."""
    try:
        executable = executable.resolve(strict=True)
        package_root = executable.parent.parent.resolve(strict=True)
        manifest_path = package_root / "codex-package.json"
        manifest_realpath = manifest_path.resolve(strict=True)
        _require_codex_package_path(manifest_realpath, package_root)
        manifest_body = manifest_realpath.read_bytes()
        manifest = json.loads(manifest_body)
    except (OSError, RuntimeError, ValueError, TypeError) as exc:
        raise ExecutorRefused("Codex package manifest is unavailable") from exc
    layout_version = manifest.get("layoutVersion")
    version = manifest.get("version")
    target = manifest.get("target")
    if (
        layout_version != 1
        or not isinstance(version, str)
        or not version
        or not isinstance(target, str)
        or not target
    ):
        raise ExecutorRefused("Codex package manifest metadata is invalid")
    executables = {
        "entrypoint": _codex_package_executable(
            package_root, manifest.get("entrypoint"), "entrypoint"
        ),
        "code_mode_host": _codex_package_executable(
            package_root,
            executable.with_name("codex-code-mode-host").relative_to(package_root),
            "code_mode_host",
        ),
    }
    if _codex_package_executable_path(
        package_root, {"executables": executables}, "entrypoint"
    ) != executable:
        raise ExecutorRefused("Codex entrypoint does not match its package manifest")
    if "execServer" in manifest:
        executables["execServer"] = _codex_package_executable(
            package_root, manifest["execServer"], "execServer"
        )
    if "pathDir" in manifest:
        executables.update(
            _codex_path_executables(package_root, manifest["pathDir"])
        )
    return package_root, {
        "layout_version": layout_version,
        "version": version,
        "target": target,
        "manifest_sha256": _digest(manifest_body),
        "executables": dict(sorted(executables.items())),
    }


def _codex_package_executable(
    package_root: Path, relative: Any, component: str
) -> dict[str, str]:
    """Bind one executable after proving its realpath stays in the package."""
    if not isinstance(relative, (str, os.PathLike)):
        raise ExecutorRefused(f"Codex package executable is invalid: {component}")
    relative_path = Path(relative)
    if relative_path.is_absolute():
        raise ExecutorRefused(
            f"Codex package executable is outside its package: {component}"
        )
    try:
        realpath = (package_root / relative_path).resolve(strict=True)
    except (OSError, RuntimeError) as exc:
        raise ExecutorRefused(
            f"Codex package executable is unavailable: {component}"
        ) from exc
    _require_codex_package_path(realpath, package_root)
    if not realpath.is_file() or not os.access(realpath, os.X_OK):
        raise ExecutorRefused(f"Codex package executable is invalid: {component}")
    return {
        "path": realpath.relative_to(package_root).as_posix(),
        "sha256": _file_digest(realpath),
    }


def _codex_path_executables(
    package_root: Path, relative: Any
) -> dict[str, dict[str, str]]:
    """Bind each executable the package adds directly to the client PATH."""
    if not isinstance(relative, (str, os.PathLike)) or Path(relative).is_absolute():
        raise ExecutorRefused("Codex pathDir is outside its package")
    try:
        path_dir = (package_root / Path(relative)).resolve(strict=True)
    except (OSError, RuntimeError) as exc:
        raise ExecutorRefused("Codex pathDir is unavailable") from exc
    _require_codex_package_path(path_dir, package_root)
    if not path_dir.is_dir():
        raise ExecutorRefused("Codex pathDir is invalid")
    bound = {}
    for candidate in sorted(path_dir.iterdir(), key=lambda path: path.name):
        try:
            realpath = candidate.resolve(strict=True)
        except (OSError, RuntimeError) as exc:
            raise ExecutorRefused("Codex pathDir entry is unavailable") from exc
        if not realpath.is_file() or not os.access(realpath, os.X_OK):
            continue
        component = f"pathDir/{candidate.name}"
        bound[component] = _codex_package_executable(
            package_root, candidate.relative_to(package_root), component
        )
    return bound


def _require_codex_package_path(path: Path, package_root: Path) -> None:
    if not path.is_relative_to(package_root):
        raise ExecutorRefused("Codex package executable is outside its package")


def _codex_package_executable_path(
    package_root: Path, identity: Mapping[str, Any], component: str
) -> Path:
    try:
        relative = identity["executables"][component]["path"]
        path = (package_root / relative).resolve(strict=True)
    except (KeyError, OSError, RuntimeError, TypeError) as exc:
        raise ExecutorRefused(
            f"Codex package executable is unavailable: {component}"
        ) from exc
    _require_codex_package_path(path, package_root)
    return path


def _resolved_codex_code_mode_host(executable: Path) -> tuple[Path, str | None]:
    """Resolve the code-mode host from the complete package identity."""
    package_root, identity = _resolved_codex_package(executable)
    return (
        _codex_package_executable_path(package_root, identity, "code_mode_host"),
        str(identity["version"]),
    )


def _installed_client_version(
    executable: Path,
    *,
    invocation_dir: Path,
    launch_env: Mapping[str, str],
) -> str:
    """Read client metadata with any Codex writes confined to the run."""
    package_root = executable.parent.parent
    codex_manifest = package_root / "codex-package.json"
    try:
        codex_metadata = json.loads(codex_manifest.read_text(encoding="utf-8"))
    except (OSError, ValueError, TypeError):
        codex_metadata = {}
    codex_version = codex_metadata.get("version")
    if (
        codex_metadata.get("layoutVersion") == 1
        and isinstance(codex_version, str)
        and codex_version
    ):
        return codex_version
    package = package_root / "package.json"
    try:
        metadata = json.loads(package.read_text(encoding="utf-8"))
    except (OSError, ValueError, TypeError):
        metadata = {}
    version = metadata.get("version")
    if metadata.get("name") == "@anthropic-ai/claude-code" and isinstance(
        version, str
    ) and version:
        return version
    raise ExecutorRefused("launched client version metadata is unavailable")


def _settings_rule_digest(
    context: ExecContext,
    settings_body: bytes,
    replacements: tuple[tuple[str, str], ...],
) -> str:
    """Bind the cap computation rule while allowing configured cap values."""
    try:
        settings = json.loads(settings_body)
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ExecutorRefused("launch settings are invalid") from exc
    policy = load_role(ROLE_CONFIG_NAMES[context.role])
    role_cap = context.role_cap if context.role_cap is not None else policy.default_cap
    governor_cap = context.governor_cap or DEFAULT_GOVERNOR_CAP
    expected = min(role_cap, governor_cap)
    observed = settings.get("max_turns")
    if isinstance(observed, bool) or not isinstance(observed, int) or observed < 1:
        raise ExecutorRefused("launch settings max-turns rule requires a positive integer")
    if observed != expected:
        raise ExecutorRefused("launch settings max-turns rule differs from min(role, governor)")
    settings["max_turns"] = {
        "rule": "min(role_cap,governor_cap)",
        "positive": True,
    }
    return _digest(
        _normalize_identity_body(_canonical_bytes(settings), replacements)
    )


def _required_policy_env_names(launch_env: Mapping[str, str]) -> list[str]:
    """Measure required policy names from the actual child environment."""
    return sorted(
        name for name in launch_env if name.startswith("MAGNOLIA_HOOK_POLICY")
    )


def _hook_interpreter_digest(
    context: ExecContext, launch_env: Mapping[str, str]
) -> str:
    """Bind an exact explicit interpreter or the repository's configured venv."""
    selected = launch_env.get("MAGNOLIA_PYTHON")
    if not selected or not context.python_executable:
        raise ExecutorRefused("hook interpreter is missing Magnolia's selection")
    requested = Path(context.python_executable)
    if not requested.is_absolute() or selected != context.python_executable:
        raise ExecutorRefused("hook interpreter differs from MAGNOLIA_PYTHON")
    repository_venv = context.installation_root.parent / ".venv"
    inherited = os.environ if context.environment is None else context.environment
    if context.python_executable != inherited.get("MAGNOLIA_PYTHON"):
        if context.python_executable != str(repository_venv / "bin" / "python3"):
            raise ExecutorRefused("hook interpreter is neither MAGNOLIA_PYTHON nor repository venv")
        if not (repository_venv / "pyvenv.cfg").is_file():
            raise ExecutorRefused("hook interpreter repository venv lacks pyvenv.cfg")
    if not requested.is_file() or not os.access(requested, os.X_OK):
        raise ExecutorRefused("hook interpreter is unavailable or not executable")
    rule = {
        "resolver": "MAGNOLIA_PYTHON-or-repository-venv-v3",
        "comparison": "lexical-absolute-path",
        "override": "absolute-existing-executable-as-given",
        "repository_python": "repository/.venv/bin/python3",
        "pyvenv_cfg": "repository/.venv/pyvenv.cfg",
        "bare_system_python": "explicit-override-only",
    }
    return _digest(_canonical_bytes(rule))


def resolve_python(
    installation_root: Path, environment: Mapping[str, str] | None = None
) -> str:
    """Select the override or repository venv without resolving interpreter symlinks."""
    inherited = os.environ if environment is None else environment
    return inherited.get("MAGNOLIA_PYTHON") or str(
        installation_root.parent / ".venv" / "bin" / "python3"
    )


def _hook_runner_path(context: ExecContext) -> Path:
    runner = (
        context.installation_root
        / "mcp-servers"
        / "compchem-memory"
        / "src"
        / "compchem_memory"
        / "hook"
        / "runner.py"
    )
    lexical = Path(os.path.abspath(runner))
    try:
        resolved = runner.resolve(strict=True)
    except OSError as exc:
        raise ExecutorRefused("hook_runner is unavailable") from exc
    if lexical != resolved or not runner.is_file():
        raise ExecutorRefused(
            "hook_runner must be a regular file without symlinked ancestors"
        )
    return runner


def _hook_runner_digest(context: ExecContext) -> str:
    """Validate and bind the repository runner path contract."""
    _hook_runner_path(context)
    return _digest(_canonical_bytes({
        "schema_version": "magnolia-hook-runner-v1",
        "relative_path": (
            "mcp-servers/compchem-memory/src/compchem_memory/hook/runner.py"
        ),
        "path_rule": "regular-file-without-symlinked-ancestors",
    }))


def _hook_source_digest(context: ExecContext) -> str:
    """Hash hook sources and the required-mode wrapper that executes them."""
    source_root = (
        context.installation_root
        / "mcp-servers"
        / "compchem-memory"
        / "src"
        / "compchem_memory"
    )
    hook_root = source_root / "hook"
    _hook_runner_path(context)
    bound_server_sources = (
        source_root / "harness" / "render_common.py",
        source_root / "memory_http.py",
        source_root / "tool_catalog.py",
        source_root / "writer_lease.py",
    )
    bodies: list[tuple[str, bytes]] = []
    try:
        for path in sorted(hook_root.rglob("*")):
            relative = path.relative_to(source_root)
            if "__pycache__" in relative.parts or not path.is_file():
                continue
            bodies.append((relative.as_posix(), path.read_bytes()))
        wrapper_source = bound_server_sources[0].read_text(encoding="utf-8")
        for path in bound_server_sources[1:]:
            bodies.append((path.relative_to(source_root).as_posix(), path.read_bytes()))
    except OSError as exc:
        raise ExecutorRefused("hook source binding is unavailable") from exc
    if not bodies:
        raise ExecutorRefused("hook source binding found no hook package")
    tree = ast.parse(wrapper_source)
    selected = []
    for node in tree.body:
        names = {
            target.id
            for target in getattr(node, "targets", ())
            if isinstance(target, ast.Name)
        }
        name = getattr(node, "name", None)
        if (
            names & {"_REQUIRED_HOOK_SCRIPT", "REQUIRED_HOOK_TIMEOUT_S"}
            or name == "_required_pre_tool_command"
        ):
            segment = ast.get_source_segment(wrapper_source, node)
            if segment:
                selected.append(segment)
    if len(selected) != 3:
        raise ExecutorRefused("required-mode hook wrapper binding is incomplete")
    bodies.append(
        (
            "harness/render_common.py::required-wrapper",
            "\n".join(selected).encode(),
        )
    )
    return _digest_named_bodies(bodies)


def _file_digest(path: Path) -> str:
    digest = hashlib.sha256()
    try:
        with path.open("rb") as stream:
            for chunk in iter(lambda: stream.read(1024 * 1024), b""):
                digest.update(chunk)
    except OSError as exc:
        raise ExecutorRefused("launched client executable cannot be hashed") from exc
    return digest.hexdigest()


def _identity_replacements(
    context: ExecContext, invocation_dir: Path
) -> tuple[tuple[str, str], ...]:
    named_paths = {
        "{invocation_dir}": invocation_dir,
        "{project_dir}": context.project_dir,
        "{run_dir}": context.run_dir,
        "{control_dir}": context.control_dir,
        "{work_dir}": context.work_dir,
        "{installation_root}": context.installation_root,
        "{workspace_root}": context.workspace_root,
    }
    replacements = [
        (str(spelling), name)
        for name, path in named_paths.items()
        if path is not None
        for spelling in (path, path.resolve())
    ]
    if context.python_executable:
        replacements.append((context.python_executable, "{python_executable}"))
    if context.project_identity:
        replacements.append((context.project_identity, "{project_identity}"))
    return tuple(sorted(set(replacements), key=lambda item: len(item[0]), reverse=True))


def _normalize_identity_body(
    body: bytes, replacements: tuple[tuple[str, str], ...]
) -> bytes:
    try:
        value = json.loads(body)
    except (UnicodeDecodeError, json.JSONDecodeError):
        return _replace_identity_text(body.decode("utf-8"), replacements).encode()
    return _canonical_bytes(_normalize_identity_value(value, replacements))


def _normalize_identity_value(
    value: Any,
    replacements: tuple[tuple[str, str], ...],
    *,
    key: str | None = None,
) -> Any:
    if key == "required_capability_identity":
        return "{required_capability_identity}"
    if isinstance(value, Mapping):
        return {
            str(name): _normalize_identity_value(item, replacements, key=str(name))
            for name, item in value.items()
        }
    if isinstance(value, list):
        return [_normalize_identity_value(item, replacements) for item in value]
    if isinstance(value, str):
        python_executable = next(
            (path for path, name in replacements if name == "{python_executable}"), None
        )
        if key == "command" and (parsed := parse_hook_command(
            value, python_executable=python_executable
        )) is not None:
            tokens, wrapped = parsed
            command = shlex.join([_replace_identity_text(token, replacements) for token in tokens])
            if wrapped:
                return _required_pre_tool_command(command, tokens[-2], REQUIRED_HOOK_TIMEOUT_S)
            return command
        return _replace_identity_text(value, replacements)
    return value


def _replace_identity_text(
    value: str, replacements: tuple[tuple[str, str], ...]
) -> str:
    for concrete, placeholder in replacements:
        value = value.replace(concrete, placeholder)
    return value


def _hook_commands_digest(
    native_bodies: Mapping[str, bytes],
    replacements: tuple[tuple[str, str], ...],
) -> str:
    commands: list[str] = []
    for body in native_bodies.values():
        try:
            value = _normalize_identity_value(json.loads(body), replacements)
        except (UnicodeDecodeError, json.JSONDecodeError):
            continue
        _collect_hook_commands(value, commands)
    return _digest(_canonical_bytes(sorted(commands)))


def _collect_hook_commands(value: Any, commands: list[str]) -> None:
    if isinstance(value, Mapping):
        command = value.get("command")
        if is_magnolia_hook_command(command):
            commands.append(command)
        for item in value.values():
            _collect_hook_commands(item, commands)
    elif isinstance(value, list):
        for item in value:
            _collect_hook_commands(item, commands)


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
    mcp_tools = list(_resolved_memory_tools(context, policy).neutral_names)
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
    resolved_tools = _resolved_memory_tools(context, policy)
    service = None
    if resolved_tools.neutral_names:
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
        _validate_memory_service(context, service)
    tool_allowlist = list(resolved_tools.neutral_names)
    if context.route is not None and (
        context.route.client == "codex" and context.role in {"worker", "reviewer"}
    ):
        tool_allowlist.extend(CODEX_READ_SERVER_TOOLS)
    return {
        "schema_version": "magnolia-launch-mcp-v1",
        "memory_service": service,
        "tool_allowlist": list(dict.fromkeys(tool_allowlist)),
    }


def _resolved_memory_tools(
    context: ExecContext, policy: RolePolicy
) -> ResolvedMemoryToolAllowlist:
    """Resolve one role list, applying the explicit Vibe-memory gate."""
    resolved = resolve_memory_tool_allowlist(
        policy.mcp_tool_allowlist,
        required=policy.role == "campaign-worker",
    )
    if context.route is None or context.route.client != "vibe":
        return resolved
    evidence = context.role_capabilities.get("vibe", {}).get(context.role, {})
    if isinstance(evidence, Mapping) and evidence.get("vibe_memory") is True:
        return resolved
    return resolve_memory_tool_allowlist((), required=False)


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
            entry = {"url": service["url"]}
            authorization = service.get("authorization_env")
            if isinstance(authorization, str):
                entry["bearer_token_env_var"] = authorization
        else:
            args = _stdio_memory_args(service, mcp_payload)
            entry: dict[str, Any] = {
                "command": service["command"],
                "args": args,
            }
            env_vars = service.get("env_vars", ())
            if not isinstance(env_vars, (list, tuple)):
                raise ExecutorRefused("memory stdio route env_vars must be a list")
            entry["env_vars"] = list(dict.fromkeys(
                [*(str(name) for name in env_vars), "PYTHONPYCACHEPREFIX"]
            ))
        entry["tools"] = _codex_tool_approvals(
            "compchem-memory", mcp_payload
        )
        servers["compchem-memory"] = entry
    servers["magnolia-read"] = {
        "command": context.python_executable,
        "args": ["-I", str(_hook_runner_path(context)), "--read-server"],
        "cwd": str(context.project_dir.resolve()),
        "env_vars": [
            "MAGNOLIA_HOOK_POLICY",
            "MAGNOLIA_HOOK_POLICY_REQUIRED",
            "MAGNOLIA_HOOK_POLICY_SHA256",
            "MAGNOLIA_CONTROL_DIR",
            "MAGNOLIA_PROJECT_DIR",
            "PYTHONPYCACHEPREFIX",
        ],
        "tools": _codex_tool_approvals("magnolia-read", mcp_payload),
    }
    return servers


def _codex_tool_approvals(
    server: str, mcp_payload: Mapping[str, Any]
) -> dict[str, dict[str, str]]:
    """Approve exactly the role tools represented by one governed server."""
    allowlist = mcp_payload.get("tool_allowlist", ())
    if not isinstance(allowlist, (list, tuple)):
        raise ExecutorRefused("Codex MCP tool allowlist must be a list")
    prefix = f"mcp:{server}."
    return {
        str(tool).removeprefix(prefix): {"approval_mode": "approve"}
        for tool in allowlist
        if isinstance(tool, str) and tool.startswith(prefix)
    }


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
        authorization = service.get("authorization_env")
        if isinstance(authorization, str):
            entry["auth"] = {
                "type": "static",
                "api_key_env": authorization,
            }
    else:
        args = _stdio_memory_args(service, mcp_payload)
        entry = {
            "name": "compchem-memory",
            "transport": "stdio",
            "command": service["command"],
        }
        if args:
            entry["args"] = args
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
    args = _stdio_memory_args(service, mcp_payload)
    entry = {"command": service["command"], "args": args}
    env_vars = service.get("env_vars", ())
    if not isinstance(env_vars, (list, tuple)):
        raise ExecutorRefused("memory stdio route env_vars must be a list")
    if env_vars:
        entry["env"] = {str(name): f"${{{name}}}" for name in env_vars}
    return {"compchem-memory": entry}


def _stdio_memory_args(
    service: Mapping[str, Any], mcp_payload: Mapping[str, Any]
) -> list[str]:
    """Bind the exact server allowlist in the stdio process launch contract."""
    raw_arguments = service.get("args", ())
    if not isinstance(raw_arguments, (list, tuple)):
        raise ExecutorRefused("memory stdio route args must be a list")
    arguments = [str(argument) for argument in raw_arguments]
    allowlist = mcp_payload.get("tool_allowlist", ())
    if not isinstance(allowlist, (list, tuple)):
        raise ExecutorRefused("memory stdio tool_allowlist must be a list")
    memory_tools = [
        str(tool)
        for tool in allowlist
        if isinstance(tool, str) and tool.startswith("mcp:compchem-memory.")
    ]
    if memory_tools:
        arguments.extend(
            [MEMORY_TOOL_ALLOWLIST_ARG, json.dumps(memory_tools, separators=(",", ":"))]
        )
    return arguments


def _validate_memory_service(context: ExecContext, service: Mapping[str, Any]) -> None:
    transport = service.get("transport")
    dependencies = []
    authorization = service.get("authorization_env")
    if isinstance(authorization, str):
        dependencies.append(authorization)
    env_vars = service.get("env_vars", ())
    if isinstance(env_vars, (list, tuple)):
        dependencies.extend(str(value) for value in env_vars)
        provider_keys = sorted(set(str(value) for value in env_vars) & MEMORY_PROVIDER_ENV)
        if provider_keys:
            raise ExecutorRefused(
                "filtered provider credential cannot enter the client environment: "
                + ", ".join(provider_keys)
            )
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
        if not isinstance(authorization, str) or not authorization:
            raise ExecutorRefused("memory HTTP route must be authenticated")
        return
    if transport == "stdio":
        if not isinstance(service.get("command"), str) or not service["command"]:
            raise ExecutorRefused("memory stdio route requires a command")
        args = service.get("args")
        runner = Path(str(args[1])) if isinstance(args, (list, tuple)) and len(args) == 3 else None
        if (
            runner is None
            or args[0] != "-I"
            or args[2] != "--memory-server"
            or not runner.is_absolute()
            or runner != _hook_runner_path(context)
        ):
            raise ExecutorRefused(
                "memory stdio route requires the isolated repository runner"
            )
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
    context: ExecContext,
    policy_path: Path,
    policy_digest: str,
    *,
    protected_names: set[str] | None = None,
) -> dict[str, str]:
    inherited = os.environ if context.environment is None else context.environment
    assert context.route is not None
    if context.route.client == "codex":
        explicit_selectors = sorted(
            str(name)
            for source in (
                context.environment if context.environment is not None else {},
                context.renderer_env,
            )
            for name in source
            if str(name).upper() in CODEX_REMOTE_ENV
            or "CODE_MODE_HOST" in str(name).upper()
        )
        if explicit_selectors:
            raise ExecutorRefused(
                "Codex launch environment has a remote host/server selector: "
                + ", ".join(explicit_selectors)
            )
    if context.route.client == "claude-code":
        redirecting = sorted(
            {
                name
                for name in (*inherited, *context.renderer_env)
                if name in CLAUDE_REDIRECT_ENV
                or name.startswith("ANTHROPIC_")
                or (
                    name.startswith("CLAUDE_CODE_")
                    and name != "CLAUDE_CODE_OAUTH_TOKEN"
                )
            }
        )
        if redirecting:
            raise ExecutorRefused(
                "Claude client environment changes endpoint/runtime: "
                + ", ".join(redirecting)
            )
        state_home = {**inherited, **context.renderer_env}.get("XDG_STATE_HOME")
        if state_home is not None and not os.path.isabs(str(state_home)):
            raise ExecutorRefused(
                "Claude client environment has a relative XDG_STATE_HOME; use an absolute path"
            )
        service_names = context.memory_service.get("env_vars", ())
        if not isinstance(service_names, (list, tuple)):
            service_names = ()
        inherited_allowlist = CLAUDE_BASE_ENV | frozenset(
            str(name) for name in service_names if str(name).startswith("MAGNOLIA_")
        )
        env = {
            str(key): str(value)
            for key, value in inherited.items()
            if key in inherited_allowlist
        }
        env.update(
            {
                str(key): str(value)
                for key, value in context.renderer_env.items()
                if key in inherited_allowlist and key != "MAGNOLIA_MODEL"
            }
        )
    else:
        env = {
            str(key): str(value)
            for key, value in inherited.items()
            if key not in FILTERED_CLIENT_ENV
            and not (
                context.route.client == "codex"
                and (
                    str(key).upper() in CODEX_REMOTE_ENV
                    or "CODE_MODE_HOST" in str(key).upper()
                )
            )
        }
        env.update(
            {
                str(key): str(value)
                for key, value in context.renderer_env.items()
                if key not in FILTERED_CLIENT_ENV
                and not (
                    context.route.client == "codex"
                    and (
                        str(key).upper() in CODEX_REMOTE_ENV
                        or "CODE_MODE_HOST" in str(key).upper()
                    )
                )
            }
        )
    if context.memory_service.get("transport") == "http":
        for name in MEMORY_PROVIDER_ENV:
            env.pop(name, None)
    if protected_names is not None:
        protected_names.update(
            str(name) for name in context.renderer_env if str(name) in env
        )
        service_names = context.memory_service.get("env_vars", ())
        if isinstance(service_names, (list, tuple)):
            protected_names.update(
                str(name) for name in service_names if str(name) in env
            )
    assert context.installation_root is not None
    assert context.workspace_root is not None
    assert context.run_dir is not None
    assert context.control_dir is not None
    assert context.work_dir is not None
    explicit = _explicit_environment(
        context,
        policy_path=policy_path,
        policy_digest=policy_digest,
        pycache_prefix=_fresh_pycache_prefix(policy_path.parent),
        inherited=env,
    )
    if protected_names is not None:
        protected_names.update(explicit)
    env.update({key: str(value) for key, value in explicit.items()})
    return env


def _explicit_environment(
    context: ExecContext,
    *,
    policy_path: Path,
    policy_digest: str,
    pycache_prefix: str,
    inherited: Mapping[str, str],
) -> dict[str, str]:
    """Build every unconditional and conditional child-owned value."""
    explicit = _base_explicit_environment(
        context,
        policy_path=policy_path,
        policy_digest=policy_digest,
        pycache_prefix=pycache_prefix,
    )
    memory_model = context.setup.get("memory_model")
    memory_provider = context.setup.get("memory_provider")
    if isinstance(memory_model, str) and memory_model:
        explicit["MAGNOLIA_MEMORY_MODEL"] = memory_model
    if isinstance(memory_provider, str) and memory_provider:
        explicit["MAGNOLIA_MEMORY_PROVIDER"] = memory_provider
    for setup_key, env_name in (
        ("critic_provider", "MAGNOLIA_CRITIC_PROVIDER"),
        ("critic_model", "MAGNOLIA_CRITIC_MODEL"),
        ("direct_fallback_provider", "MAGNOLIA_DIRECT_FALLBACK_PROVIDER"),
        ("direct_fallback_model", "MAGNOLIA_DIRECT_FALLBACK_MODEL"),
    ):
        value = context.setup.get(setup_key)
        if isinstance(value, str) and value:
            explicit[env_name] = value
    if context.route.client == "claude-code" and context.memory_service.get("transport") == "stdio":
        for name in ("MAGNOLIA_MEMORY_MODEL", "MAGNOLIA_MEMORY_PROVIDER"):
            explicit.setdefault(name, inherited.get(name, ""))
    if context.route.model:
        explicit["MAGNOLIA_MODEL"] = context.route.model
    policy = load_role(ROLE_CONFIG_NAMES[context.role])
    memory_tools = _resolved_memory_tools(context, policy)
    if memory_tools.neutral_names:
        explicit[MEMORY_TOOL_ALLOWLIST_ENV] = json.dumps(
            memory_tools.neutral_names, separators=(",", ":")
        )
        explicit[MEMORY_TOOL_ALLOWLIST_REQUIRED_ENV] = "1"
    service = context.memory_service
    authorization = service.get("authorization_env")
    token_file = service.get("token_file")
    if isinstance(authorization, str) and isinstance(token_file, str):
        try:
            token_path = Path(token_file).resolve()
            if token_path.stat().st_mode & 0o777 != 0o600:
                raise ExecutorRefused("memory service bearer token file is not mode 0600")
            token = token_path.read_text(encoding="utf-8").strip()
        except OSError as exc:
            raise ExecutorRefused("memory service bearer token is unavailable") from exc
        if not token:
            raise ExecutorRefused("memory service bearer token is empty")
        explicit[authorization] = token
    if context.route.client == "vibe":
        # VIBE_DISABLE_TELEMETRY is ignored by Vibe 3.1.2.
        explicit["VIBE_ENABLE_TELEMETRY"] = "false"
    return explicit


def _base_explicit_environment(
    context: ExecContext,
    *,
    policy_path: Path,
    policy_digest: str,
    pycache_prefix: str,
) -> dict[str, str]:
    """Build the unconditional child environment from its single key source."""
    assert context.route is not None
    assert context.installation_root is not None
    assert context.workspace_root is not None
    assert context.run_dir is not None
    assert context.control_dir is not None
    assert context.work_dir is not None
    assert context.project_dir is not None
    return {
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
        "PYTHONPYCACHEPREFIX": pycache_prefix,
        "MAGNOLIA_ROOT": str(context.installation_root.parent.resolve()),
        "MAGNOLIA_PROJECT_DIR": str(context.project_dir.resolve()),
        "MAGNOLIA_RULES_DIR": "rules",
        "MAGNOLIA_TRANSCRIPT_TOOL_CHARS": "4000",
        "MAGNOLIA_DISTILL_INTERVAL_MIN": "60",
        "COMPCHEM_TOOLS_PORT": str(context.tools_port),
    }


def _fresh_pycache_prefix(invocation_dir: Path) -> str:
    """Return a new run-owned cache path so repository bytecode is never read."""
    while True:
        candidate = invocation_dir / f"pycache-{uuid.uuid4().hex}"
        if not candidate.exists() and not candidate.is_symlink():
            return str(candidate)


def _configuration_identity(
    context: ExecContext,
    settings_body: bytes,
    mcp_body: bytes,
    native_bodies: Mapping[str, bytes],
    *,
    policy_body: bytes | None = None,
    invocation_dir: Path | None = None,
) -> str:
    assert context.route is not None
    assert context.project_dir is not None
    if context.route.client == "codex":
        if policy_body is None or invocation_dir is None:
            raise ExecutorRefused("Codex configuration identity requires run-owned policy")
        replacements = _identity_replacements(context, invocation_dir)
        bodies = [
            ("settings.json", _normalize_identity_body(settings_body, replacements)),
            ("mcp.json", _normalize_identity_body(mcp_body, replacements)),
            *(
                (name, _normalize_identity_body(body, replacements))
                for name, body in sorted(native_bodies.items())
            ),
            ("policy.json", _normalize_identity_body(policy_body, replacements)),
        ]
        return _digest_named_bodies(bodies)
    if context.route.client in {"claude-code", "vibe"}:
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
    client_sha256: str | None,
    code_mode_host_path: Path | None,
    code_mode_host_sha256: str | None,
    code_mode_host_version: str | None,
    codex_package_identity: Mapping[str, Any] | None,
    protected_env_names: frozenset[str],
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
        client_sha256,
        code_mode_host_path,
        code_mode_host_sha256,
        code_mode_host_version,
        codex_package_identity,
        protected_env_names,
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
    client_sha256: str | None,
    code_mode_host_path: Path | None,
    code_mode_host_sha256: str | None,
    code_mode_host_version: str | None,
    codex_package_identity: Mapping[str, Any] | None,
    protected_env_names: frozenset[str],
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
        protected_env_names=protected_env_names & frozenset(env),
        client_sha256=client_sha256,
        code_mode_host_path=code_mode_host_path,
        code_mode_host_sha256=code_mode_host_sha256,
        code_mode_host_version=code_mode_host_version,
        codex_package_identity=(
            MappingProxyType(dict(codex_package_identity))
            if codex_package_identity is not None
            else None
        ),
        memory_service_handle=context.memory_service.get("_handle"),
    )


def _json_bytes(value: Mapping[str, Any]) -> bytes:
    return (json.dumps(value, sort_keys=True, separators=(",", ":")) + "\n").encode()


def _canonical_bytes(value: Any) -> bytes:
    return (json.dumps(value, sort_keys=True, separators=(",", ":")) + "\n").encode()


def _digest(body: bytes) -> str:
    return hashlib.sha256(body).hexdigest()
