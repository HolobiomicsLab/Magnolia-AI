"""Campaign state, journal, locks, and transitions."""

from __future__ import annotations

import fcntl
import hashlib
import json
import os
import shutil
from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterator, Mapping

from .models import (
    CampaignError,
    Protocol,
    ProtocolError,
    canonical_protocol_bytes,
    load_protocol,
    protocol_sha256,
)


STATES = (
    "queued",
    "running",
    "waiting_job",
    "validating",
    "waiting_human",
    "completed",
    "failed",
    "halted",
    "paused",
)
TERMINAL_STATES = frozenset({"completed", "failed", "halted"})
PAUSABLE_STATES = frozenset({"queued", "waiting_job", "validating"})
TRANSITIONS: dict[str, frozenset[str]] = {
    "queued": frozenset({"running", "paused", "halted", "failed"}),
    "running": frozenset({"validating", "waiting_job", "waiting_human", "failed", "halted"}),
    "waiting_job": frozenset(
        {"validating", "waiting_human", "failed", "halted", "paused"}
    ),
    "validating": frozenset(
        {"queued", "running", "waiting_human", "completed", "failed", "halted", "paused"}
    ),
    "waiting_human": frozenset({"queued", "completed", "failed", "halted", "paused"}),
    "paused": frozenset({"queued", "halted"}),
    "completed": frozenset(),
    "failed": frozenset(),
    "halted": frozenset(),
}
USAGE_KEYS = (
    "input_tokens",
    "output_tokens",
    "reasoning_tokens",
    "cache_read_tokens",
    "cache_write_tokens",
    "cost_usd",
    "core_hours",
    "human_minutes",
)
CAMPAIGN_KEYS = {
    "schema",
    "protocol_id",
    "protocol_sha256",
    "state",
    "stage",
    "attempt",
    "jobs",
    "usage",
    "created_at",
    "updated_at",
    "brief_sha256",
    "memory_state",
}


class StateError(CampaignError):
    """Raised when campaign state is invalid or a transition is refused."""


class CampaignBusyError(StateError):
    """Raised when another process holds the campaign lock."""


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def validate_transition(current: str, target: str) -> None:
    if current not in STATES:
        raise StateError(f"unknown current state '{current}'")
    if target not in STATES:
        raise StateError(f"unknown target state '{target}'")
    if target not in TRANSITIONS[current]:
        raise StateError(f"transition {current} -> {target} is not allowed")


@dataclass
class CampaignState:
    schema: int
    protocol_id: str
    protocol_sha256: str
    state: str
    stage: str
    attempt: int
    jobs: dict[str, dict[str, Any]] = field(default_factory=dict)
    usage: dict[str, float] = field(default_factory=lambda: {key: 0 for key in USAGE_KEYS})
    created_at: str = field(default_factory=utc_now)
    updated_at: str = field(default_factory=utc_now)
    brief_sha256: str | None = None
    memory_state: dict[str, Any] | None = None

    @classmethod
    def from_dict(cls, value: Any) -> "CampaignState":
        if not isinstance(value, dict):
            raise StateError("campaign state must be an object")
        unknown = sorted(set(value) - CAMPAIGN_KEYS)
        if unknown:
            raise StateError(f"campaign state has unknown fields: {', '.join(unknown)}")
        required = {
            "schema",
            "protocol_id",
            "protocol_sha256",
            "state",
            "stage",
            "attempt",
            "jobs",
            "usage",
            "created_at",
            "updated_at",
        }
        missing = sorted(required - set(value))
        if missing:
            raise StateError(f"campaign state missing fields: {', '.join(missing)}")
        if value["schema"] != 1:
            raise StateError(f"unsupported campaign schema {value['schema']}")
        if value["state"] not in STATES:
            raise StateError(f"unknown campaign state '{value['state']}'")
        if not isinstance(value["jobs"], dict):
            raise StateError("campaign jobs must be an object")
        if not isinstance(value["usage"], dict):
            raise StateError("campaign usage must be an object")
        usage = {key: 0 for key in USAGE_KEYS}
        for key, item in value["usage"].items():
            if key not in usage:
                raise StateError(f"unknown usage field '{key}'")
            if isinstance(item, bool) or not isinstance(item, (int, float)):
                raise StateError(f"usage field '{key}' must be numeric")
            usage[key] = item
        memory_state = value.get("memory_state")
        if memory_state is not None and not isinstance(memory_state, dict):
            raise StateError("memory_state must be an object")
        return cls(
            schema=1,
            protocol_id=str(value["protocol_id"]),
            protocol_sha256=str(value["protocol_sha256"]),
            state=str(value["state"]),
            stage=str(value["stage"]),
            attempt=int(value["attempt"]),
            jobs=dict(value["jobs"]),
            usage=usage,
            created_at=str(value["created_at"]),
            updated_at=str(value["updated_at"]),
            brief_sha256=(
                str(value["brief_sha256"]) if value.get("brief_sha256") is not None else None
            ),
            memory_state=dict(memory_state) if memory_state is not None else None,
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema": self.schema,
            "protocol_id": self.protocol_id,
            "protocol_sha256": self.protocol_sha256,
            "state": self.state,
            "stage": self.stage,
            "attempt": self.attempt,
            "jobs": self.jobs,
            "usage": self.usage,
            "created_at": self.created_at,
            "updated_at": self.updated_at,
            "brief_sha256": self.brief_sha256,
            "memory_state": self.memory_state,
        }


