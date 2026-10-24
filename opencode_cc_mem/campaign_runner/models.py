"""Validated campaign protocol models."""

from __future__ import annotations

import hashlib
import json
import re
from collections.abc import Mapping
from dataclasses import asdict, dataclass
from pathlib import Path, PurePosixPath
from typing import Any


SCHEMA_VERSION = 1
STAGE_TYPES = ("human", "script", "opencode", "job")
RETRY_POLICIES = ("none", "infra_once", "repair_once")
GATE_FAIL_ACTIONS = ("repair", "human", "failed", "halt")
_SAFE_NAME = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]*$")


class CampaignError(Exception):
    """Base exception for campaign operations."""


class ProtocolError(CampaignError):
    """Raised when a protocol is missing or invalid."""


def _error(where: str, message: str) -> ProtocolError:
    return ProtocolError(f"{where}: {message}")


def _check_mapping(value: Any, where: str) -> Mapping[str, Any]:
    if not isinstance(value, Mapping):
        raise _error(where, "must be an object")
    return value


def _check_keys(value: Mapping[str, Any], allowed: set[str], where: str) -> None:
    unknown = sorted(set(value) - allowed)
    if unknown:
        raise _error(where, f"unknown field(s): {', '.join(unknown)}")


def _required(value: Mapping[str, Any], key: str, where: str) -> Any:
    if key not in value:
        raise _error(where, f"missing required field '{key}'")
    return value[key]


def _string(value: Any, where: str, allow_empty: bool = False) -> str:
    if not isinstance(value, str):
        raise _error(where, "must be a string")
    if not allow_empty and not value.strip():
        raise _error(where, "must not be empty")
    return value


def _safe_name(value: Any, where: str) -> str:
    text = _string(value, where)
    if not _SAFE_NAME.fullmatch(text):
        raise _error(where, "must contain only letters, numbers, dot, dash, underscore")
    return text


def _relative_path(value: Any, where: str) -> str:
    text = _string(value, where)
    path = PurePosixPath(text)
    if path.is_absolute() or ".." in path.parts:
        raise _error(where, "must be a relative path without '..'")
    return text


def _integer(value: Any, where: str, minimum: int = 0) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise _error(where, "must be an integer")
    if value < minimum:
        raise _error(where, f"must be >= {minimum}")
    return value


def _number(value: Any, where: str, positive: bool = False) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise _error(where, "must be a number")
    number = float(value)
    if positive and number <= 0:
        raise _error(where, "must be > 0")
    if number < 0:
        raise _error(where, "must be >= 0")
    return number


def _boolean(value: Any, where: str) -> bool:
    if not isinstance(value, bool):
        raise _error(where, "must be a boolean")
    return value


def _string_list(value: Any, where: str, path_values: bool = False) -> tuple[str, ...]:
    if not isinstance(value, list):
        raise _error(where, "must be an array of strings")
    items: list[str] = []
    for index, item in enumerate(value):
        item_where = f"{where}[{index}]"
        items.append(_relative_path(item, item_where) if path_values else _string(item, item_where))
    return tuple(items)


def _argv(value: Any, where: str, allow_template: bool = False) -> tuple[str, ...] | str:
    if allow_template and isinstance(value, str):
        return _string(value, where)
    if not isinstance(value, list) or not value:
        raise _error(where, "must be a non-empty argv array")
    items: list[str] = []
    for index, item in enumerate(value):
        text = _string(item, f"{where}[{index}]")
        if ".." in PurePosixPath(text).parts:
            raise _error(f"{where}[{index}]", "must not contain '..'")
        items.append(text)
    return tuple(items)


def _model(value: Any, where: str, required: bool = True) -> str | None:
    if value is None and not required:
        return None
    text = _string(value, where)
    if "/" not in text:
        raise _error(where, "must use provider/model format")
    return text


@dataclass(frozen=True)
class Budget:
    max_tokens: int
    max_wall_hours: float
    max_core_hours: float
    max_attempts_per_stage: int
    max_parallel_jobs: int
    max_followups: int

    @classmethod
    def from_dict(cls, value: Any, where: str) -> "Budget":
        data = _check_mapping(value, where)
        allowed = {
            "max_tokens",
            "max_wall_hours",
            "max_core_hours",
            "max_attempts_per_stage",
            "max_parallel_jobs",
            "max_followups",
        }
        _check_keys(data, allowed, where)
        return cls(
            max_tokens=_integer(_required(data, "max_tokens", where), f"{where}.max_tokens", 1),
            max_wall_hours=_number(
                _required(data, "max_wall_hours", where), f"{where}.max_wall_hours", True
            ),
            max_core_hours=_number(
                _required(data, "max_core_hours", where), f"{where}.max_core_hours"
            ),
            max_attempts_per_stage=_integer(
                _required(data, "max_attempts_per_stage", where),
                f"{where}.max_attempts_per_stage",
                1,
            ),
            max_parallel_jobs=_integer(
                _required(data, "max_parallel_jobs", where), f"{where}.max_parallel_jobs", 1
            ),
            max_followups=_integer(
                _required(data, "max_followups", where), f"{where}.max_followups"
            ),
        )


