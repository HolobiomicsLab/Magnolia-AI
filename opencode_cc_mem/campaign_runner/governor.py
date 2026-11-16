"""Cross-process usage admission, cooldown, and invocation accounting."""

from __future__ import annotations

import errno
import fcntl
import json
import math
import os
import re
import subprocess
import tempfile
import time as system_time
import uuid
from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import date, datetime, time, timedelta, timezone
from pathlib import Path
from typing import Any, Callable, Iterator, Mapping

from compchem_memory.process_identity import (
    _proc_stat_fields,
    verified_process_start_token as _verified_process_start_token,
)

from .executors.contracts import LimitInfo, RunOutcome, SessionIdentity, UsageRecord
from .routing import ResolvedRoute


DEFAULT_MAX_CONCURRENT = 2
DEFAULT_MAX_RUNS_PER_DAY = 50
DEFAULT_MAX_TURNS_PER_RUN = 60
DEFAULT_MIN_SECONDS_BETWEEN_RUNS = 30
UNVERIFIED_OWNER_TTL_SECONDS = 600
POLICY_KEYS = frozenset(
    {
        "max_concurrent",
        "max_runs_per_day",
        "max_turns_per_run",
        "min_seconds_between_runs",
        "client_native_budgets",
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
    client_native_budgets: tuple[tuple[str, float], ...] = ()
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
            isinstance(price, bool)
            or not isinstance(price, (int, float))
            or not math.isfinite(price)
            or price <= 0
        ):
            raise PolicyError(
                "max_price_per_run must be positive and finite when configured"
            )
        providers = self.api_key_providers
        if isinstance(providers, list):
            providers = tuple(providers)
            object.__setattr__(self, "api_key_providers", providers)
        if not isinstance(providers, tuple) or any(
            not isinstance(item, str) or not item.strip() for item in providers
        ):
            raise PolicyError("api_key_providers must be an explicit list of names")
        budgets = self.client_native_budgets
        if isinstance(budgets, Mapping):
            budgets = tuple(budgets.items())
            object.__setattr__(self, "client_native_budgets", budgets)
        if not isinstance(budgets, tuple):
            raise PolicyError("client_native_budgets must be a mapping")
        for item in budgets:
            if not isinstance(item, tuple) or len(item) != 2:
                raise PolicyError("client_native_budgets must be a mapping")
            client, budget = item
            if client not in {"claude-code", "vibe"}:
                raise PolicyError(
                    "client_native_budgets supports only claude-code and vibe"
                )
            if (
                isinstance(budget, bool)
                or not isinstance(budget, (int, float))
                or not math.isfinite(budget)
                or budget <= 0
            ):
                raise PolicyError(
                    "client-native budgets must be positive and finite"
                )

    def client_budget(self, client: str) -> float | None:
        """Return a proved native monetary cap; Codex has no such flag."""
        return dict(self.client_native_budgets).get(client)

    @classmethod
    def load(cls, path: str | Path | None = None) -> "Policy":
        """Load policy, using defaults only when the default path is absent."""
        explicit = path is not None
        policy_path = Path(path).expanduser() if explicit else _default_policy_path()
        if not policy_path.exists():
            if policy_path.is_symlink():
                raise PolicyError(f"usage policy is a dangling symlink: {policy_path}")
            if explicit:
                raise PolicyError(f"usage policy does not exist: {policy_path}")
            return cls()
        try:
            import yaml
        except ImportError as exc:
            raise PolicyError("PyYAML is required to read a usage policy") from exc
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
        budgets = values.get("client_native_budgets", {})
        if not isinstance(budgets, Mapping):
            raise PolicyError("client_native_budgets must be a mapping")
        values["client_native_budgets"] = tuple(budgets.items())
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
    hold_id: str | None = None


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

    def _cancel_before_client_exec(self) -> bool:
        """Cancel after a governed launcher is reaped before client exec."""
        cancelled = self.governor._cancel_before_client_exec(
            self.request.invocation_id
        )
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
        sleeper: Callable[[float], None] | None = None,
    ) -> None:
        self.policy = policy or Policy.load()
        self.ledger_path = Path(ledger_path) if ledger_path else _default_ledger_path()
        self.state_path = self.ledger_path.with_name("usage-state.json")
        self.lock_path = self.ledger_path.with_name("usage-state.lock")
        self.clock = clock or (lambda: datetime.now(timezone.utc))
        self.sleeper = sleeper or system_time.sleep

    def admit(self, request: AdmissionRequest, control: Any = None) -> Admission | QuotaWaiting:
        """Atomically reserve one invocation or persist why its caller waits."""
        stopped = _control_reason(control)
        if stopped:
            key = request.route.subscription_key
            assert key is not None
            return QuotaWaiting(key, request.invocation_id, "cancelled")
        now = self._now()
        owner = _owner_identity(now)
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
                    self._put_wait(state, waiting, owner)
                    return waiting
            effective_cap = request.role_cap
            if billing == "subscription":
                configured = request.role_cap or self.policy.max_turns_per_run
                effective_cap = min(configured, self.policy.max_turns_per_run)
            reservation = self._reservation(
                request, now, billing, effective_cap, owner
            )
            state["reservations"][request.invocation_id] = reservation
            self._remove_wait(state, request.invocation_id)
        return Admission(self, request, effective_cap, billing)

    def resume_key(self, key: str, expected_hold: str | None = None) -> bool:
        """Clear an observed quota hold without changing daily accounting."""
        with self._locked() as state:
            self._recover_pending(state)
            current = state["cooldowns"].get(key)
            if current is None:
                return False
            if current.get("reason") == "daily_limit":
                return False
            if expected_hold is not None and current.get("generation") != expected_hold:
                return False
            removed = state["cooldowns"].pop(key, None) is not None
            if removed:
                for wait_id, wait in tuple(state["waits"].items()):
                    if wait.get("key") == key and wait.get("reason") == "quota":
                        del state["waits"][wait_id]
            return removed

    def quota_wait(self, key: str, caller_id: str = "campaign") -> QuotaWaiting | None:
        """Return the current quota generation and reset as one atomic snapshot."""
        with self._locked() as state:
            self._recover_pending(state)
            self._clear_due_cooldown(state, key, self._now())
            current = state["cooldowns"].get(key)
            if current is None:
                return None
            reset = current.get("reset_at")
            return QuotaWaiting(
                key,
                caller_id,
                "quota",
                _parse_time(reset) if reset else None,
                str(current["generation"]),
            )

    def checkpoint_limit(self, key: str, limit: LimitInfo) -> QuotaWaiting:
        """Persist a typed client limit as a shared durable quota generation."""
        now = self._now()
        reset_at = limit.reset_at
        if limit.reason == "daily_limit" and reset_at is None:
            reset_at = _next_daily_reset(now)
        observed = {
            "reason": limit.reason,
            "reset_at": reset_at.isoformat() if reset_at else None,
            "observed_at": now.isoformat(),
            "generation": limit.hold_id or uuid.uuid4().hex,
        }
        with self._locked() as state:
            current = state["cooldowns"].get(key)
            stored = _merge_cooldown(current, observed)
            state["cooldowns"][key] = stored
        hold_id = str(stored["generation"])
        reset = stored.get("reset_at")
        return QuotaWaiting(
            key,
            "campaign",
            "quota",
            _parse_time(reset) if reset else None,
            hold_id,
        )

    def wait(
        self,
        waiting: QuotaWaiting,
        control: Any = None,
        *,
        poll_interval: float = 1.0,
        sleeper: Callable[[float], None] | None = None,
    ) -> bool:
        """Wait for a timed hold while checking caller cancellation each poll."""
        if poll_interval <= 0:
            raise ValueError("poll interval must be positive")
        sleep = sleeper or self.sleeper
        try:
            while self._wait_is_active(waiting):
                if _control_reason(control):
                    self.cancel_wait(waiting)
                    return False
                delay = poll_interval
                if waiting.reset_at is not None:
                    delay = min(delay, (waiting.reset_at - self._now()).total_seconds())
                sleep(max(delay, 0.0))
            return True
        except BaseException:
            self.cancel_wait(waiting)
            raise

    def cancel_wait(self, waiting: QuotaWaiting) -> bool:
        """Remove a wait record when its caller gives up or is cancelled."""
        with self._locked() as state:
            return state["waits"].pop(
                _wait_id(waiting.key, waiting.caller_id), None
            ) is not None

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
        process_ended: bool | None = None,
        service_request_ended: bool,
    ) -> bool:
        """Finalize an abandoned hold only after local and service work ended."""
        with self._locked() as state:
            self._recover_pending(state)
            reservation = state["reservations"].get(invocation_id)
            if not reservation:
                return False
            reservation = dict(reservation)
        if not reservation.get("launched"):
            if not _reservation_owner_gone(reservation, self._now()):
                return False
            with self._locked() as state:
                current = state["reservations"].get(invocation_id)
                if current != reservation or current.get("launched"):
                    return False
                del state["reservations"][invocation_id]
                self._remove_wait(state, invocation_id)
                return True
        if process_ended is None:
            process_ended = not _reservation_process_alive(reservation)
        if not process_ended or not service_request_ended:
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
            for key in tuple(state["cooldowns"]):
                self._clear_due_cooldown(state, key, self._now())
            waits = list(state["waits"].values())
            holds = [
                {"key": key, **hold}
                for key, hold in state["cooldowns"].items()
            ]
            rows = self._ledger_rows()
        first_day = day - timedelta(days=6)
        selected = [row for row in rows if _row_day(row) == day]
        week = [row for row in rows if first_day <= _row_day(row) <= day]
        by_day = []
        for offset in range(7):
            current = first_day + timedelta(days=offset)
            by_day.append({"day": current.isoformat(), "runs": sum(_row_day(row) == current for row in week)})
        return {
            "day": {
                "date": day.isoformat(),
                **_usage_summary(selected),
            },
            "week": {
                "from": first_day.isoformat(),
                "through": day.isoformat(),
                **_usage_summary(week),
                "days": by_day,
            },
            "waits": sorted(waits, key=lambda item: (item["key"], item["caller_id"])),
            "holds": sorted(holds, key=lambda item: item["key"]),
        }

    def _subscription_wait(
        self, state: dict[str, Any], request: AdmissionRequest, now: datetime
    ) -> QuotaWaiting | None:
        key = request.route.subscription_key
        assert key is not None
        cooldown = state["cooldowns"].get(key)
        if cooldown:
            reset = cooldown.get("reset_at")
            reason = str(cooldown.get("reason") or "quota")
            return QuotaWaiting(
                key,
                request.invocation_id,
                reason if reason == "daily_limit" else "quota",
                _parse_time(reset) if reset else None,
                str(cooldown["generation"]),
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
            reset = _next_daily_reset(now)
            hold = {
                "reason": "daily_limit",
                "reset_at": reset.isoformat(),
                "observed_at": now.isoformat(),
                "generation": uuid.uuid4().hex,
            }
            stored = _merge_cooldown(state["cooldowns"].get(key), hold)
            state["cooldowns"][key] = stored
            return QuotaWaiting(
                key,
                request.invocation_id,
                "daily_limit",
                _parse_time(stored["reset_at"]),
                str(stored["generation"]),
            )
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
        owner: Mapping[str, Any],
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
            "owner_pid": owner["pid"],
            "owner_start_token": owner["start_token"],
            "owner_expires_at": owner["expires_at"],
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

    def _cancel_before_client_exec(self, invocation_id: str) -> bool:
        with self._locked() as state:
            self._recover_pending(state)
            if invocation_id not in state["reservations"]:
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
                observed = {
                    "reason": limit.reason if limit else "quota or rate limit",
                    "reset_at": limit.reset_at.isoformat() if limit and limit.reset_at else None,
                    "observed_at": self._now().isoformat(),
                    "generation": uuid.uuid4().hex,
                }
                current = state["cooldowns"].get(reservation["key"])
                state["cooldowns"][reservation["key"]] = _merge_cooldown(
                    current, observed
                )
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
            "reserved_at": reservation["reserved_at"],
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
            "session_identity": _session_dict(usage.session_identity)
            if usage is not None and usage.session_identity is not None
            else reservation["session_identity"],
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

    def _put_wait(
        self,
        state: dict[str, Any],
        waiting: QuotaWaiting,
        owner: Mapping[str, Any],
    ) -> None:
        state["waits"][_wait_id(waiting.key, waiting.caller_id)] = {
            "key": waiting.key,
            "caller_id": waiting.caller_id,
            "reason": waiting.reason,
            "reset_at": waiting.reset_at.isoformat() if waiting.reset_at else None,
            "hold_id": waiting.hold_id,
            "observed_at": self._now().isoformat(),
            "owner_pid": owner["pid"],
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
            for wait_id, wait in tuple(state["waits"].items()):
                if wait.get("key") == key and wait.get("reason") == "quota":
                    del state["waits"][wait_id]

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
            body = self.ledger_path.read_bytes()
        except FileNotFoundError:
            return []
        if not body:
            return []
        has_torn_tail = not body.endswith(b"\n")
        chunks = body.split(b"\n")
        if not has_torn_tail:
            chunks = chunks[:-1]
        rows = []
        for index, encoded in enumerate(chunks):
            line_number = index + 1
            try:
                line = encoded.decode("utf-8")
                value = json.loads(line)
            except (UnicodeDecodeError, json.JSONDecodeError) as exc:
                if has_torn_tail and index == len(chunks) - 1:
                    self._quarantine_torn_tail(encoded, body[: -len(encoded)])
                    return rows
                raise RuntimeError(
                    f"invalid usage ledger row {line_number}: {exc}"
                ) from exc
            if not isinstance(value, dict):
                raise RuntimeError(f"invalid usage ledger row {line_number}")
            rows.append(value)
        if has_torn_tail:
            self._replace_ledger(body + b"\n")
        return rows

    def _quarantine_torn_tail(self, fragment: bytes, valid_prefix: bytes) -> None:
        quarantine = self.ledger_path.with_suffix(
            self.ledger_path.suffix + ".quarantine"
        )
        record = {
            "quarantined_at": self._now().isoformat(),
            "fragment": fragment.decode("utf-8", errors="replace"),
        }
        quarantine.parent.mkdir(parents=True, exist_ok=True)
        with quarantine.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(record, sort_keys=True) + "\n")
            handle.flush()
            os.fsync(handle.fileno())
        self._replace_ledger(valid_prefix)

    def _replace_ledger(self, body: bytes) -> None:
        descriptor, temporary = tempfile.mkstemp(
            prefix=".usage-ledger-", dir=self.ledger_path.parent
        )
        try:
            with os.fdopen(descriptor, "wb") as handle:
                handle.write(body)
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temporary, self.ledger_path)
        finally:
            try:
                os.unlink(temporary)
            except FileNotFoundError:
                pass

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
            _prune_stale_waits(state)
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
    root = _xdg_root("XDG_CONFIG_HOME", Path.home() / ".config")
    return root / "magnolia" / "usage-policy.yaml"


