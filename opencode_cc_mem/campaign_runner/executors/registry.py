"""Select executors; role admission is independent of client identity."""

from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING, Any, Callable, Mapping

from campaign_runner.service import read_service
from campaign_runner.workers import OpenCodeExecutor, WorkerExecutor

if TYPE_CHECKING:
    from campaign_runner.routing import ResolvedRoute

from .contracts import ExecContext
from .opencode import (
    OpenCodeEvaluatorExecutor,
    OpenCodeHelperExecutor,
    OpenCodeServiceExecutor,
)


class ExecutorRefused(RuntimeError):
    def __init__(self, reason: str):
        self.reason = reason
        super().__init__(reason)


def _capable(context: ExecContext, client: str, role: str) -> bool:
    capabilities = context.role_capabilities.get(client)
    if capabilities is None:
        return False
    if isinstance(capabilities, dict):
        evidence = capabilities.get(role, False)
        if isinstance(evidence, dict):
            return evidence.get("accepted") is True
        return evidence is True
    return role in capabilities


def _governor_cap(context: ExecContext) -> int | None:
    if context.governor_cap is not None:
        return context.governor_cap
    governance = context.governance
    if governance is not None:
        return governance.governor.policy.max_turns_per_run
    return None


def _effective_cap(context: ExecContext, default: int | None = None) -> int | None:
    role_cap = context.role_cap if context.role_cap is not None else default
    governor_cap = _governor_cap(context)
    caps = [cap for cap in (role_cap, governor_cap) if cap is not None]
    return min(caps) if caps else None


def _release_prepared_memory_service(prepared: Any) -> None:
    """Release a managed handle when registry validation refuses after preparation."""
    handle = getattr(prepared, "memory_service_handle", None)
    if handle is None:
        return
    from compchem_memory.harness.memory_service import release_memory_service

    release_memory_service(handle)


def _managed_service_record(context: ExecContext) -> Mapping[str, Any] | None:
    if context.project_dir is None or not context.opencode_server:
        return None
    record = read_service(context.project_dir)
    if not isinstance(record, Mapping):
        return None
    port = record.get("port")
    if context.opencode_server.rstrip("/") not in {
        f"http://127.0.0.1:{port}",
        f"http://localhost:{port}",
    }:
        return None
    return record


def _service_cap_checker(
    context: ExecContext, agent: str
) -> Callable[[int], None] | None:
    if not context.opencode_server or _governor_cap(context) is None:
        return None

    def verify(required_cap: int) -> None:
        managed = _managed_service_record(context)
        if managed is not None:
            recorded = managed.get("agent_steps")
            if not isinstance(recorded, Mapping):
                raise ExecutorRefused(
                    "managed OpenCode service started without recorded caps — "
                    "restart it when idle"
                )
            caps: Mapping[str, Any] | None = recorded
        else:
            supplied = context.setup.get("opencode_service_caps")
            caps = supplied if isinstance(supplied, Mapping) else None
        shown = caps.get(agent) if caps is not None else None
        if (
            managed is not None
            and isinstance(shown, int)
            and not isinstance(shown, bool)
            and shown > required_cap
        ):
            raise ExecutorRefused(
                "managed OpenCode service caps exceed the governor cap — "
                "restart it when idle"
            )
        if isinstance(shown, bool) or not isinstance(shown, int) or shown > required_cap:
            raise ExecutorRefused(
                "externally supplied OpenCode service must prove a compatible "
                f"{agent} cap via ExecContext.setup['opencode_service_caps']; "
                "use the managed dedicated service or supply its effective caps"
            )

    cap = _effective_cap(context)
    if cap is None:
        raise ExecutorRefused("governed OpenCode service requires a positive effective cap")
    verify(cap)
    return verify


