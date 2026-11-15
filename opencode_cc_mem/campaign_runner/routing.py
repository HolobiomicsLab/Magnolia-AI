"""Resolve explicit client routes without launching a client."""

from __future__ import annotations

import os
from dataclasses import dataclass
from typing import Any, Mapping

CLIENTS = ("claude-code", "codex", "vibe")
ROLES = ("worker", "reviewer", "helper", "evaluator", "interactive")


class RouteError(ValueError):
    """Raised when a role has no usable configured model."""


@dataclass(frozen=True)
class ResolvedRoute:
    client: str
    model: str | None
    family: str | None
    subscription_key: str | None
    role: str
    source: str

    @property
    def hook_harness(self) -> str:
        return "claude" if self.client == "claude-code" else self.client

    @property
    def native_default(self) -> bool:
        return self.model is None


def _value(inputs: Mapping[str, Any], key: str) -> Any:
    value = inputs.get(key)
    return value if isinstance(value, Mapping) else {}


def _global(inputs: Mapping[str, Any], harness: str | None = None) -> tuple[str, str | None, str]:
    client = harness or str(inputs.get("harness") or "opencode")
    if client == "opencode":
        return client, inputs.get("main_model"), "main_model"
    return client, _value(inputs, "models").get(client), f"models.{client}"


def _route(
    configured: str | None,
    role: str,
    source: str,
    default_client: str,
    *,
    required: bool,
) -> ResolvedRoute:
    if configured is None or not str(configured).strip():
        if not required:
            return ResolvedRoute(default_client, None, None, default_client, role, "native-default")
        raise RouteError(f"no model configured for {role}; configure a role route or a model for its harness")
    configured = str(configured)
    from .reviewers import model_family

    client = default_client if role == "interactive" else "opencode"
    model = configured
    for prefix in CLIENTS:
        if configured.startswith(prefix + "/"):
            client, model = prefix, configured[len(prefix) + 1 :]
            break
    if not model:
        raise RouteError(f"empty model in route for {role}: {configured!r}")
    subscription_key = client if client != "opencode" else f"opencode:{model.split('/', 1)[0]}"
    return ResolvedRoute(client, model, model_family(model), subscription_key, role, source)


def resolve_route(role: str, inputs: Mapping[str, Any]) -> ResolvedRoute:
    """Resolve role overrides using mappings or existing typed config objects."""
    if role not in ROLES:
        raise RouteError(f"unknown route role: {role}")
    get = inputs.get
    routes = _value(inputs, "routes")
    client, global_model, global_source = _global(inputs)
    selected: str | None = None
    source = global_source
    if role == "worker":
        stage = _value(inputs, "stage")
        worker = _value(inputs, "worker")
        hard_stage = get("hard", stage.get("hard", False))
        candidates = (
            (stage.get("model"), "stage.model"),
            (worker.get("hard_stage_model") if hard_stage else None, "worker.hard_stage_model"),
            (worker.get("model"), "worker.model"),
            (routes.get("worker"), "routes.worker"),
        )
    elif role == "reviewer":
        reviewer = _value(inputs, "reviewer")
        candidates = (
            (reviewer.get("model"), "reviewer.model"),
            (routes.get("reviewer"), "routes.reviewer"),
        )
    elif role == "helper":
        agent = _value(inputs, "agent")
        json_config = _value(agent, "json")
        candidates = (
            (json_config.get("model"), "agent.json.model"),
            (os.environ.get("MAGNOLIA_AGENTS_MODEL"), "MAGNOLIA_AGENTS_MODEL"),
            (routes.get("helper"), "routes.helper"),
        )
    elif role == "evaluator":
        candidates = (
            (get("model"), "model"),
            (os.environ.get("MAGNOLIA_EVAL_MODEL"), "MAGNOLIA_EVAL_MODEL"),
            (routes.get("evaluator"), "routes.evaluator"),
        )
    else:
        candidates = (
            (get("model") or get("model_flag"), "model"),
            (routes.get("interactive"), "routes.interactive"),
        )
    for candidate, candidate_source in candidates:
        if candidate:
            selected, source = candidate, candidate_source
            break
    explicit_harness = get("explicit_harness") or get("harness_flag")
    if role == "interactive" and explicit_harness and selected is None:
        client, selected, source = _global(inputs, str(explicit_harness))
        if selected is None:
            return _route(None, role, source, client, required=False)
    if role == "interactive" and selected is None:
        if explicit_harness:
            client = str(explicit_harness)
        return _route(None, role, source, client, required=False)
    if role == "helper" and selected is None and global_model is None:
        selected, source = "deepseek/deepseek-flash", "legacy-fallback"
    if role == "evaluator" and selected is None:
        selected, source = get("api_fallback_model"), "api-fallback"
    if selected is None:
        selected, source = global_model, global_source
    return _route(selected, role, source, client, required=True)