def _default_ledger_path() -> Path:
    root = _xdg_root("XDG_STATE_HOME", Path.home() / ".local" / "state")
    return root / "magnolia" / "usage-ledger.jsonl"


def _xdg_root(name: str, fallback: Path) -> Path:
    configured = os.environ.get(name)
    if configured:
        candidate = Path(configured).expanduser()
        if candidate.is_absolute():
            return candidate
    return fallback


_USAGE_FIELDS = (
    "turns",
    "input_tokens",
    "output_tokens",
    "reasoning_tokens",
    "cached_tokens",
    "cache_write_tokens",
    "cost",
    "duration_seconds",
)
_SUBSCRIPTION_GROUP = re.compile(
    r"(?:claude-code|codex|vibe|opencode:[A-Za-z0-9._-]+)\Z"
)
_PROJECT_DIGEST_GROUP = re.compile(r"[0-9a-f]{8,64}\Z")


def _usage_summary(rows: list[Mapping[str, Any]]) -> dict[str, Any]:
    """Aggregate privacy-bounded ledger rows without treating unknown as zero."""

    def totals(items: list[Mapping[str, Any]]) -> dict[str, Any]:
        values = {name: 0 for name in _USAGE_FIELDS}
        unknown = {name: 0 for name in _USAGE_FIELDS}
        for row in items:
            usage = row.get("usage")
            for name in _USAGE_FIELDS:
                value = usage.get(name) if isinstance(usage, Mapping) else None
                if isinstance(value, (int, float)) and not isinstance(value, bool):
                    values[name] += value
                else:
                    unknown[name] += 1
        return {"runs": len(items), "usage": values, "unknown": unknown}

    def grouped(field: str, pattern: re.Pattern[str]) -> dict[str, Any]:
        groups: dict[str, list[Mapping[str, Any]]] = {}
        for row in rows:
            value = row.get(field)
            if not isinstance(value, str) or pattern.fullmatch(value) is None:
                value = "unknown"
            groups.setdefault(value, []).append(row)
        return {key: totals(groups[key]) for key in sorted(groups)}

    return {
        **totals(rows),
        "by_subscription": grouped("subscription_key", _SUBSCRIPTION_GROUP),
        "by_project": grouped("project_digest", _PROJECT_DIGEST_GROUP),
    }