def _atomic_write_bytes(path: Path, body: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    try:
        with temporary.open("wb") as handle:
            handle.write(body)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
    finally:
        try:
            temporary.unlink()
        except FileNotFoundError:
            pass


def _atomic_write_json(path: Path, value: Any) -> None:
    body = json.dumps(value, indent=2, sort_keys=True).encode("utf-8") + b"\n"
    _atomic_write_bytes(path, body)


class CampaignStore:
    def __init__(self, run_dir: str | Path) -> None:
        self.run_dir = Path(run_dir).resolve()
        self.control_dir = self.run_dir / "control"
        self.work_dir = self.run_dir / "work"
        self.attempts_dir = self.run_dir / "attempts"
        self.reports_dir = self.run_dir / "reports"
        self.campaign_path = self.control_dir / "campaign.json"
        self.protocol_path = self.control_dir / "protocol.json"
        self.brief_path = self.control_dir / "brief.md"
        self.events_path = self.control_dir / "events.jsonl"
        self.jobs_path = self.control_dir / "jobs.json"
        self.approvals_dir = self.control_dir / "approvals"
        self.lock_path = self.control_dir / ".lock"

    def _ensure_dirs(self) -> None:
        for directory in (
            self.control_dir,
            self.work_dir,
            self.attempts_dir,
            self.reports_dir,
            self.approvals_dir,
            self.control_dir / "scripts",
            self.control_dir / "checks",
        ):
            directory.mkdir(parents=True, exist_ok=True)

    @contextmanager
    def lock(self) -> Iterator[None]:
        self.control_dir.mkdir(parents=True, exist_ok=True)
        handle = self.lock_path.open("a+")
        try:
            try:
                fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            except OSError as exc:
                raise CampaignBusyError(f"campaign is locked: {self.run_dir}") from exc
            yield
        finally:
            try:
                fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
            finally:
                handle.close()

    def initialize(
        self,
        protocol_path: str | Path,
        brief_path: str | Path,
        memory_state: dict[str, Any] | None = None,
    ) -> CampaignState:
        protocol = load_protocol(protocol_path)
        brief_source = Path(brief_path)
        if not brief_source.is_file():
            raise StateError(f"brief does not exist: {brief_source}")
        brief_bytes = brief_source.read_bytes()
        if not brief_bytes.strip():
            raise StateError(f"brief is empty: {brief_source}")
        self._ensure_dirs()
        with self.lock():
            if self.campaign_path.exists() or self.events_path.exists():
                raise StateError(f"campaign already initialized: {self.run_dir}")
            protocol_body = canonical_protocol_bytes(protocol)
            protocol_hash = hashlib.sha256(protocol_body).hexdigest()
            brief_hash = hashlib.sha256(brief_bytes).hexdigest()
            _atomic_write_bytes(self.protocol_path, protocol_body + b"\n")
            _atomic_write_bytes(self.brief_path, brief_bytes)
            source_dir = Path(protocol_path).resolve().parent
            for asset in ("prompts", "scripts", "checks", "jobs"):
                source = source_dir / asset
                if source.is_dir():
                    shutil.copytree(source, self.control_dir / asset, dirs_exist_ok=True)
            now = utc_now()
            state = CampaignState(
                schema=1,
                protocol_id=protocol.id,
                protocol_sha256=protocol_hash,
                state="queued",
                stage=protocol.stages[0].id,
                attempt=0,
                created_at=now,
                updated_at=now,
                brief_sha256=brief_hash,
                memory_state=memory_state,
            )
            initial = {
                "schema": 1,
                "ts": now,
                "seq": 1,
                "campaign": protocol.id,
                "from": None,
                "to": "queued",
                "reason": "campaign initialized",
                "stage": state.stage,
                "attempt": 0,
                "initial": state.to_dict(),
            }
            self._append_event_locked(initial)
            _atomic_write_json(self.campaign_path, state.to_dict())
            _atomic_write_json(self.jobs_path, {})
            return state

    def verify_protocol(self) -> Protocol:
        if not self.protocol_path.is_file():
            raise StateError(f"missing protocol copy: {self.protocol_path}")
        try:
            protocol = load_protocol(self.protocol_path)
        except ProtocolError as exc:
            raise StateError(str(exc)) from exc
        expected = hashlib.sha256(self.protocol_path.read_bytes().rstrip(b"\n")).hexdigest()
        actual = protocol_sha256(protocol)
        if expected != actual:
            raise StateError("protocol copy is not canonical; possible tampering")
        return protocol

    def load(self) -> CampaignState:
        try:
            data = json.loads(self.campaign_path.read_text(encoding="utf-8"))
        except FileNotFoundError as exc:
            raise StateError(f"campaign does not exist: {self.run_dir}") from exc
        except json.JSONDecodeError as exc:
            raise StateError(f"invalid campaign JSON: {exc}") from exc
        state = CampaignState.from_dict(data)
        protocol = self.verify_protocol()
        if state.protocol_id != protocol.id:
            raise StateError("campaign protocol id does not match protocol copy")
        if state.protocol_sha256 != protocol_sha256(protocol):
            raise StateError("campaign protocol hash does not match protocol copy")
        if state.stage not in {stage.id for stage in protocol.stages}:
            raise StateError(f"campaign refers to unknown stage '{state.stage}'")
        return state

    def save(self, state: CampaignState) -> None:
        _atomic_write_json(self.campaign_path, state.to_dict())

    def read_events(self) -> list[dict[str, Any]]:
        try:
            text = self.events_path.read_text(encoding="utf-8")
        except FileNotFoundError:
            return []
        lines = [line for line in text.splitlines() if line.strip()]
        events: list[dict[str, Any]] = []
        for index, line in enumerate(lines):
            try:
                event = json.loads(line)
            except json.JSONDecodeError as exc:
                if index == len(lines) - 1:
                    break
                raise StateError(f"invalid event at line {index + 1}: {exc}") from exc
            if not isinstance(event, dict) or "seq" not in event or "to" not in event:
                raise StateError(f"invalid event at line {index + 1}")
            events.append(event)
        return events

    def _append_event_locked(self, event: dict[str, Any]) -> None:
        self.control_dir.mkdir(parents=True, exist_ok=True)
        with self.events_path.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(event, sort_keys=True) + "\n")
            handle.flush()
            os.fsync(handle.fileno())

    def _new_event(
        self,
        state: CampaignState,
        target: str,
        reason: str,
        stage: str,
        attempt: int,
    ) -> dict[str, Any]:
        events = self.read_events()
        seq = int(events[-1]["seq"]) + 1 if events else 1
        return {
            "schema": 1,
            "ts": utc_now(),
            "seq": seq,
            "campaign": state.protocol_id,
            "from": state.state,
            "to": target,
            "reason": reason,
            "stage": stage,
            "attempt": attempt,
        }

    def transition(
        self,
        target: str,
        reason: str,
        stage: str | None = None,
        attempt: int | None = None,
    ) -> CampaignState:
        with self.lock():
            return self._transition_locked(target, reason, stage, attempt)

    def _transition_locked(
        self,
        target: str,
        reason: str,
        stage: str | None = None,
        attempt: int | None = None,
        allow_human_exit: bool = False,
    ) -> CampaignState:
        state = self.load()
        validate_transition(state.state, target)
        if (
            state.state == "waiting_human"
            and target == "queued"
            and not allow_human_exit
        ):
            raise StateError("use approve() to leave waiting_human")
        resolved_stage = stage if stage is not None else state.stage
        protocol = self.verify_protocol()
        if resolved_stage not in {item.id for item in protocol.stages}:
            raise StateError(f"unknown stage '{resolved_stage}'")
        resolved_attempt = state.attempt if attempt is None else attempt
        if resolved_attempt < 0:
            raise StateError("attempt must be >= 0")
        event = self._new_event(state, target, reason, resolved_stage, resolved_attempt)
        self._append_event_locked(event)
        state.state = target
        state.stage = resolved_stage
        state.attempt = resolved_attempt
        state.updated_at = str(event["ts"])
        self.save(state)
        return state

    def update_usage(self, delta: Mapping[str, float], reason: str) -> CampaignState:
        with self.lock():
            state = self.load()
            for key, value in delta.items():
                if key not in state.usage:
                    raise StateError(f"unknown usage field '{key}'")
                state.usage[key] = float(state.usage[key]) + float(value)
            event = self._new_event(state, state.state, reason, state.stage, state.attempt)
            event["usage_after"] = state.usage
            self._append_event_locked(event)
            state.updated_at = str(event["ts"])
            self.save(state)
            return state

    def update_jobs(self, jobs: dict[str, dict[str, Any]], reason: str) -> CampaignState:
        with self.lock():
            state = self.load()
            state.jobs = jobs
            event = self._new_event(state, state.state, reason, state.stage, state.attempt)
            event["jobs_after"] = state.jobs
            self._append_event_locked(event)
            state.updated_at = str(event["ts"])
            self.save(state)
            return state

    def reconcile(self) -> CampaignState:
        with self.lock():
            events = self.read_events()
            if not events:
                raise StateError("cannot reconcile without journal events")
            if self.campaign_path.exists():
                state = self.load()
            else:
                state = self._reconstruct_from_events(events)
            last = events[-1]
            state.state = str(last["to"])
            if "stage" in last:
                state.stage = str(last["stage"])
            if "attempt" in last:
                state.attempt = int(last["attempt"])
            if isinstance(last.get("usage_after"), dict):
                state.usage = {key: float(value) for key, value in last["usage_after"].items()}
            if isinstance(last.get("jobs_after"), dict):
                state.jobs = dict(last["jobs_after"])
            state.updated_at = str(last.get("ts", state.updated_at))
            self.save(state)
            return state

    def _reconstruct_from_events(self, events: list[dict[str, Any]]) -> CampaignState:
        first = events[0]
        initial = first.get("initial")
        if not isinstance(initial, dict):
            raise StateError("cannot reconstruct campaign without initialization event")
        state = CampaignState.from_dict(initial)
        for event in events[1:]:
            state.state = str(event["to"])
            state.stage = str(event.get("stage", state.stage))
            state.attempt = int(event.get("attempt", state.attempt))
            if isinstance(event.get("usage_after"), dict):
                state.usage = {key: float(value) for key, value in event["usage_after"].items()}
            if isinstance(event.get("jobs_after"), dict):
                state.jobs = dict(event["jobs_after"])
            state.updated_at = str(event.get("ts", state.updated_at))
        return state

    def pause(self, reason: str = "operator pause") -> CampaignState:
        with self.lock():
            state = self.load()
            if state.state not in PAUSABLE_STATES:
                raise StateError(f"cannot pause from state '{state.state}'")
            return self._transition_locked("paused", reason)

    def resume(self, reason: str = "operator resume") -> CampaignState:
        with self.lock():
            state = self.load()
            if state.state != "paused":
                raise StateError(f"cannot resume from state '{state.state}'")
            return self._transition_locked("queued", reason)

    def halt(self, reason: str = "operator halt") -> CampaignState:
        with self.lock():
            state = self.load()
            if state.state in TERMINAL_STATES:
                raise StateError(f"campaign is already terminal: {state.state}")
            if state.state == "paused":
                return self._transition_locked("halted", reason)
            return self._transition_locked("halted", reason)

    def approve(
        self,
        approver: str,
        reason: str,
    ) -> CampaignState:
        with self.lock():
            state = self.load()
            if state.state != "waiting_human":
                raise StateError("campaign is not waiting for approval")
            protocol = self.verify_protocol()
            stage = protocol.stage(state.stage)
            next_stage = protocol.next_stage_id(state.stage)
            target = "completed" if stage.type == "human" and next_stage is None else "queued"
            approval = {
                "ts": utc_now(),
                "approver": approver,
                "reason": reason,
                "stage": state.stage,
                "attempt": state.attempt,
                "from": state.state,
                "to": target,
            }
            filename = (
                f"{approval['ts'].replace(':', '').replace('+', '_')}-{os.getpid()}.json"
            )
            _atomic_write_json(self.approvals_dir / filename, approval)
            if target == "completed":
                return self._transition_locked(
                    "completed",
                    f"approved by {approver}: {reason}",
                    allow_human_exit=True,
                )
            if stage.type == "human":
                return self._transition_locked(
                    "queued",
                    f"approved by {approver}: {reason}",
                    stage=next_stage,
                    attempt=0,
                    allow_human_exit=True,
                )
            return self._transition_locked(
                "queued",
                f"approved by {approver}: {reason}",
                stage=state.stage,
                attempt=state.attempt,
                allow_human_exit=True,
            )