@dataclass(frozen=True)
class WorkerConfig:
    model: str
    agent: str
    hard_stage_model: str | None = None
    max_steps: int = 30
    timeout_seconds: int = 1800

    @classmethod
    def from_dict(cls, value: Any, where: str) -> "WorkerConfig":
        data = _check_mapping(value, where)
        allowed = {"model", "agent", "hard_stage_model", "max_steps", "timeout_seconds"}
        _check_keys(data, allowed, where)
        hard_model = data.get("hard_stage_model")
        return cls(
            model=_model(_required(data, "model", where), f"{where}.model") or "",
            agent=_safe_name(_required(data, "agent", where), f"{where}.agent"),
            hard_stage_model=(
                _model(hard_model, f"{where}.hard_stage_model", False)
                if hard_model is not None
                else None
            ),
            max_steps=_integer(data.get("max_steps", 30), f"{where}.max_steps", 1),
            timeout_seconds=_integer(
                data.get("timeout_seconds", 1800), f"{where}.timeout_seconds", 1
            ),
        )


@dataclass(frozen=True)
class ReviewerConfig:
    enabled: bool
    model: str | None = None
    alternate_model: str | None = None
    max_steps: int = 8
    timeout_seconds: int = 600
    repair_attempts: int = 1

    @classmethod
    def from_dict(cls, value: Any, where: str) -> "ReviewerConfig":
        data = _check_mapping(value, where)
        allowed = {
            "enabled",
            "model",
            "alternate_model",
            "max_steps",
            "timeout_seconds",
            "repair_attempts",
        }
        _check_keys(data, allowed, where)
        enabled = _boolean(_required(data, "enabled", where), f"{where}.enabled")
        model_value = data.get("model")
        alternate_value = data.get("alternate_model")
        if enabled and model_value is None:
            raise _error(where, "model is required when reviewer is enabled")
        return cls(
            enabled=enabled,
            model=_model(model_value, f"{where}.model", False),
            alternate_model=_model(alternate_value, f"{where}.alternate_model", False),
            max_steps=_integer(data.get("max_steps", 8), f"{where}.max_steps", 1),
            timeout_seconds=_integer(
                data.get("timeout_seconds", 600), f"{where}.timeout_seconds", 1
            ),
            repair_attempts=_integer(
                data.get("repair_attempts", 1), f"{where}.repair_attempts"
            ),
        )


@dataclass(frozen=True)
class Stage:
    id: str
    type: str
    model: str | None = None
    inputs: tuple[str, ...] = ()
    outputs: tuple[str, ...] = ()
    command: tuple[str, ...] | str | None = None
    prompt: str | None = None
    gate: tuple[str, ...] = ()
    retry: str = "none"
    review: bool = False
    on_gate_fail: str = "human"
    timeout_seconds: int | None = None

    @classmethod
    def from_dict(cls, value: Any, where: str) -> "Stage":
        data = _check_mapping(value, where)
        allowed = {
            "id",
            "type",
            "model",
            "inputs",
            "outputs",
            "command",
            "prompt",
            "gate",
            "retry",
            "review",
            "on_gate_fail",
            "timeout_seconds",
        }
        _check_keys(data, allowed, where)
        stage_id = _safe_name(_required(data, "id", where), f"{where}.id")
        stage_type = _string(_required(data, "type", where), f"{where}.type")
        if stage_type not in STAGE_TYPES:
            raise _error(f"{where}.type", f"must be one of: {', '.join(STAGE_TYPES)}")
        retry = _string(data.get("retry", "none"), f"{where}.retry")
        if retry not in RETRY_POLICIES:
            raise _error(f"{where}.retry", f"must be one of: {', '.join(RETRY_POLICIES)}")
        on_gate_fail = _string(data.get("on_gate_fail", "human"), f"{where}.on_gate_fail")
        if on_gate_fail not in GATE_FAIL_ACTIONS:
            raise _error(
                f"{where}.on_gate_fail",
                f"must be one of: {', '.join(GATE_FAIL_ACTIONS)}",
            )
        model_value = data.get("model")
        if model_value is not None and stage_type != "opencode":
            raise _error(f"{where}.model", "only opencode stages may override the model")
        review = _boolean(data.get("review", False), f"{where}.review")
        if review and stage_type != "opencode":
            raise _error(f"{where}.review", "only opencode stages may use a reviewer")
        command_value = data.get("command")
        command: tuple[str, ...] | str | None
        if stage_type == "script":
            command = _argv(_required(data, "command", where), f"{where}.command")
        elif stage_type == "job":
            command = _argv(
                _required(data, "command", where), f"{where}.command", allow_template=True
            )
        else:
            if command_value is not None:
                raise _error(f"{where}.command", "not valid for this stage type")
            command = None
        prompt_value = data.get("prompt")
        if stage_type == "opencode":
            prompt = _relative_path(_required(data, "prompt", where), f"{where}.prompt")
        else:
            if prompt_value is not None:
                raise _error(f"{where}.prompt", "only valid for opencode stages")
            prompt = None
        if stage_type == "human":
            gate: tuple[str, ...] = ()
        else:
            gate = _argv(_required(data, "gate", where), f"{where}.gate")  # type: ignore[assignment]
        timeout_value = data.get("timeout_seconds")
        return cls(
            id=stage_id,
            type=stage_type,
            model=_model(model_value, f"{where}.model", False),
            inputs=_string_list(data.get("inputs", []), f"{where}.inputs", True),
            outputs=_string_list(data.get("outputs", []), f"{where}.outputs", True),
            command=command,
            prompt=prompt,
            gate=gate,
            retry=retry,
            review=review,
            on_gate_fail=on_gate_fail,
            timeout_seconds=(
                _integer(timeout_value, f"{where}.timeout_seconds", 1)
                if timeout_value is not None
                else None
            ),
        )