def _utc(value: datetime) -> datetime:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError("governor clock must return a timezone-aware datetime")
    return value.astimezone(timezone.utc)


def _next_daily_reset(now: datetime) -> datetime:
    return datetime.combine(now.date() + timedelta(days=1), time.min, timezone.utc)


def _parse_time(value: str | None) -> datetime:
    if not value:
        raise ValueError("missing timestamp")
    return _utc(datetime.fromisoformat(value))


def _row_day(row: Mapping[str, Any]) -> date:
    basis = row.get("reserved_at") or row.get("started_at")
    return _parse_time(str(basis)).date()


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
    verified = _verified_process_start_token(pid)
    if verified:
        return verified
    return f"unverified:{pid}:{system_time.time_ns()}"


def _owner_identity(now: datetime) -> dict[str, Any]:
    pid = os.getpid()
    token = _verified_process_start_token(pid)
    expires_at = None
    if token is None:
        expires_at = (now + timedelta(seconds=UNVERIFIED_OWNER_TTL_SECONDS)).isoformat()
    return {"pid": pid, "start_token": token, "expires_at": expires_at}


def _reservation_owner_gone(reservation: Mapping[str, Any], now: datetime) -> bool:
    pid = reservation.get("owner_pid")
    if not isinstance(pid, int) or pid <= 0:
        return True
    expected = reservation.get("owner_start_token")
    if isinstance(expected, str) and expected:
        current = _verified_process_start_token(pid)
        if current is not None:
            return current != expected
        return _pid_gone(pid)
    expires_at = reservation.get("owner_expires_at")
    if isinstance(expires_at, str) and now >= _parse_time(expires_at):
        return True
    return _pid_gone(pid)


