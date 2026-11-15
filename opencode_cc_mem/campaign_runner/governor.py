"""Cross-process usage admission, cooldown, and invocation accounting."""

from __future__ import annotations

import fcntl
import json
import os
import tempfile
import time as system_time
from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import date, datetime, time, timedelta, timezone
from pathlib import Path
from typing import Any, Callable, Iterator, Mapping

import yaml

from .executors.contracts import RunOutcome, SessionIdentity, UsageRecord
from .routing import ResolvedRoute


DEFAULT_MAX_CONCURRENT = 2
DEFAULT_MAX_RUNS_PER_DAY = 50
DEFAULT_MAX_TURNS_PER_RUN = 60
DEFAULT_MIN_SECONDS_BETWEEN_RUNS = 30
POLICY_KEYS = frozenset(
    {
        "max_concurrent",
        "max_runs_per_day",
        "max_turns_per_run",
        "min_seconds_between_runs",
        "max_price_per_run",
        "api_key_providers",
    }
)


class PolicyError(ValueError):
    """Raised when a configured usage policy cannot be used safely."""


@dataclass(frozen=True)
class Policy:
    """Limits applied independently to every subscription key."""

    max_concurrent: int = DEFAULT_MAX_CONCURRENT
    max_runs_per_day: int = DEFAULT_MAX_RUNS_PER_DAY
    max_turns_per_run: int = DEFAULT_MAX_TURNS_PER_RUN
    min_seconds_between_runs: int = DEFAULT_MIN_SECONDS_BETWEEN_RUNS
    max_price_per_run: float | None = None
    api_key_providers: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        for name in (
            "max_concurrent",
            "max_runs_per_day",
            "max_turns_per_run",
            "min_seconds_between_runs",
        ):
            value = getattr(self, name)
            if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
                raise PolicyError(f"{name} must be a positive integer")
        price = self.max_price_per_run
        if price is not None and (
            isinstance(price, bool) or not isinstance(price, (int, float)) or price <= 0
        ):
            raise PolicyError("max_price_per_run must be positive when configured")
        providers = self.api_key_providers
        if isinstance(providers, list):
            providers = tuple(providers)
            object.__setattr__(self, "api_key_providers", providers)
        if not isinstance(providers, tuple) or any(
            not isinstance(item, str) or not item.strip() for item in providers
        ):
            raise PolicyError("api_key_providers must be an explicit list of names")

    @classmethod
    def load(cls, path: str | Path | None = None) -> "Policy":
        """Load policy, using defaults only when the default path is absent."""
        explicit = path is not None
        policy_path = Path(path).expanduser() if explicit else _default_policy_path()
        if not policy_path.exists():
            if explicit:
                raise PolicyError(f"usage policy does not exist: {policy_path}")
            return cls()
        try:
            payload = yaml.safe_load(policy_path.read_text(encoding="utf-8"))
        except (OSError, yaml.YAMLError) as exc:
            raise PolicyError(f"cannot read usage policy {policy_path}: {exc}") from exc
        if not isinstance(payload, Mapping):
            raise PolicyError("usage policy must be a mapping")
        unknown = sorted(set(payload) - POLICY_KEYS)
        if unknown:
            raise PolicyError("unknown usage policy fields: " + ", ".join(unknown))
        values = dict(payload)
        providers = values.get("api_key_providers", [])
        if not isinstance(providers, list):
            raise PolicyError("api_key_providers must be an explicit list")
        values["api_key_providers"] = tuple(providers)
        try:
            return cls(**values)
        except (TypeError, PolicyError) as exc:
            raise PolicyError(f"invalid usage policy {policy_path}: {exc}") from exc


