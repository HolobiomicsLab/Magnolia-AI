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
        if not _capable(context, route.client, role):
            raise ExecutorRefused(f"{route.client} lacks accepted {role} role capability")
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