def _reservation_process_alive(reservation: Mapping[str, Any]) -> bool:
    pid = reservation.get("pid")
    pgid = reservation.get("pgid")
    if not isinstance(pid, int) or pid <= 0:
        return True
    if not isinstance(pgid, int) or pgid <= 0:
        return True
    leader_gone = _pid_gone(pid)
    expected = reservation.get("process_start_token")
    if isinstance(expected, str) and expected.startswith(("proc:", "ps:")):
        current = _verified_process_start_token(pid)
        if current is None and not leader_gone:
            return True
        if current is not None and current != expected:
            leader_gone = True
    if not leader_gone:
        return True
    return _process_group_exists(pgid)


def _pid_gone(pid: int) -> bool:
    state = _process_state(pid)
    if state is not None:
        return "Z" in state
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return True
    except PermissionError:
        return False
    return False


def _process_group_exists(pgid: int) -> bool:
    try:
        os.killpg(pgid, 0)
    except OSError as exc:
        if exc.errno == errno.ESRCH:
            return False
        if exc.errno != errno.EPERM:
            return True
    if Path("/proc").is_dir():
        has_live_members = _linux_group_has_live_members(pgid)
    else:
        has_live_members = _ps_group_has_live_members(pgid)
    return True if has_live_members is None else has_live_members