@dataclass(frozen=True)
class AdmissionRequest:
    """Identity and cap for exactly one prospective physical invocation."""

    route: ResolvedRoute
    invocation_id: str
    parent_run_id: str | None
    project_digest: str
    run_kind: str
    role_cap: int | None = None
    session_identity: SessionIdentity | None = None

    def __post_init__(self) -> None:
        if not self.invocation_id or not self.project_digest or not self.run_kind:
            raise ValueError("invocation, project, and run-kind identities are required")
        if not self.route.subscription_key:
            raise ValueError("governed invocation requires a subscription key")
        if self.role_cap is not None and (
            isinstance(self.role_cap, bool) or self.role_cap <= 0
        ):
            raise ValueError("role cap must be positive")


@dataclass(frozen=True)
class QuotaWaiting:
    """A durable refusal to launch until the named condition changes."""

    key: str
    caller_id: str
    reason: str
    reset_at: datetime | None = None


@dataclass
class Admission:
    """Exception-safe ownership of one reserved physical invocation."""

    governor: "Governor"
    request: AdmissionRequest
    effective_cap: int | None
    billing: str
    _finalized: bool = field(default=False, init=False, repr=False)

    def __enter__(self) -> "Admission":
        return self

    def __exit__(self, exc_type: Any, exc: Any, traceback: Any) -> bool:
        if self._finalized:
            return False
        if not self.governor._was_launched(self.request.invocation_id):
            self.cancel_before_launch()
            return False
        kind = "failure" if exc_type else "success"
        self.release(outcome=RunOutcome(kind))
        return False

    def mark_started(
        self,
        *,
        pid: int,
        pgid: int,
        process_start_token: str | None = None,
        service_request_id: str | None = None,
        started_at: datetime | None = None,
    ) -> "Admission":
        """Attach the actual process and service identity to this reservation."""
        self.governor._mark_started(
            self.request.invocation_id,
            pid=pid,
            pgid=pgid,
            process_start_token=process_start_token or _process_start_token(pid),
            service_request_id=service_request_id,
            started_at=started_at,
        )
        return self

    record_started = mark_started

    def cancel_before_launch(self) -> bool:
        """Cancel only a reservation that demonstrably never spawned work."""
        cancelled = self.governor._cancel_before_launch(self.request.invocation_id)
        self._finalized = self._finalized or cancelled
        return cancelled

    def release(
        self,
        *,
        usage: UsageRecord | None = None,
        outcome: RunOutcome | None = None,
    ) -> bool:
        """Persist one completion; repeated releases are harmless."""
        if self._finalized:
            return False
        finalized = self.governor._finalize(
            self.request.invocation_id,
            usage=usage,
            outcome=outcome or RunOutcome("success"),
        )
        self._finalized = self._finalized or finalized
        return finalized

    complete = release