def executor_for(route: ResolvedRoute, role: str, context: ExecContext) -> WorkerExecutor | Any:
    if role not in {"worker", "reviewer", "service", "helper", "evaluator"}:
        raise ExecutorRefused(f"executor registry does not provide the {role} role")
    if route.client != "opencode":
        if (
            route.client == "codex"
            and role in {"worker", "reviewer"}
            and not _capable(context, route.client, role)
        ):
            raise ExecutorRefused(
                f"D2: Codex {role} lacks accepted capability projection evidence"
            )
        if not _capable(context, route.client, role):
            raise ExecutorRefused(f"{route.client} lacks accepted {role} role capability")
        if route.client == "vibe" and role == "worker":
            raise ExecutorRefused(
                "D4: Vibe worker refused because resume, hook-crash and the full "
                "denial matrix are not observed"
            )
        if route.client == "claude-code":
            from campaign_runner.launch import ROLE_CONFIG_NAMES, prepare_launch
            from campaign_runner.roles import load_role

            from .claude_code import ClaudeCodeExecutor

            if not route.model:
                raise ExecutorRefused("Claude Code requires an explicit selected model")
            prepared = prepare_launch(context)
            try:
                default_cap = load_role(ROLE_CONFIG_NAMES[role]).default_cap
                effective_cap = _effective_cap(context, default_cap)
                if effective_cap is None:
                    raise ExecutorRefused("Claude Code requires a positive effective cap")
                return ClaudeCodeExecutor(
                    prepared=prepared,
                    model=route.model,
                    role=role,
                    max_turns=effective_cap,
                    timeout_seconds=context.timeout_seconds,
                    governance=context.governance,
                    resume_permitted=(
                        context.resume_from is not None
                        or context.resume_artifact is not None
                    ),
                    resume_artifact=context.resume_artifact,
                )
            except Exception:
                _release_prepared_memory_service(prepared)
                raise
        if route.client == "codex":
            from campaign_runner.launch import ROLE_CONFIG_NAMES, prepare_launch
            from campaign_runner.roles import load_role

            from .codex import CodexExecutor, require_codex_trust

            if not route.model:
                raise ExecutorRefused("Codex requires an explicit selected model")
            if role in {"helper", "evaluator"} and context.required_capabilities:
                raise ExecutorRefused("Codex helper/evaluator contract is no-tools")
            if context.project_dir is None:
                raise ExecutorRefused("Codex requires a canonical project directory")
            require_codex_trust(context.project_dir, context.setup)
            prepared = prepare_launch(context)
            try:
                default_cap = load_role(ROLE_CONFIG_NAMES[role]).default_cap
                effective_cap = _effective_cap(context, default_cap)
                if effective_cap is None:
                    raise ExecutorRefused("Codex requires a positive supervising cap")
                return CodexExecutor(
                    prepared=prepared,
                    model=route.model,
                    role=role,
                    max_turns=effective_cap,
                    timeout_seconds=context.timeout_seconds,
                    governance=context.governance,
                    resume_permitted=(
                        context.resume_from is not None
                        or context.resume_artifact is not None
                    ),
                    resume_artifact=context.resume_artifact,
                    reasoning_effort=str(
                        context.setup.get("codex_reasoning_effort", "low")
                    ),
                    user_config_path=Path(
                        str(
                            context.setup.get(
                                "codex_user_config_path",
                                Path.home() / ".codex" / "config.toml",
                            )
                        )
                    ),
                )
            except Exception:
                _release_prepared_memory_service(prepared)
                raise
        if route.client == "vibe":
            from campaign_runner.launch import ROLE_CONFIG_NAMES, prepare_launch
            from campaign_runner.roles import load_role

            from .vibe import VibeExecutor, require_vibe_project_source, require_vibe_trust

            if not route.model:
                raise ExecutorRefused("Vibe requires an explicit selected model")
            if context.project_dir is None:
                raise ExecutorRefused("Vibe requires a canonical project directory")
            require_vibe_trust(context.project_dir, context.setup)
            require_vibe_project_source(context.project_dir, route.model)
            prepared = prepare_launch(context)
            try:
                default_cap = load_role(ROLE_CONFIG_NAMES[role]).default_cap
                effective_cap = _effective_cap(context, default_cap)
                if effective_cap is None:
                    raise ExecutorRefused("Vibe requires a positive effective cap")
                price = context.setup.get("max_price")
                if price is not None and (
                    isinstance(price, bool)
                    or not isinstance(price, (int, float))
                    or price <= 0
                ):
                    raise ExecutorRefused("Vibe max_price must be a positive number")
                return VibeExecutor(
                    prepared=prepared,
                    model=route.model,
                    role=role,
                    max_turns=effective_cap,
                    timeout_seconds=context.timeout_seconds,
                    governance=context.governance,
                    max_price=float(price) if price is not None else None,
                    resume_artifact=context.resume_artifact,
                )
            except Exception:
                _release_prepared_memory_service(prepared)
                raise
        raise ExecutorRefused(f"{route.client} {role} executor is not implemented in this task")
    if role == "service":
        return OpenCodeServiceExecutor(
            binary=context.opencode_binary or "opencode",
            db_path=context.opencode_db,
            project_dir=Path(context.project_dir) if context.project_dir else None,
            governor_cap=_governor_cap(context),
        )
    if role == "helper":
        return OpenCodeHelperExecutor(
            binary=context.opencode_binary or "opencode",
            model=route.model or "",
            role_cap=context.role_cap or 40,
            governor_cap=_governor_cap(context),
        )
    if role == "evaluator":
        return OpenCodeEvaluatorExecutor(
            binary=context.opencode_binary or "opencode",
            model=route.model or "",
            db_path=context.opencode_db,
            role_cap=context.role_cap or 1,
            governor_cap=_governor_cap(context),
        )
    effective_cap = _effective_cap(context)
    return OpenCodeExecutor(
        binary=context.opencode_binary or "opencode",
        model=route.model or "",
        agent=context.agent,
        server_url=context.opencode_server,
        db_path=context.opencode_db,
        environment=context.environment,
        timeout_seconds=context.timeout_seconds,
        max_format_retries=0 if role == "reviewer" else 2,
        max_steps=effective_cap,
        governance=context.governance,
        service_cap_check=_service_cap_checker(context, context.agent),
    )