@dataclass(frozen=True)
class Protocol:
    schema: int
    id: str
    project: str
    worker: WorkerConfig
    reviewer: ReviewerConfig
    budget: Budget
    stages: tuple[Stage, ...]

    @classmethod
    def from_dict(cls, value: Any) -> "Protocol":
        data = _check_mapping(value, "protocol")
        allowed = {"schema", "id", "project", "worker", "reviewer", "budget", "stages"}
        _check_keys(data, allowed, "protocol")
        schema = _integer(_required(data, "schema", "protocol"), "protocol.schema", 1)
        if schema != SCHEMA_VERSION:
            raise _error("protocol.schema", f"unsupported schema {schema}")
        stages_value = _required(data, "stages", "protocol")
        if not isinstance(stages_value, list) or not stages_value:
            raise _error("protocol.stages", "must be a non-empty array")
        stages = tuple(
            Stage.from_dict(item, f"protocol.stages[{index}]")
            for index, item in enumerate(stages_value)
        )
        seen: set[str] = set()
        for stage in stages:
            if stage.id in seen:
                raise _error("protocol.stages", f"duplicate stage id '{stage.id}'")
            seen.add(stage.id)
        protocol = cls(
            schema=schema,
            id=_safe_name(_required(data, "id", "protocol"), "protocol.id"),
            project=_safe_name(_required(data, "project", "protocol"), "protocol.project"),
            worker=WorkerConfig.from_dict(
                _required(data, "worker", "protocol"), "protocol.worker"
            ),
            reviewer=ReviewerConfig.from_dict(
                _required(data, "reviewer", "protocol"), "protocol.reviewer"
            ),
            budget=Budget.from_dict(_required(data, "budget", "protocol"), "protocol.budget"),
            stages=stages,
        )
        return protocol

    def stage(self, stage_id: str) -> Stage:
        for stage in self.stages:
            if stage.id == stage_id:
                return stage
        raise ProtocolError(f"protocol: unknown stage '{stage_id}'")

    def next_stage_id(self, stage_id: str) -> str | None:
        for index, stage in enumerate(self.stages):
            if stage.id == stage_id:
                if index + 1 >= len(self.stages):
                    return None
                return self.stages[index + 1].id
        raise ProtocolError(f"protocol: unknown stage '{stage_id}'")

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def load_protocol(path: str | Path) -> Protocol:
    protocol_path = Path(path)
    try:
        raw = protocol_path.read_text(encoding="utf-8")
    except OSError as exc:
        raise ProtocolError(f"cannot read protocol {protocol_path}: {exc}") from exc
    try:
        data = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise ProtocolError(f"invalid JSON in {protocol_path}: {exc}") from exc
    return Protocol.from_dict(data)


def canonical_protocol_bytes(protocol: Protocol) -> bytes:
    return json.dumps(
        protocol.to_dict(),
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
    ).encode("utf-8")


def protocol_sha256(protocol: Protocol) -> str:
    return hashlib.sha256(canonical_protocol_bytes(protocol)).hexdigest()
