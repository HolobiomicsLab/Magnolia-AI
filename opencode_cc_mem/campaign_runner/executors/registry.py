"""Select executors; role admission is independent of client identity."""

from __future__ import annotations

from campaign_runner.workers import OpenCodeExecutor, WorkerExecutor
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from campaign_runner.routing import ResolvedRoute

from .contracts import ExecContext


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


def executor_for(route: ResolvedRoute, role: str, context: ExecContext) -> WorkerExecutor:
    if role not in {"worker", "reviewer"}:
        raise ExecutorRefused(f"executor registry does not provide the {role} role")
    if route.client != "opencode":
        if not _capable(context, route.client, role):
            raise ExecutorRefused(f"{route.client} lacks accepted {role} role capability")
        raise ExecutorRefused(f"{route.client} {role} executor is not implemented in this task")
    return OpenCodeExecutor(
        binary=context.opencode_binary or "opencode",
        model=route.model or "",
        agent=context.agent,
        server_url=context.opencode_server,
        db_path=context.opencode_db,
        timeout_seconds=context.timeout_seconds,
        max_steps=context.role_cap,
    )