class Governor:
    """Coordinate subscription reservations and a privacy-bounded ledger."""

    def __init__(
        self,
        policy: Policy | None = None,
        *,
        ledger_path: str | Path | None = None,
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        self.policy = policy or Policy.load()
        self.ledger_path = Path(ledger_path) if ledger_path else _default_ledger_path()
        self.state_path = self.ledger_path.with_name("usage-state.json")
        self.lock_path = self.ledger_path.with_name("usage-state.lock")
        self.clock = clock or (lambda: datetime.now(timezone.utc))

    def admit(self, request: AdmissionRequest, control: Any = None) -> Admission | QuotaWaiting:
        """Atomically reserve one invocation or persist why its caller waits."""
        stopped = _control_reason(control)
        if stopped:
            return self._record_wait(request, "cancelled", None)
        now = self._now()
        key = request.route.subscription_key
        assert key is not None
        billing = "api" if self._is_api_provider(key) else "subscription"
        with self._locked() as state:
            self._recover_pending(state)
            self._clear_due_cooldown(state, key, now)
            existing = state["reservations"].get(request.invocation_id)
            if existing:
                return Admission(
                    self,
                    request,
                    existing.get("effective_cap"),
                    existing["billing"],
                )
            if request.invocation_id in state["completed"]:
                raise ValueError(f"invocation already completed: {request.invocation_id}")
            if billing == "subscription":
                waiting = self._subscription_wait(state, request, now)
                if waiting:
                    self._put_wait(state, waiting)
                    return waiting
            effective_cap = request.role_cap
            if billing == "subscription":
                configured = request.role_cap or self.policy.max_turns_per_run
                effective_cap = min(configured, self.policy.max_turns_per_run)
            reservation = self._reservation(request, now, billing, effective_cap)
            state["reservations"][request.invocation_id] = reservation
            self._remove_wait(state, request.invocation_id)
        return Admission(self, request, effective_cap, billing)

    def resume_key(self, key: str) -> bool:
        """Clear an observed quota hold without changing daily accounting."""
        with self._locked() as state:
            self._recover_pending(state)
            removed = state["cooldowns"].pop(key, None) is not None
            if removed:
                for wait_id, wait in tuple(state["waits"].items()):
                    if wait.get("key") == key and wait.get("reason") == "quota":
                        del state["waits"][wait_id]
            return removed

    def wait(
        self,
        waiting: QuotaWaiting,
        control: Any = None,
        *,
        poll_interval: float = 1.0,
        sleeper: Callable[[float], None] = system_time.sleep,
    ) -> bool:
        """Wait for a timed hold while checking caller cancellation each poll."""
        if poll_interval <= 0:
            raise ValueError("poll interval must be positive")
        while self._wait_is_active(waiting):
            if _control_reason(control):
                return False
            delay = poll_interval
            if waiting.reset_at is not None:
                delay = min(delay, (waiting.reset_at - self._now()).total_seconds())
            sleeper(max(delay, 0.0))
        return True

    def _wait_is_active(self, waiting: QuotaWaiting) -> bool:
        now = self._now()
        with self._locked() as state:
            self._recover_pending(state)
            self._clear_due_cooldown(state, waiting.key, now)
            if waiting.reason == "quota":
                active = waiting.key in state["cooldowns"]
            elif waiting.reason == "concurrency":
                active = sum(
                    item["key"] == waiting.key
                    for item in state["reservations"].values()
                ) >= self.policy.max_concurrent
            else:
                active = waiting.reset_at is None or now < waiting.reset_at
            if not active:
                state["waits"].pop(_wait_id(waiting.key, waiting.caller_id), None)
            return active

    def reconcile(
        self,
        invocation_id: str,
        *,
        process_ended: bool,
        service_request_ended: bool,
    ) -> bool:
        """Finalize an abandoned hold only after local and service work ended."""
        if not process_ended or not service_request_ended:
            return False
        with self._locked() as state:
            self._recover_pending(state)
            reservation = state["reservations"].get(invocation_id)
            if not reservation or not reservation.get("launched"):
                return False
        return self._finalize(
            invocation_id,
            usage=None,
            outcome=RunOutcome("failure"),
        )

    def report(self, day: date) -> dict[str, Any]:
        """Return selected-day, seven-day, and current-wait accounting."""
        with self._locked() as state:
            self._recover_pending(state)
            waits = list(state["waits"].values())
            rows = self._ledger_rows()
        first_day = day - timedelta(days=6)
        selected = [row for row in rows if _row_day(row) == day]
        week = [row for row in rows if first_day <= _row_day(row) <= day]
        by_day = []
        for offset in range(7):
            current = first_day + timedelta(days=offset)
            by_day.append({"day": current.isoformat(), "runs": sum(_row_day(row) == current for row in week)})
        return {
            "day": {"date": day.isoformat(), "runs": len(selected)},
            "week": {"from": first_day.isoformat(), "through": day.isoformat(), "runs": len(week), "days": by_day},
            "waits": sorted(waits, key=lambda item: (item["key"], item["caller_id"])),
        }

    def _subscription_wait(
        self, state: dict[str, Any], request: AdmissionRequest, now: datetime
    ) -> QuotaWaiting | None:
        key = request.route.subscription_key
        assert key is not None
        cooldown = state["cooldowns"].get(key)
        if cooldown:
            reset = cooldown.get("reset_at")
            return QuotaWaiting(
                key,
                request.invocation_id,
                "quota",
                _parse_time(reset) if reset else None,
            )
        active = [item for item in state["reservations"].values() if item["key"] == key]
        if len(active) >= self.policy.max_concurrent:
            return QuotaWaiting(key, request.invocation_id, "concurrency")
        today = now.date()
        completed = sum(
            row.get("subscription_key") == key and _row_day(row) == today
            for row in self._ledger_rows()
        )
        reserved = sum(_parse_time(item["reserved_at"]).date() == today for item in active)
        if completed + reserved >= self.policy.max_runs_per_day:
            reset = datetime.combine(today + timedelta(days=1), time.min, timezone.utc)
            return QuotaWaiting(key, request.invocation_id, "daily_limit", reset)
        starts = [_parse_time(item["started_at"] or item["reserved_at"]) for item in active]
        starts.extend(
            _parse_time(row["started_at"])
            for row in self._ledger_rows()
            if row.get("subscription_key") == key and row.get("started_at")
        )
        if starts:
            reset = max(starts) + timedelta(seconds=self.policy.min_seconds_between_runs)
            if now < reset:
                return QuotaWaiting(key, request.invocation_id, "spacing", reset)
        return None

    def _reservation(
        self,
        request: AdmissionRequest,
        now: datetime,
        billing: str,
        effective_cap: int | None,
    ) -> dict[str, Any]:
        return {
            "invocation_id": request.invocation_id,
            "parent_run_id": request.parent_run_id,
            "project_digest": request.project_digest,
            "run_kind": request.run_kind,
            "client": request.route.client,
            "model": request.route.model,
            "key": request.route.subscription_key,
            "billing": billing,
            "effective_cap": effective_cap,
            "reserved_at": now.isoformat(),
            "started_at": None,
            "launched": False,
            "pid": None,
            "pgid": None,
            "process_start_token": None,
            "service_request_id": None,
            "session_identity": _session_dict(request.session_identity),
        }

    def _mark_started(self, invocation_id: str, **identity: Any) -> None:
        started_at = identity.pop("started_at", None) or self._now()
        if not identity.get("process_start_token"):
            raise ValueError("process start token is required to prevent PID reuse")
        with self._locked() as state:
            self._recover_pending(state)
            reservation = state["reservations"].get(invocation_id)
            if not reservation:
                if invocation_id in state["completed"]:
                    return
                raise ValueError(f"unknown invocation reservation: {invocation_id}")
            if reservation["launched"]:
                return
            reservation.update(identity)
            reservation["started_at"] = _utc(started_at).isoformat()
            reservation["launched"] = True

    def _cancel_before_launch(self, invocation_id: str) -> bool:
        with self._locked() as state:
            self._recover_pending(state)
            reservation = state["reservations"].get(invocation_id)
            if not reservation or reservation.get("launched"):
                return False
            del state["reservations"][invocation_id]
            return True

    def _was_launched(self, invocation_id: str) -> bool:
        with self._locked() as state:
            self._recover_pending(state)
            reservation = state["reservations"].get(invocation_id)
            return bool(reservation and reservation.get("launched"))

    def _finalize(
        self,
        invocation_id: str,
        *,
        usage: UsageRecord | None,
        outcome: RunOutcome,
    ) -> bool:
        with self._locked() as state:
            self._recover_pending(state)
            if invocation_id in state["completed"]:
                return False
            reservation = state["reservations"].get(invocation_id)
            if not reservation or not reservation.get("launched"):
                return False
            record = self._completion_record(reservation, usage, outcome)
            state["pending_completions"][invocation_id] = record
            if outcome.kind == "quota":
                limit = outcome.limit
                state["cooldowns"][reservation["key"]] = {
                    "reason": limit.reason if limit else "quota or rate limit",
                    "reset_at": limit.reset_at.isoformat() if limit and limit.reset_at else None,
                    "observed_at": self._now().isoformat(),
                }
            self._save_state(state)
            self._append_once(record)
            state["completed"][invocation_id] = record["ended_at"]
            state["pending_completions"].pop(invocation_id, None)
            state["reservations"].pop(invocation_id, None)
            return True

    def _completion_record(
        self,
        reservation: Mapping[str, Any],
        usage: UsageRecord | None,
        outcome: RunOutcome,
    ) -> dict[str, Any]:
        usage_values = None
        if usage is not None:
            usage_values = {
                name: getattr(usage, name)
                for name in (
                    "turns",
                    "input_tokens",
                    "output_tokens",
                    "reasoning_tokens",
                    "cached_tokens",
                    "cache_write_tokens",
                    "cost",
                    "duration_seconds",
                    "source",
                    "completeness",
                )
            }
        limit = outcome.limit
        return {
            "schema_version": "magnolia-invocation-v1",
            "invocation_id": reservation["invocation_id"],
            "parent_run_id": reservation["parent_run_id"],
            "started_at": reservation["started_at"],
            "ended_at": self._now().isoformat(),
            "client": reservation["client"],
            "resolved_model": reservation["model"],
            "subscription_key": reservation["key"],
            "billing": reservation["billing"],
            "run_kind": reservation["run_kind"],
            "project_digest": reservation["project_digest"],
            "effective_cap": reservation["effective_cap"],
            "actual_work": {
                "pid": reservation["pid"],
                "pgid": reservation["pgid"],
                "process_start_token": reservation["process_start_token"],
                "service_request_id": reservation["service_request_id"],
            },
            "session_identity": reservation["session_identity"],
            "usage": usage_values,
            "outcome": {
                "kind": outcome.kind,
                "limit": {
                    "reason": limit.reason,
                    "reset_at": limit.reset_at.isoformat() if limit.reset_at else None,
                    "evidence_source": limit.evidence_source,
                } if limit else None,
            },
        }

    def _record_wait(
        self, request: AdmissionRequest, reason: str, reset_at: datetime | None
    ) -> QuotaWaiting:
        key = request.route.subscription_key
        assert key is not None
        waiting = QuotaWaiting(key, request.invocation_id, reason, reset_at)
        with self._locked() as state:
            self._recover_pending(state)
            self._put_wait(state, waiting)
        return waiting

    def _put_wait(self, state: dict[str, Any], waiting: QuotaWaiting) -> None:
        state["waits"][_wait_id(waiting.key, waiting.caller_id)] = {
            "key": waiting.key,
            "caller_id": waiting.caller_id,
            "reason": waiting.reason,
            "reset_at": waiting.reset_at.isoformat() if waiting.reset_at else None,
            "observed_at": self._now().isoformat(),
        }

    def _remove_wait(self, state: dict[str, Any], caller_id: str) -> None:
        for wait_id, wait in tuple(state["waits"].items()):
            if wait.get("caller_id") == caller_id:
                del state["waits"][wait_id]

    def _clear_due_cooldown(self, state: dict[str, Any], key: str, now: datetime) -> None:
        cooldown = state["cooldowns"].get(key)
        if not cooldown or not cooldown.get("reset_at"):
            return
        if now >= _parse_time(cooldown["reset_at"]):
            del state["cooldowns"][key]

    def _recover_pending(self, state: dict[str, Any]) -> None:
        for invocation_id, record in tuple(state["pending_completions"].items()):
            self._append_once(record)
            state["completed"][invocation_id] = record["ended_at"]
            state["reservations"].pop(invocation_id, None)
            del state["pending_completions"][invocation_id]

    def _append_once(self, record: Mapping[str, Any]) -> None:
        if record["invocation_id"] in {
            row.get("invocation_id") for row in self._ledger_rows()
        }:
            return
        self.ledger_path.parent.mkdir(parents=True, exist_ok=True)
        with self.ledger_path.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(record, sort_keys=True, separators=(",", ":")) + "\n")
            handle.flush()
            os.fsync(handle.fileno())

    def _ledger_rows(self) -> list[dict[str, Any]]:
        try:
            lines = self.ledger_path.read_text(encoding="utf-8").splitlines()
        except FileNotFoundError:
            return []
        rows = []
        for line_number, line in enumerate(lines, start=1):
            try:
                value = json.loads(line)
            except json.JSONDecodeError as exc:
                raise RuntimeError(
                    f"invalid usage ledger row {line_number}: {exc}"
                ) from exc
            if not isinstance(value, dict):
                raise RuntimeError(f"invalid usage ledger row {line_number}")
            rows.append(value)
        return rows

    def _is_api_provider(self, key: str) -> bool:
        if not key.startswith("opencode:"):
            return False
        return key.split(":", 1)[1] in self.policy.api_key_providers

    def _now(self) -> datetime:
        return _utc(self.clock())

    @contextmanager
    def _locked(self) -> Iterator[dict[str, Any]]:
        self.ledger_path.parent.mkdir(parents=True, exist_ok=True)
        with self.lock_path.open("a+b") as lock:
            fcntl.flock(lock.fileno(), fcntl.LOCK_EX)
            state = self._load_state()
            try:
                yield state
            finally:
                self._save_state(state)
                fcntl.flock(lock.fileno(), fcntl.LOCK_UN)

    def _load_state(self) -> dict[str, Any]:
        try:
            state = json.loads(self.state_path.read_text(encoding="utf-8"))
        except FileNotFoundError:
            state = {}
        except json.JSONDecodeError as exc:
            raise RuntimeError(f"invalid usage governor state: {exc}") from exc
        if not isinstance(state, dict):
            raise RuntimeError("invalid usage governor state")
        state.setdefault("version", 1)
        for name in ("reservations", "cooldowns", "waits", "pending_completions", "completed"):
            state.setdefault(name, {})
            if not isinstance(state[name], dict):
                raise RuntimeError(f"invalid usage governor state section: {name}")
        return state

    def _save_state(self, state: Mapping[str, Any]) -> None:
        self.state_path.parent.mkdir(parents=True, exist_ok=True)
        body = json.dumps(state, sort_keys=True, separators=(",", ":")) + "\n"
        descriptor, temporary = tempfile.mkstemp(
            prefix=".usage-state-", dir=self.state_path.parent
        )
        try:
            with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
                handle.write(body)
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temporary, self.state_path)
        finally:
            try:
                os.unlink(temporary)
            except FileNotFoundError:
                pass