def _process_state(pid: int) -> str | None:
    try:
        body = Path(f"/proc/{pid}/stat").read_bytes()
    except FileNotFoundError:
        return _ps_process_state(pid)
    except OSError:
        return None
    fields = _proc_stat_fields(body)
    if fields is None or len(fields[0]) != 1:
        return None
    try:
        return fields[0].decode("ascii")
    except UnicodeDecodeError:
        return None


def _ps_process_state(pid: int) -> str | None:
    try:
        result = subprocess.run(
            ["/bin/ps", "-o", "stat=", "-p", str(pid)],
            check=False,
            capture_output=True,
            text=True,
            timeout=1,
            env={"LC_ALL": "C", "TZ": "UTC"},
        )
    except (OSError, subprocess.TimeoutExpired):
        return None
    state = result.stdout.strip()
    if result.returncode != 0 or not state:
        return None
    return state


def _linux_group_has_live_members(pgid: int) -> bool | None:
    expected_group = str(pgid).encode("ascii")
    saw_group = False
    try:
        entries = tuple(Path("/proc").iterdir())
    except OSError:
        return None
    for entry in entries:
        if not entry.name.isdigit():
            continue
        try:
            body = (entry / "stat").read_bytes()
        except FileNotFoundError:
            continue
        except OSError:
            return None
        fields = _proc_stat_fields(body)
        if fields is None or len(fields) < 3:
            return None
        if fields[2] != expected_group:
            continue
        saw_group = True
        if fields[0] != b"Z":
            return True
    return False if saw_group else None


