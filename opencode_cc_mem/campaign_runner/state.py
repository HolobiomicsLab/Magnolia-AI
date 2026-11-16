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

from .executors.contracts import SessionIdentity
from .models import (
    CampaignError,
    Protocol,
    ProtocolError,
    canonical_protocol_bytes,
    load_protocol,
    protocol_sha256,
)
from .security import redact_structure


STATES = (
    "queued",
    "running",
    "waiting_job",
    "validating",
    "waiting_quota",
    "waiting_human",
    "completed",
    "failed",
    "halted",
    "paused",
)
TERMINAL_STATES = frozenset({"completed", "failed", "halted"})
PAUSABLE_STATES = frozenset(
    {"queued", "running", "waiting_job", "validating", "waiting_quota"}
)
TRANSITIONS: dict[str, frozenset[str]] = {
    "queued": frozenset(
        {"running", "waiting_quota", "waiting_human", "paused", "halted", "failed"}
    ),
    "running": frozenset(
        {
            "validating",
            "waiting_job",
            "waiting_quota",
            "waiting_human",
            "failed",
            "halted",
            "paused",
        }
    ),
    "waiting_job": frozenset(
        {"validating", "waiting_human", "failed", "halted", "paused"}
    ),
    "validating": frozenset(
        {
            "queued",
            "running",
            "waiting_quota",
            "waiting_human",
            "completed",
            "failed",
            "halted",
            "paused",
        }
    ),
    "waiting_quota": frozenset(
        {"queued", "running", "validating", "paused", "halted", "failed"}
    ),
    "waiting_human": frozenset({"queued", "completed", "failed", "halted", "paused"}),
    "paused": frozenset(
        {"queued", "waiting_job", "validating", "waiting_quota", "halted", "failed"}
    ),
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
    "job_core_hours",
    "paused_from",
    "quota_wait",
    "resumed_from_quota",
}
QUOTA_PHASE_STATES = {
    "pre-worker": "queued",
    "worker": "running",
    "reviewer": "validating",
}
QUOTA_WAIT_KEYS = {
    "reason",
    "subscription_key",
    "observed_at",
    "hold_id",
    "reset_at",
    "suspended_phase",
    "stage",
    "attempt",
    "session_identity",
    "result_ref",
    "retry_cursor",
}
SESSION_IDENTITY_KEYS = {
    "client",
    "model",
    "session_id",
    "project_digest",
    "role",
    "policy_digest",
}


class StateError(CampaignError):
    """Raised when campaign state is invalid or a transition is refused."""


class CampaignBusyError(StateError):
    """Raised when another process holds the campaign lock."""


class CampaignStateChangedError(CampaignBusyError):
    """Raised when a runner's loaded state is no longer current."""


@dataclass(frozen=True)
class QuotaResumeResult:
    """Typed disposition for an operator quota-resume request."""

    state: "CampaignState"
    resumed: bool
    reason: str


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _utc_datetime(value: Any, field_name: str) -> datetime:
    if isinstance(value, str):
        try:
            value = datetime.fromisoformat(value)
        except ValueError as exc:
            raise StateError(f"quota {field_name} must be an ISO datetime") from exc
    if not isinstance(value, datetime) or value.tzinfo is None or value.utcoffset() is None:
        raise StateError(f"quota {field_name} must be timezone-aware")
    return value.astimezone(timezone.utc)