def _default_policy_path() -> Path:
    root = Path(os.environ.get("XDG_CONFIG_HOME", Path.home() / ".config"))
    return root / "magnolia" / "usage-policy.yaml"


def _default_ledger_path() -> Path:
    root = Path(os.environ.get("XDG_STATE_HOME", Path.home() / ".local" / "state"))
    return root / "magnolia" / "usage-ledger.jsonl"


def _utc(value: datetime) -> datetime:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError("governor clock must return a timezone-aware datetime")
    return value.astimezone(timezone.utc)


def _parse_time(value: str | None) -> datetime:
    if not value:
        raise ValueError("missing timestamp")
    return _utc(datetime.fromisoformat(value))


def _row_day(row: Mapping[str, Any]) -> date:
    return _parse_time(str(row["started_at"])).date()


def _wait_id(key: str, caller_id: str) -> str:
    return f"{key}\x1f{caller_id}"


def _session_dict(identity: SessionIdentity | None) -> dict[str, Any] | None:
    if identity is None:
        return None
    return {
        "client": identity.client,
        "model": identity.model,
        "session_id": identity.session_id,
        "project_digest": identity.project_digest,
        "role": identity.role,
        "policy_digest": identity.policy_digest,
    }


def _control_reason(control: Any) -> str | None:
    callback = getattr(control, "should_stop", None) if control is not None else None
    if not callable(callback):
        return None
    try:
        return callback()
    except Exception as exc:
        return f"stop check failed: {exc}"


def _process_start_token(pid: int) -> str:
    try:
        body = Path(f"/proc/{pid}/stat").read_text(encoding="utf-8")
        fields = body.rsplit(")", 1)[1].split()
        return f"{pid}:{fields[19]}"
    except (OSError, IndexError):
        return f"{pid}:{system_time.time_ns()}"