def _ps_group_has_live_members(pgid: int) -> bool | None:
    try:
        result = subprocess.run(
            ["/bin/ps", "-axo", "pgid=,stat="],
            check=False,
            capture_output=True,
            text=True,
            timeout=1,
            env={"LC_ALL": "C", "TZ": "UTC"},
        )
    except (OSError, subprocess.TimeoutExpired):
        return None
    if result.returncode != 0:
        return None
    saw_group = False
    for line in result.stdout.splitlines():
        fields = line.split(None, 1)
        if len(fields) != 2:
            return None
        try:
            process_group = int(fields[0])
        except ValueError:
            return None
        if process_group != pgid:
            continue
        saw_group = True
        if "Z" not in fields[1]:
            return True
    return False if saw_group else None


def _pid_exists_fast(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


def _prune_stale_waits(state: dict[str, Any]) -> None:
    for wait_id, waiting in tuple(state["waits"].items()):
        owner_pid = waiting.get("owner_pid")
        if isinstance(owner_pid, int) and not _pid_exists_fast(owner_pid):
            del state["waits"][wait_id]


def _merge_cooldown(
    current: Mapping[str, Any] | None, observed: dict[str, Any]
) -> dict[str, Any]:
    if current is None:
        return observed
    current_reset = current.get("reset_at")
    observed_reset = observed.get("reset_at")
    if current_reset is None or observed_reset is None:
        observed["reset_at"] = None
    elif _parse_time(current_reset) > _parse_time(observed_reset):
        observed["reset_at"] = current_reset
    return observed