def _required_text(value: Any, field_name: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise StateError(f"quota {field_name} must be a non-empty string")
    return value


def _session_identity_from_dict(value: Any) -> SessionIdentity:
    if not isinstance(value, dict):
        raise StateError("quota session_identity must be an object")
    unknown = sorted(set(value) - SESSION_IDENTITY_KEYS)
    missing = sorted(SESSION_IDENTITY_KEYS - set(value))
    if unknown:
        raise StateError(
            "quota session_identity has unknown fields: " + ", ".join(unknown)
        )
    if missing:
        raise StateError(
            "quota session_identity missing fields: " + ", ".join(missing)
        )
    required = {
        key: _required_text(value[key], f"session_identity.{key}")
        for key in SESSION_IDENTITY_KEYS - {"model"}
    }
    model = value["model"]
    if model is not None:
        model = _required_text(model, "session_identity.model")
    return SessionIdentity(model=model, **required)


@dataclass(frozen=True)
class QuotaWait:
    """Durable continuation checkpoint for a quota-suspended campaign role."""

    reason: str
    subscription_key: str
    observed_at: datetime
    hold_id: str
    reset_at: datetime | None
    suspended_phase: str
    stage: str
    attempt: int
    session_identity: SessionIdentity | None = None
    result_ref: str | None = None
    retry_cursor: int | None = None

    def __post_init__(self) -> None:
        for name in ("reason", "subscription_key", "hold_id", "stage"):
            object.__setattr__(self, name, _required_text(getattr(self, name), name))
        if self.suspended_phase not in QUOTA_PHASE_STATES:
            raise StateError(f"unknown quota suspended_phase '{self.suspended_phase}'")
        if isinstance(self.attempt, bool) or not isinstance(self.attempt, int) or self.attempt < 0:
            raise StateError("quota attempt must be a non-negative integer")
        object.__setattr__(self, "observed_at", _utc_datetime(self.observed_at, "observed_at"))
        if self.reset_at is not None:
            object.__setattr__(self, "reset_at", _utc_datetime(self.reset_at, "reset_at"))
        if self.session_identity is not None and not isinstance(
            self.session_identity, SessionIdentity
        ):
            raise StateError("quota session_identity must be a SessionIdentity")
        if self.result_ref is not None:
            object.__setattr__(
                self, "result_ref", _required_text(self.result_ref, "result_ref")
            )
        if self.retry_cursor is not None and (
            isinstance(self.retry_cursor, bool)
            or not isinstance(self.retry_cursor, int)
            or self.retry_cursor < 0
        ):
            raise StateError("quota retry_cursor must be a non-negative integer")

    @classmethod
    def from_dict(cls, value: Any) -> "QuotaWait":
        """Validate and restore a serialized quota continuation checkpoint."""
        if not isinstance(value, dict):
            raise StateError("quota_wait must be an object")
        unknown = sorted(set(value) - QUOTA_WAIT_KEYS)
        missing = sorted(QUOTA_WAIT_KEYS - set(value))
        if unknown:
            raise StateError(f"quota_wait has unknown fields: {', '.join(unknown)}")
        if missing:
            raise StateError(f"quota_wait missing fields: {', '.join(missing)}")
        session = value["session_identity"]
        return cls(
            reason=value["reason"],
            subscription_key=value["subscription_key"],
            observed_at=_utc_datetime(value["observed_at"], "observed_at"),
            hold_id=value["hold_id"],
            reset_at=(
                _utc_datetime(value["reset_at"], "reset_at")
                if value["reset_at"] is not None
                else None
            ),
            suspended_phase=value["suspended_phase"],
            stage=value["stage"],
            attempt=value["attempt"],
            session_identity=(
                _session_identity_from_dict(session) if session is not None else None
            ),
            result_ref=value["result_ref"],
            retry_cursor=value["retry_cursor"],
        )

    def to_dict(self) -> dict[str, Any]:
        """Return the strict JSON-compatible quota checkpoint representation."""
        session = None
        if self.session_identity is not None:
            session = {
                key: getattr(self.session_identity, key)
                for key in SESSION_IDENTITY_KEYS
            }
        return {
            "reason": self.reason,
            "subscription_key": self.subscription_key,
            "observed_at": self.observed_at.isoformat(),
            "hold_id": self.hold_id,
            "reset_at": self.reset_at.isoformat() if self.reset_at else None,
            "suspended_phase": self.suspended_phase,
            "stage": self.stage,
            "attempt": self.attempt,
            "session_identity": session,
            "result_ref": self.result_ref,
            "retry_cursor": self.retry_cursor,
        }


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
    job_core_hours: dict[str, float] = field(default_factory=dict)
    paused_from: str | None = None
    quota_wait: QuotaWait | None = None
    resumed_from_quota: QuotaWait | None = None

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
        paused_from = value.get("paused_from")
        if paused_from is not None and paused_from not in STATES:
            raise StateError(f"unknown paused_from state '{paused_from}'")
        raw_job_core = value.get("job_core_hours", {})
        if not isinstance(raw_job_core, dict):
            raise StateError("job_core_hours must be an object")
        job_core_hours: dict[str, float] = {}
        for key, item in raw_job_core.items():
            if isinstance(item, bool) or not isinstance(item, (int, float)):
                raise StateError(f"job_core_hours[{key!r}] must be numeric")
            job_core_hours[str(key)] = float(item)
        raw_quota_wait = value.get("quota_wait")
        quota_wait = (
            QuotaWait.from_dict(raw_quota_wait) if raw_quota_wait is not None else None
        )
        raw_resumed_from_quota = value.get("resumed_from_quota")
        resumed_from_quota = (
            QuotaWait.from_dict(raw_resumed_from_quota)
            if raw_resumed_from_quota is not None
            else None
        )
        if value["state"] == "waiting_quota" and quota_wait is None:
            raise StateError("waiting_quota state requires quota_wait")
        if quota_wait is not None and resumed_from_quota is not None:
            raise StateError(
                "campaign cannot have both quota_wait and resumed_from_quota"
            )
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
            job_core_hours=job_core_hours,
            paused_from=paused_from,
            quota_wait=quota_wait,
            resumed_from_quota=resumed_from_quota,
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
            "job_core_hours": self.job_core_hours,
            "paused_from": self.paused_from,
            "quota_wait": self.quota_wait.to_dict() if self.quota_wait else None,
            "resumed_from_quota": (
                self.resumed_from_quota.to_dict() if self.resumed_from_quota else None
            ),
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
    body = (
        json.dumps(redact_structure(value), indent=2, sort_keys=True).encode("utf-8")
        + b"\n"
    )
    _atomic_write_bytes(path, body)


def memory_state_for_run(run_dir: Path) -> dict[str, Any]:
    if Path(run_dir).parent.name != "runs":
        return {"entries_sha256": None, "entry_count": 0}
    project_dir = Path(run_dir).parent.parent
    entries_dir = project_dir / ".magnolia" / "entries"
    if not entries_dir.is_dir():
        return {"entries_sha256": None, "entry_count": 0}
    digest = hashlib.sha256()
    count = 0
    for path in sorted(entries_dir.glob("*.md")):
        relative = path.relative_to(entries_dir).as_posix().encode()
        digest.update(relative)
        digest.update(b"\0")
        digest.update(path.read_bytes())
        digest.update(b"\0")
        count += 1
    return {"entries_sha256": digest.hexdigest(), "entry_count": count}


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
        self.leases_dir = self.control_dir / "leases"
        self.requests_dir = self.control_dir / "requests"
        self.distill_queue_path = self.control_dir / "distill-queue.jsonl"
        self.job_errors_path = self.control_dir / "job-errors.jsonl"
        self.cancel_errors_path = self.control_dir / "cancel-errors.jsonl"
        self.start_approval_path = self.approvals_dir / "start.json"
        self.lock_path = self.control_dir / ".lock"

    def _ensure_dirs(self) -> None:
        for directory in (
            self.control_dir,
            self.work_dir,
            self.attempts_dir,
            self.reports_dir,
            self.approvals_dir,
            self.requests_dir,
            self.leases_dir,
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
                memory_state=(
                    memory_state if memory_state is not None else memory_state_for_run(self.run_dir)
                ),
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
        stage_ids = {stage.id for stage in protocol.stages}
        self._validate_quota_checkpoint(
            state.quota_wait, state, stage_ids, "quota checkpoint"
        )
        self._validate_quota_checkpoint(
            state.resumed_from_quota,
            state,
            stage_ids,
            "resumed quota checkpoint",
        )
        return state

    @staticmethod
    def _validate_quota_checkpoint(
        checkpoint: QuotaWait | None,
        state: CampaignState,
        stage_ids: set[str],
        label: str,
    ) -> None:
        if checkpoint is None:
            return
        if checkpoint.stage not in stage_ids:
            raise StateError(
                f"{label} stage '{checkpoint.stage}' is not present in the protocol"
            )
        if checkpoint.stage != state.stage:
            raise StateError(
                f"{label} stage '{checkpoint.stage}' does not match "
                f"campaign stage '{state.stage}'"
            )
        if checkpoint.attempt != state.attempt:
            raise StateError(
                f"{label} attempt {checkpoint.attempt} does not match "
                f"campaign attempt {state.attempt} for stage '{state.stage}'"
            )

    def verify_memory_state(self) -> None:
        state = self.load()
        current = memory_state_for_run(self.run_dir)
        expected = state.memory_state or {}
        if expected.get("entries_sha256") != current.get("entries_sha256"):
            raise StateError("project memory changed since campaign initialization")

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
            handle.write(json.dumps(redact_structure(event), sort_keys=True) + "\n")
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
        event = {
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
        if state.state == "paused" and state.paused_from:
            event["paused_from"] = state.paused_from
        if state.quota_wait is not None:
            event["quota_hold_id"] = state.quota_wait.hold_id
            event["quota_wait_after"] = state.quota_wait.to_dict()
        if state.resumed_from_quota is not None:
            event["quota_resumed_from"] = state.resumed_from_quota.to_dict()
        return event

    def transition(
        self,
        target: str,
        reason: str,
        stage: str | None = None,
        attempt: int | None = None,
    ) -> CampaignState:
        with self.lock():
            return self._transition_locked(target, reason, stage, attempt)

    def transition_if_current(
        self,
        target: str,
        reason: str,
        *,
        expected_state: str,
        expected_stage: str,
        expected_attempt: int,
        stage: str | None = None,
        attempt: int | None = None,
    ) -> CampaignState:
        """Transition only when the campaign still matches the loaded state."""
        with self.lock():
            current = self.load()
            actual = (current.state, current.stage, current.attempt)
            expected = (expected_state, expected_stage, expected_attempt)
            if actual != expected:
                raise CampaignStateChangedError(
                    "campaign state changed while acquiring the attempt lease: "
                    f"expected {expected}, found {actual}"
                )
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
        if target == "waiting_quota" and not (
            state.state == "paused"
            and state.paused_from == "waiting_quota"
            and state.quota_wait is not None
        ):
            raise StateError(
                "use wait_for_quota() to enter waiting_quota from a campaign phase"
            )
        if state.state == "waiting_quota" and target in QUOTA_PHASE_STATES.values():
            raise StateError("use resume_if_quota_ready() to leave waiting_quota")
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
        if state.quota_wait is not None and (
            resolved_stage,
            resolved_attempt,
        ) != (state.quota_wait.stage, state.quota_wait.attempt):
            raise StateError("cannot change stage or attempt while quota is held")
        event = self._new_event(state, target, reason, resolved_stage, resolved_attempt)
        if target == "paused":
            event["paused_from"] = state.state
        clear_quota = (
            state.quota_wait is not None and target in {"halted", "failed"}
        ) or (
            state.state == "paused"
            and state.paused_from == "waiting_quota"
            and target != "waiting_quota"
        )
        if clear_quota:
            event["quota_wait_after"] = None
        if state.resumed_from_quota is not None:
            event["quota_resumed_from"] = None
        self._append_event_locked(event)
        state.state = target
        if target == "paused":
            state.paused_from = str(event["paused_from"])
        state.stage = resolved_stage
        state.attempt = resolved_attempt
        if clear_quota:
            state.quota_wait = None
        state.resumed_from_quota = None
        state.updated_at = str(event["ts"])
        self.save(state)
        return state

    def wait_for_quota(self, checkpoint: QuotaWait) -> CampaignState:
        """Persist a quota checkpoint without spending or changing its attempt."""
        if not isinstance(checkpoint, QuotaWait):
            raise StateError("quota checkpoint must be a QuotaWait")
        with self.lock():
            state = self.load()
            expected_state = QUOTA_PHASE_STATES[checkpoint.suspended_phase]
            prelaunch_rollback = (
                checkpoint.suspended_phase == "pre-worker"
                and state.state == "running"
                and checkpoint.stage == state.stage
                and checkpoint.attempt == state.attempt - 1
            )
            if state.state != expected_state and not prelaunch_rollback:
                raise StateError(
                    f"quota phase '{checkpoint.suspended_phase}' requires "
                    f"state '{expected_state}', found '{state.state}'"
                )
            if not prelaunch_rollback and (
                checkpoint.stage,
                checkpoint.attempt,
            ) != (state.stage, state.attempt):
                raise StateError(
                    "quota checkpoint stage and attempt must match campaign state"
                )
            event = self._new_event(
                state,
                "waiting_quota",
                checkpoint.reason,
                checkpoint.stage,
                checkpoint.attempt,
            )
            event["quota_hold_id"] = checkpoint.hold_id
            event["quota_wait_after"] = checkpoint.to_dict()
            if state.resumed_from_quota is not None:
                event["quota_resumed_from"] = None
            self._append_event_locked(event)
            state.state = "waiting_quota"
            state.stage = checkpoint.stage
            state.attempt = checkpoint.attempt
            state.quota_wait = checkpoint
            state.resumed_from_quota = None
            state.updated_at = str(event["ts"])
            self.save(state)
            return state

    def refresh_quota_wait(self, checkpoint: QuotaWait) -> CampaignState:
        """Replace the current quota generation/reset without leaving its phase."""
        with self.lock():
            state = self.load()
            if state.state != "waiting_quota" or state.quota_wait is None:
                raise StateError("quota refresh requires waiting_quota")
            current = state.quota_wait
            if (checkpoint.stage, checkpoint.attempt, checkpoint.suspended_phase) != (
                current.stage,
                current.attempt,
                current.suspended_phase,
            ):
                raise StateError("quota refresh cannot change continuation identity")
            event = self._new_event(
                state,
                "waiting_quota",
                "quota hold refreshed",
                state.stage,
                state.attempt,
            )
            event["quota_hold_id"] = checkpoint.hold_id
            event["quota_wait_after"] = checkpoint.to_dict()
            self._append_event_locked(event)
            state.quota_wait = checkpoint
            state.updated_at = str(event["ts"])
            self.save(state)
            return state

    def resume_if_quota_ready(
        self,
        now: datetime,
        manual: bool = False,
        expected_hold_id: str | None = None,
    ) -> CampaignState:
        """Restore the exact suspended phase when its reset is due or cleared manually."""
        current_time = _utc_datetime(now, "resume time")
        if not isinstance(manual, bool):
            raise StateError("manual must be a boolean")
        if expected_hold_id is not None:
            expected_hold_id = _required_text(expected_hold_id, "expected_hold_id")
        if manual and expected_hold_id is None:
            raise StateError("manual quota resume requires expected_hold_id")
        with self.lock():
            state = self.load()
            if state.state != "waiting_quota":
                return state
            checkpoint = state.quota_wait
            if checkpoint is None:
                raise StateError("waiting_quota state has no quota checkpoint")
            if manual and expected_hold_id != checkpoint.hold_id:
                return state
            if not manual and (
                checkpoint.reset_at is None or current_time < checkpoint.reset_at
            ):
                return state
            target = QUOTA_PHASE_STATES[checkpoint.suspended_phase]
            validate_transition(state.state, target)
            reason = (
                "quota hold cleared manually"
                if manual
                else "quota reset time reached"
            )
            event = self._new_event(
                state,
                target,
                reason,
                checkpoint.stage,
                checkpoint.attempt,
            )
            event["quota_hold_id"] = checkpoint.hold_id
            event["quota_wait_after"] = None
            event["quota_resumed_from"] = checkpoint.to_dict()
            self._append_event_locked(event)
            state.state = target
            state.stage = checkpoint.stage
            state.attempt = checkpoint.attempt
            state.quota_wait = None
            state.resumed_from_quota = checkpoint
            state.updated_at = str(event["ts"])
            self.save(state)
            return state

    def update_usage(self, delta: Mapping[str, float], reason: str) -> CampaignState:
        with self.lock():
            state = self.load()
            return self._add_usage_locked(state, delta, reason)

    def manual_resume_quota(
        self, now: datetime, expected_hold_id: str
    ) -> QuotaResumeResult:
        """Return a typed mismatch/resume result for an operator request."""
        state = self.load()
        if state.state != "waiting_quota" or state.quota_wait is None:
            return QuotaResumeResult(state, False, "not_waiting")
        if state.quota_wait.hold_id != expected_hold_id:
            return QuotaResumeResult(state, False, "hold_mismatch")
        resumed = self.resume_if_quota_ready(
            now, manual=True, expected_hold_id=expected_hold_id
        )
        return QuotaResumeResult(resumed, True, "resumed")

    def _add_usage_locked(
        self,
        state: CampaignState,
        delta: Mapping[str, float],
        reason: str,
    ) -> CampaignState:
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

    def update_job_usage(
        self,
        attempt_key: str,
        core_hours: float,
        reason: str,
    ) -> CampaignState:
        with self.lock():
            state = self.load()
            previous = float(state.job_core_hours.get(attempt_key, 0))
            if core_hours <= previous:
                return state
            state.job_core_hours[attempt_key] = float(core_hours)
            state.usage["core_hours"] = float(state.usage["core_hours"]) + (
                float(core_hours) - previous
            )
            event = self._new_event(state, state.state, reason, state.stage, state.attempt)
            event["usage_after"] = state.usage
            event["job_core_hours_after"] = state.job_core_hours
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
            if isinstance(last.get("job_core_hours_after"), dict):
                state.job_core_hours = {
                    str(key): float(value)
                    for key, value in last["job_core_hours_after"].items()
                }
            self._apply_quota_event(state, last)
            if last.get("to") == "paused":
                if last.get("paused_from"):
                    state.paused_from = str(last["paused_from"])
            else:
                state.paused_from = None
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
            if isinstance(event.get("job_core_hours_after"), dict):
                state.job_core_hours = {
                    str(key): float(value)
                    for key, value in event["job_core_hours_after"].items()
                }
            self._apply_quota_event(state, event)
            if event.get("to") == "paused":
                if event.get("paused_from"):
                    state.paused_from = str(event["paused_from"])
            else:
                state.paused_from = None
            state.updated_at = str(event.get("ts", state.updated_at))
        return state

    @staticmethod
    def _apply_quota_event(state: CampaignState, event: Mapping[str, Any]) -> None:
        if "quota_wait_after" in event:
            checkpoint = event["quota_wait_after"]
            state.quota_wait = (
                QuotaWait.from_dict(checkpoint) if checkpoint is not None else None
            )
        if "quota_resumed_from" in event:
            resumed = event["quota_resumed_from"]
            state.resumed_from_quota = (
                QuotaWait.from_dict(resumed) if resumed is not None else None
            )

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
            origin = state.paused_from
            target = "queued"
            if origin in {"waiting_job", "validating", "waiting_quota"}:
                target = origin
            elif origin is None and self._has_resumable_job_attempt(state):
                target = "waiting_job"
            elif origin == "running" and self._has_resumable_job_attempt(state):
                target = "waiting_job"
            result = self._transition_locked(target, reason, attempt=state.attempt)
            result.paused_from = None
            self.save(result)
            return result

    def _has_resumable_job_attempt(self, state: CampaignState) -> bool:
        for record in state.jobs.values():
            if not isinstance(record, dict):
                continue
            if (
                record.get("stage") == state.stage
                and record.get("attempt") == state.attempt
                and record.get("status") in {"submitted", "running", "succeeded"}
            ):
                return True
        return False

    def halt(self, reason: str = "operator halt") -> CampaignState:
        with self.lock():
            state = self.load()
            if state.state in TERMINAL_STATES:
                raise StateError(f"campaign is already terminal: {state.state}")
            if state.state == "paused":
                return self._transition_locked("halted", reason)
            return self._transition_locked("halted", reason)

    def fail(self, reason: str) -> CampaignState:
        with self.lock():
            state = self.load()
            if state.state in TERMINAL_STATES:
                raise StateError(f"campaign is already terminal: {state.state}")
            return self._transition_locked("failed", reason)

    def start_approved(self) -> bool:
        try:
            approval = json.loads(
                self.start_approval_path.read_text(encoding="utf-8")
            )
        except (OSError, json.JSONDecodeError):
            return False
        if not isinstance(approval, dict):
            return False
        if not all(
            isinstance(approval.get(key), str) and approval[key].strip()
            for key in ("ts", "approver", "reason")
        ):
            return False
        expected = protocol_sha256(self.verify_protocol())
        return approval.get("protocol_sha256") == expected

    def approve_start(
        self,
        approver: str,
        reason: str,
        minutes: float = 0,
    ) -> CampaignState:
        if minutes < 0:
            raise StateError("minutes must be >= 0")
        if not approver.strip() or not reason.strip():
            raise StateError("approver and reason must not be empty")
        with self.lock():
            state = self.load()
            if state.state != "queued":
                raise StateError("startup approval is only valid for a queued campaign")
            if self.start_approved():
                raise StateError("startup is already approved")
            approval = {
                "ts": utc_now(),
                "approver": approver,
                "reason": reason,
                "minutes": float(minutes),
                "stage": state.stage,
                "attempt": state.attempt,
                "protocol_sha256": protocol_sha256(self.verify_protocol()),
            }
            _atomic_write_json(self.start_approval_path, approval)
            if minutes:
                state = self._add_usage_locked(
                    state,
                    {"human_minutes": float(minutes)},
                    f"startup approval by {approver}",
                )
            return state

    def approve(
        self,
        approver: str,
        reason: str,
        minutes: float = 0,
    ) -> CampaignState:
        if minutes < 0:
            raise StateError("minutes must be >= 0")
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
                "minutes": float(minutes),
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
                result = self._transition_locked(
                    "completed",
                    f"approved by {approver}: {reason}",
                    allow_human_exit=True,
                )
            elif stage.type == "human":
                result = self._transition_locked(
                    "queued",
                    f"approved by {approver}: {reason}",
                    stage=next_stage,
                    attempt=0,
                    allow_human_exit=True,
                )
            else:
                result = self._transition_locked(
                    "queued",
                    f"approved by {approver}: {reason}",
                    stage=state.stage,
                    attempt=state.attempt,
                    allow_human_exit=True,
                )
            if minutes:
                result = self._add_usage_locked(
                    result,
                    {"human_minutes": float(minutes)},
                    f"human approval by {approver}",
                )
            return result
