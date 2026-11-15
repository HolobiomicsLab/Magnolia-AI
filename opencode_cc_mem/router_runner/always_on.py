"""Opt-in always-on conversation runtime over the legacy mailbox executor.

This module is deliberately additive. It does not replace agentd, the campaign
runner, project memory, or magnolia-agents-daemon. A runtime sweep turns JSON
events into ordinary ``*.task.md`` letters; the existing daemon remains the only
OpenCode executor. Replies are reconciled into durable Work Item and outbox
records on a later sweep.

Enable per project in agent.json::

    {"enabled": true,
     "always_on": {"enabled": true, "auto_execute": false,
                   "delivery_dir": "inbox/always-on-outbox"}}

``auto_execute`` is false by default. When false, a Work Item waits for the
same content-bound, single-use approval used by agentd. When true, the lane's
operator-authored policy approves the exact WorkSpec; event content cannot
change that WorkSpec within an active Work Item.

CLI (from opencode_cc_mem/)::

    python3 -m router_runner.always_on --db /tmp/ao.db \
        --project-dir projects/demo run --once

Input events live in <project>/inbox/always-on/*.json. See
``docs/always-on-runtime.md`` for the schema and operational contract.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import sqlite3
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

from .admission import AdmissionPolicy
from .agentd import operator
from .campaign_adapter import CompositeAdapter
from .engine import ControlEngine
from .job_service import FakeJobService
from .models import ACTIVE_STATES, RouterError, TriggerSpec, WorkItemState, WorkSpec
from .store import ControlStore, dumps


SCHEMA = """
CREATE TABLE IF NOT EXISTS ao_events (
    event_id TEXT PRIMARY KEY,
    lane TEXT NOT NULL,
    source_id TEXT NOT NULL,
    source_event_id TEXT NOT NULL,
    actor TEXT NOT NULL,
    thread_key TEXT NOT NULL,
    text TEXT NOT NULL,
    work_spec TEXT NOT NULL,
    work_item_id TEXT,
    run_id TEXT,
    status TEXT NOT NULL,
    created_at TEXT NOT NULL,
    completed_at TEXT,
    UNIQUE(lane, source_id, source_event_id)
);
CREATE INDEX IF NOT EXISTS ao_events_pending
    ON ao_events(lane, status, created_at);
CREATE INDEX IF NOT EXISTS ao_events_thread
    ON ao_events(lane, thread_key, created_at);
CREATE TABLE IF NOT EXISTS ao_runs (
    run_id TEXT PRIMARY KEY,
    lane TEXT NOT NULL,
    work_item_id TEXT NOT NULL,
    thread_key TEXT NOT NULL,
    session_key TEXT NOT NULL,
    status TEXT NOT NULL,
    letter_path TEXT NOT NULL,
    reply_path TEXT NOT NULL,
    created_at TEXT NOT NULL,
    started_at TEXT,
    ended_at TEXT,
    error TEXT
);
CREATE INDEX IF NOT EXISTS ao_runs_active
    ON ao_runs(lane, status, created_at);
-- Enforce one active worker per lane across processes, not just within one
-- Python runtime. Completing/failing a run releases the slot atomically.
CREATE UNIQUE INDEX IF NOT EXISTS ao_one_active_run
    ON ao_runs(lane) WHERE status IN ('bridging', 'running');
CREATE TABLE IF NOT EXISTS ao_timers (
    timer_id TEXT PRIMARY KEY,
    lane TEXT NOT NULL,
    thread_key TEXT NOT NULL,
    kind TEXT NOT NULL,
    due_at TEXT NOT NULL,
    recurrence_seconds INTEGER,
    text TEXT NOT NULL,
    work_spec TEXT NOT NULL,
    status TEXT NOT NULL,
    created_at TEXT NOT NULL,
    last_event_id TEXT,
    last_error TEXT,
    error_count INTEGER NOT NULL DEFAULT 0
);
CREATE INDEX IF NOT EXISTS ao_timers_due
    ON ao_timers(lane, status, due_at);
CREATE TABLE IF NOT EXISTS ao_outbox (
    message_id TEXT PRIMARY KEY,
    lane TEXT NOT NULL,
    work_item_id TEXT NOT NULL,
    run_id TEXT NOT NULL,
    kind TEXT NOT NULL,
    body TEXT NOT NULL,
    state TEXT NOT NULL,
    destination TEXT,
    idempotency_key TEXT NOT NULL UNIQUE,
    created_at TEXT NOT NULL,
    sent_at TEXT
);
CREATE INDEX IF NOT EXISTS ao_outbox_pending
    ON ao_outbox(lane, state, created_at);
"""

_WAITING_RE = re.compile(r"^always-on-status:\s*waiting-human\s*$", re.I | re.M)
_NO_SEND_RE = re.compile(r"^always-on-no-send:\s*(?:yes|true)\s*$", re.I | re.M)
_STATUS_RE = re.compile(r"^[\s>*-]*status:\s*(\S+?)\s*$", re.I | re.M)
_TERMINAL_WORK_STATES = {
    WorkItemState.COMPLETED,
    WorkItemState.FAILED,
    WorkItemState.STOPPED,
    WorkItemState.IGNORED,
}


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _iso(value: str | datetime) -> str:
    if isinstance(value, str):
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    else:
        parsed = value
    if parsed.tzinfo is None:
        raise ValueError("timer due_at must include a timezone")
    return parsed.astimezone(timezone.utc).isoformat()


def _new_id(prefix: str) -> str:
    import uuid

    return f"{prefix}_{uuid.uuid4().hex[:12]}"


def _atomic_write(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f"{path.name}.tmp.{os.getpid()}.{_new_id('write')}")
    try:
        tmp.write_text(text, encoding="utf-8")
        os.replace(tmp, path)
    finally:
        try:
            tmp.unlink()
        except FileNotFoundError:
            pass


def _publish_once(path: Path, text: str) -> None:
    """Atomically create a run letter without ever replacing an existing one.

    A hard-link publish is atomic on the local filesystem and, unlike replace,
    cannot reopen a letter the legacy daemon has already marked done/error.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f"{path.name}.tmp.{os.getpid()}.{_new_id('write')}")
    try:
        tmp.write_text(text, encoding="utf-8")
        try:
            os.link(tmp, path)
        except FileExistsError:
            pass  # another publisher won; its letter is authoritative
    finally:
        try:
            tmp.unlink()
        except FileNotFoundError:
            pass


def _safe_key(value: str) -> str:
    slug = re.sub(r"[^A-Za-z0-9_.:-]+", "-", value).strip("-.:")[:48] or "thread"
    digest = hashlib.sha256(value.encode("utf-8")).hexdigest()[:10]
    return f"{slug}-{digest}"


def _letter_status(text: str) -> str | None:
    match = _STATUS_RE.search("\n".join(text.splitlines()[:15]))
    return match.group(1).lower() if match else None


class MemoryBridge:
    """Bounded, non-authoritative memory handoff for legacy headless letters.

    Only boot context and the runtime's own session-notes file are included.
    Progress never enters staging/, entries/, or rules/ automatically.
    """

    def __init__(self, project_dir: Path, max_chars: int = 12000):
        self.project_dir = project_dir
        self.max_chars = max(256, int(max_chars))

    def progress_path(self, thread_key: str) -> Path:
        return (self.project_dir / ".magnolia" / "session-notes" / "always-on"
                / f"{_safe_key(thread_key)}.md")

    def load(self, thread_key: str) -> str:
        sections = []
        boot = self.project_dir / ".magnolia" / "boot-context.md"
        progress = self.progress_path(thread_key)
        for title, path in (("Project boot context", boot),
                            ("Previous thread progress", progress)):
            try:
                text = path.read_text(encoding="utf-8", errors="replace").strip()
            except OSError:
                continue
            if text:
                sections.append(f"### {title}\nSource: {path}\n\n{text}")
        return "\n\n".join(sections)[-self.max_chars:]

    def save(self, thread_key: str, run_id: str, reply_path: Path, body: str) -> Path:
        path = self.progress_path(thread_key)
        # This file is runtime-owned working state, not a confirmed learning.
        # Keep the latest reply bounded and point to the complete receipt.
        text = (
            "---\n"
            "kind: always-on-progress\n"
            f"thread_key: {json.dumps(thread_key)}\n"
            f"run_id: {run_id}\n"
            f"updated_at: {_now()}\n"
            "authority: guidance-only\n"
            "---\n\n"
            "# Latest always-on progress\n\n"
            f"Complete reply: {reply_path}\n\n"
            f"{body[-6000:]}\n"
        )
        _atomic_write(path, text)
        return path


class AlwaysOnRuntime:
    """Durable event/run coordinator using the existing ControlEngine store."""

    def __init__(
        self,
        engine: ControlEngine,
        project_dir: str | Path,
        *,
        auto_execute: bool = False,
        delivery_dir: str | Path | None = None,
        memory_limit: int = 12000,
    ):
        self.engine = engine
        self.store = engine.store
        self.store.conn.executescript(SCHEMA)
        # Additive migration for databases created by the first v0.1 draft.
        timer_columns = {
            row[1] for row in self.store.conn.execute("PRAGMA table_info(ao_timers)")
        }
        if "last_error" not in timer_columns:
            self.store.conn.execute("ALTER TABLE ao_timers ADD COLUMN last_error TEXT")
        if "error_count" not in timer_columns:
            self.store.conn.execute(
                "ALTER TABLE ao_timers ADD COLUMN error_count INTEGER NOT NULL DEFAULT 0"
            )
        self.store.conn.commit()
        self.project_dir = Path(project_dir)
        self.lane = self.project_dir.name
        self.auto_execute = bool(auto_execute)
        self.delivery_dir = Path(delivery_dir) if delivery_dir else None
        if self.delivery_dir and not self.delivery_dir.is_absolute():
            self.delivery_dir = self.project_dir / self.delivery_dir
        self.memory = MemoryBridge(self.project_dir, memory_limit)

    @classmethod
    def from_project(cls, db: str, project_dir: str | Path) -> "AlwaysOnRuntime":
        project = Path(project_dir)
        config_path = project / "agent.json"
        try:
            lane_config = json.loads(config_path.read_text(encoding="utf-8"))
        except FileNotFoundError as exc:
            raise RouterError(f"lane is not registered: {config_path} is absent") from exc
        config = lane_config.get("always_on", {})
        if not lane_config.get("enabled", False) or not config.get("enabled", False):
            raise RouterError(
                f"always-on runtime is opt-in; set enabled=true in {config_path} "
                "and in its always_on object"
            )
        store = ControlStore(db)
        policy = AdmissionPolicy(
            allowed_senders=frozenset({operator()}),
            allowed_projects=frozenset({project.name}),
            policy_version="always-on-v0.1",
        )
        engine = ControlEngine(
            store,
            policy,
            CompositeAdapter(out_root=str(project / "runs")),
            FakeJobService(project / "runs" / "jobs"),
        )
        # Do not call engine.recover() here: mailbox attempts are owned by the
        # legacy daemon and tracked in ao_runs, not ControlEngine.assignments.
        return cls(
            engine,
            project,
            auto_execute=config.get("auto_execute", False),
            delivery_dir=config.get("delivery_dir"),
            memory_limit=int(config.get("memory_limit", 12000)),
        )

    @property
    def event_dir(self) -> Path:
        return self.project_dir / "inbox" / "always-on"

    def close(self) -> None:
        self.store.close()

    # -------------------------------------------------------------- events --

    def _work_spec(self, letter: dict) -> WorkSpec:
        raw = letter.get("work_spec", {})
        spec = WorkSpec(
            project=raw.get("project", self.lane),
            protocol=raw.get("protocol", "legacy-mailbox-v0.1"),
            scope=raw.get("scope", "one always-on thread; existing mailbox permissions"),
            budget=raw.get("budget", "one headless OpenCode run per batch"),
        )
        if spec.project != self.lane:
            raise RouterError(
                f"cross-project execution is not available: {spec.project} != {self.lane}"
            )
        if spec.protocol != "legacy-mailbox-v0.1":
            raise RouterError(
                "always-on mailbox events require protocol=legacy-mailbox-v0.1; "
                "campaign and task-pack protocols must use their own adapters"
            )
        return spec

    def ingest(self, letter: dict) -> dict:
        source_id = str(letter.get("source_id", "")).strip()
        source_event_id = str(letter.get("source_event_id", "")).strip()
        text = str(letter.get("text", "")).strip()
        thread_key = str(letter.get("thread_key", "default")).strip() or "default"
        actor = str(letter.get("actor") or operator())
        if not source_id or not source_event_id or not text:
            raise RouterError("event requires source_id, source_event_id, and text")
        if actor not in self.engine.policy.allowed_senders:
            raise RouterError(f"sender not allowed: {actor}")
        spec = self._work_spec(letter)
        existing = self.store.find_one(
            "ao_events", "lane = ? AND source_id = ? AND source_event_id = ?",
            (self.lane, source_id, source_event_id),
        )
        if existing:
            return {"created": False, "event": existing}

        event_id = _new_id("aoe")
        try:
            with self.store.tx():
                self.store.insert("ao_events", {
                    "event_id": event_id,
                    "lane": self.lane,
                    "source_id": source_id,
                    "source_event_id": source_event_id,
                    "actor": actor,
                    "thread_key": thread_key,
                    "text": text,
                    "work_spec": dumps(spec.as_dict()),
                    "work_item_id": None,
                    "run_id": None,
                    "status": "pending",
                    "created_at": _now(),
                    "completed_at": None,
                })
        except sqlite3.IntegrityError:
            # A concurrent sweep won the uniqueness race; its row is the event.
            existing = self.store.find_one(
                "ao_events", "lane = ? AND source_id = ? AND source_event_id = ?",
                (self.lane, source_id, source_event_id),
            )
            if existing:
                return {"created": False, "event": existing}
            raise
        try:
            attached = self._attach_event(event_id, spec)
        except Exception:
            with self.store.tx():
                self.store.update("ao_events", "event_id", event_id, {
                    "status": "error", "completed_at": _now(),
                })
            raise
        return {"created": True, "event": self.store.get("ao_events", "event_id", event_id),
                **attached}

    def _latest_thread_work_item(self, thread_key: str, exclude_event: str) -> dict | None:
        rows = self.store.all(
            "ao_events",
            "lane = ? AND thread_key = ? AND event_id != ? "
            "AND work_item_id IS NOT NULL ORDER BY created_at DESC",
            (self.lane, thread_key, exclude_event),
        )
        for row in rows:
            wi = self.store.get("work_items", "work_item_id", row["work_item_id"])
            if wi:
                return wi
        return None

    def _check_same_scope(self, wi: dict, spec: WorkSpec) -> None:
        current = WorkSpec(
            project=wi["project"], protocol=wi["protocol"],
            scope=wi["scope"], budget=wi["budget"],
        )
        if current != spec:
            raise RouterError(
                "WorkSpec cannot change inside an active thread; use a new thread_key "
                "so changed protocol, scope, or budget receives fresh approval"
            )

    def _attach_event(self, event_id: str, spec: WorkSpec) -> dict:
        # Serialize predecessor lookup + trigger/Work Item creation + linkage
        # across processes. Nested ControlEngine transactions join this one.
        with self.store.tx():
            return self._attach_event_locked(event_id, spec)

    def _attach_event_locked(self, event_id: str, spec: WorkSpec) -> dict:
        event = self.store.get("ao_events", "event_id", event_id)
        previous = self._latest_thread_work_item(event["thread_key"], event_id)
        if previous and previous["state"] in ACTIVE_STATES:
            self._check_same_scope(previous, spec)
            waiting_approval = previous["state"] == WorkItemState.CANDIDATE
            with self.store.tx():
                self.store.update("ao_events", "event_id", event_id, {
                    "work_item_id": previous["work_item_id"],
                    "status": "waiting_approval" if waiting_approval else "pending",
                })
            if not waiting_approval:
                # Context-only follow-up: no scope or execution authority added.
                self.engine.follow_up(previous["work_item_id"], event["actor"], event["text"])
                if previous["state"] == WorkItemState.WAITING_HUMAN:
                    with self.store.tx():
                        self.engine._transition(
                            previous["work_item_id"], WorkItemState.QUEUED
                        )
            return {"work_item_id": previous["work_item_id"], "approval": None}
        # A terminal predecessor is only a parent/thread link. Changed scope,
        # protocol, or budget is valid here and receives a fresh approval.
        return self._create_work_item(event, spec, previous)

    def _create_work_item(self, event: dict, spec: WorkSpec,
                          previous: dict | None) -> dict:
        trigger, _ = self.engine.ingest(TriggerSpec(
            source_id=event["source_id"],
            source_event_id=event["source_event_id"],
            actor=event["actor"],
            project_hint=self.lane,
            payload={"text": event["text"], "thread_key": event["thread_key"]},
        ))
        # Resume cleanly if an older/interrupted implementation left a trigger
        # and candidate before linking the ao_event. Normally the outer atomic
        # transaction makes this path unnecessary, but recovery must not loop.
        wi = self.store.find_one("work_items", "trigger_id = ?", (trigger["trigger_id"],))
        if wi is None:
            approval = self.engine.request_work(trigger["trigger_id"], spec)
            work_item_id = approval["work_item_id"]
            wi = self.store.get("work_items", "work_item_id", work_item_id)
        else:
            self._check_same_scope(wi, spec)
            work_item_id = wi["work_item_id"]
            approval = self.store.find_one("approvals", "work_item_id = ?", (work_item_id,))
            if approval is None:
                raise RouterError("existing Work Item has no approval request")

        if wi["state"] in _TERMINAL_WORK_STATES:
            raise RouterError(
                f"cannot attach a new event to {wi['state']} Work Item {work_item_id}"
            )
        if previous:
            self.store.update("work_items", "work_item_id", work_item_id, {
                "parent_work_item_id": previous["work_item_id"],
                "thread_id": previous["thread_id"] or previous["work_item_id"],
            })

        waiting = wi["state"] == WorkItemState.CANDIDATE
        self.store.update("ao_events", "event_id", event["event_id"], {
            "work_item_id": work_item_id,
            "status": "waiting_approval" if waiting else "pending",
        })
        # A human STOP is never auto-resumed, even on an auto-execute lane. A
        # genuinely new request after STOP requires a fresh operator approval.
        auto_allowed = self.auto_execute and (
            not previous or previous["state"] != WorkItemState.STOPPED
        )
        if waiting and auto_allowed:
            if approval.get("state", "pending") != "pending":
                raise RouterError("candidate approval is not pending")
            self.engine.approve(
                work_item_id, approval["nonce"], f"policy:always-on:{self.lane}"
            )
            self.store.update("ao_events", "event_id", event["event_id"], {
                "status": "pending",
            })
        return {"work_item_id": work_item_id, "approval": approval}

    def approve(self, work_item_id: str, nonce: str) -> dict:
        with self.store.tx():
            wi = self.engine.approve(work_item_id, nonce, f"operator:{operator()}")
            for event in self.store.all(
                "ao_events", "work_item_id = ? AND status = ?",
                (work_item_id, "waiting_approval"),
            ):
                self.store.update("ao_events", "event_id", event["event_id"], {
                    "status": "pending",
                })
            return wi

    # --------------------------------------------------------------- timers --

    def add_timer(self, spec: dict) -> dict:
        kind = spec.get("kind", "followup")
        if kind not in {"followup", "schedule"}:
            raise RouterError("timer kind must be followup or schedule")
        recurrence = spec.get("recurrence_seconds")
        if kind == "schedule" and (not recurrence or int(recurrence) <= 0):
            raise RouterError("schedule timers require positive recurrence_seconds")
        text = str(spec.get("text", "")).strip()
        if not text:
            raise RouterError("timer requires text")
        work_spec = self._work_spec(spec)
        timer_id = _new_id("aot")
        row = {
            "timer_id": timer_id,
            "lane": self.lane,
            "thread_key": str(spec.get("thread_key", "default")),
            "kind": kind,
            "due_at": _iso(spec["due_at"]),
            "recurrence_seconds": int(recurrence) if recurrence else None,
            "text": text,
            "work_spec": dumps(work_spec.as_dict()),
            "status": "active",
            "created_at": _now(),
            "last_event_id": None,
            "last_error": None,
            "error_count": 0,
        }
        with self.store.tx():
            self.store.insert("ao_timers", row)
        return row

    def fire_due_timers(self, now: str | datetime | None = None) -> list[dict]:
        current = _iso(now or datetime.now(timezone.utc))
        fired = []
        due = self.store.all(
            "ao_timers", "lane = ? AND status = ? AND due_at <= ? ORDER BY due_at",
            (self.lane, "active", current),
        )
        for timer in due:
            occurrence = timer["due_at"]
            with self.store.tx():
                current_timer = self.store.get("ao_timers", "timer_id", timer["timer_id"])
                if (not current_timer or current_timer["status"] != "active"
                        or current_timer["due_at"] != occurrence):
                    continue  # another runtime already handled this occurrence
                try:
                    result = self.ingest({
                        "source_id": f"timer:{self.lane}",
                        "source_event_id": f"{timer['timer_id']}:{occurrence}",
                        "thread_key": current_timer["thread_key"],
                        "text": current_timer["text"],
                        "work_spec": json.loads(current_timer["work_spec"]),
                    })
                    if result["event"]["status"] == "error":
                        raise RouterError("timer event previously failed admission")
                except (RouterError, ValueError) as exc:
                    # Keep the occurrence due for an explicit fix/retry. Never
                    # advance a schedule past an event that failed admission.
                    self.store.update("ao_timers", "timer_id", timer["timer_id"], {
                        "last_error": str(exc),
                        "error_count": current_timer["error_count"] + 1,
                    })
                    fired.append({"timer_id": timer["timer_id"], "error": str(exc)})
                    continue
                if current_timer["kind"] == "followup":
                    changes = {"status": "done", "last_event_id": result["event"]["event_id"]}
                else:
                    next_due = datetime.fromisoformat(occurrence)
                    step = timedelta(seconds=current_timer["recurrence_seconds"])
                    now_dt = datetime.fromisoformat(current)
                    while next_due <= now_dt:
                        next_due += step
                    changes = {
                        "due_at": next_due.isoformat(),
                        "last_event_id": result["event"]["event_id"],
                    }
                changes["last_error"] = None
                self.store.update("ao_timers", "timer_id", timer["timer_id"], changes)
                fired.append({"timer_id": timer["timer_id"], **result})
        return fired

    # ---------------------------------------------------------------- runs --

    def _active_run(self) -> dict | None:
        return self.store.find_one(
            "ao_runs", "lane = ? AND status IN ('bridging', 'running')", (self.lane,)
        )

    def _recover_event_states(self) -> None:
        """Repair state-only interruptions from older/non-atomic callers."""
        with self.store.tx():
            waiting = self.store.all(
                "ao_events", "lane = ? AND status = ?", (self.lane, "waiting_approval")
            )
            for event in waiting:
                wi = self.store.get("work_items", "work_item_id", event["work_item_id"])
                if wi and wi["state"] != WorkItemState.CANDIDATE:
                    # Approval already committed; do not require the used nonce.
                    self.store.update("ao_events", "event_id", event["event_id"], {
                        "status": "pending",
                    })
            pending = self.store.all(
                "ao_events", "lane = ? AND status = ? ORDER BY created_at",
                (self.lane, "pending"),
            )
            resumed = set()
            for event in pending:
                wi_id = event["work_item_id"]
                if not wi_id or wi_id in resumed:
                    continue
                wi = self.store.get("work_items", "work_item_id", wi_id)
                if wi and wi["state"] == WorkItemState.WAITING_HUMAN:
                    # Input may have arrived while the previous run was active.
                    # Its presence is itself the answer that resumes the wait.
                    self.engine._transition(wi_id, WorkItemState.QUEUED)
                    resumed.add(wi_id)

    def _prepare_terminal_events(self) -> None:
        pending = self.store.all(
            "ao_events", "lane = ? AND status = ? ORDER BY created_at",
            (self.lane, "pending"),
        )
        for event in pending:
            if not event["work_item_id"]:
                continue
            wi = self.store.get("work_items", "work_item_id", event["work_item_id"])
            if wi and wi["state"] in _TERMINAL_WORK_STATES:
                with self.store.tx():
                    self.store.update("ao_events", "event_id", event["event_id"], {
                        "work_item_id": None, "run_id": None,
                    })
                self._attach_event(event["event_id"], WorkSpec(**json.loads(event["work_spec"])))

    def pump(self) -> dict | None:
        """Bridge at most one run per lane; rapid same-Work Item events batch."""
        self._recover_event_states()
        self._prepare_terminal_events()
        if self._active_run():
            return None
        pending = self.store.all(
            "ao_events", "lane = ? AND status = ? ORDER BY created_at",
            (self.lane, "pending"),
        )
        for event in pending:
            if not event["work_item_id"]:
                self._attach_event(event["event_id"], WorkSpec(**json.loads(event["work_spec"])))
                event = self.store.get("ao_events", "event_id", event["event_id"])
            wi = self.store.get("work_items", "work_item_id", event["work_item_id"])
            if not wi or wi["state"] != WorkItemState.QUEUED:
                continue
            batch = self.store.all(
                "ao_events",
                "lane = ? AND work_item_id = ? AND status = ? ORDER BY created_at",
                (self.lane, wi["work_item_id"], "pending"),
            )
            return self._start_run(wi, batch)
        return None

    def _start_run(self, wi: dict, events: list[dict]) -> dict:
        run_id = _new_id("aor")
        letter = self.project_dir / "inbox" / "always-on-runs" / f"{run_id}.task.md"
        reply = letter.with_name(f"{run_id}_reply.md")
        session_key = _safe_key(wi["thread_id"] or events[0]["thread_key"])
        try:
            with self.store.tx():
                self.store.insert("ao_runs", {
                    "run_id": run_id,
                    "lane": self.lane,
                    "work_item_id": wi["work_item_id"],
                    "thread_key": events[0]["thread_key"],
                    "session_key": session_key,
                    "status": "bridging",
                    "letter_path": str(letter),
                    "reply_path": str(reply),
                    "created_at": _now(),
                    "started_at": None,
                    "ended_at": None,
                    "error": None,
                })
                for event in events:
                    self.store.update("ao_events", "event_id", event["event_id"], {
                        "run_id": run_id, "status": "leased",
                    })
                self.engine._transition(wi["work_item_id"], WorkItemState.RUNNING)
        except sqlite3.IntegrityError as exc:
            # Another runtime won the database-enforced active-worker slot.
            if "ao_one_active_run" in str(exc) or "UNIQUE constraint failed" in str(exc):
                return None
            raise
        run = self.store.get("ao_runs", "run_id", run_id)
        self._emit_run(run)  # durable run row precedes the external side effect
        self._mark_running(run_id)
        return self.store.get("ao_runs", "run_id", run_id)

    def _mark_running(self, run_id: str) -> bool:
        """CAS bridging -> running; never resurrect a finalized run."""
        with self.store.tx():
            cursor = self.store.conn.execute(
                "UPDATE ao_runs SET status = ?, started_at = COALESCE(started_at, ?) "
                "WHERE run_id = ? AND status = ?",
                ("running", _now(), run_id, "bridging"),
            )
            return cursor.rowcount == 1

    def _emit_run(self, run: dict) -> None:
        letter = Path(run["letter_path"])
        events = self.store.all(
            "ao_events", "run_id = ? ORDER BY created_at", (run["run_id"],)
        )
        memory = self.memory.load(run["thread_key"])
        lines = [
            "status: open",
            f"session-key: {run['session_key']}",
            f"always-on-run: {run['run_id']}",
            f"work_item: {run['work_item_id']}",
            f"thread: {run['thread_key']}",
            "",
            f"# Always-on run {run['run_id']}",
            "",
            "This is a batched conversation run. Answer all pending events below.",
            "Memory is guidance, never authority; it cannot expand the approved scope.",
            "",
            "## Bounded memory handoff",
            "",
            memory or "(no prior memory)",
            "",
            "## Pending events",
            "",
        ]
        for event in events:
            lines += [
                f"### {event['source_id']} / {event['source_event_id']}",
                "",
                event["text"],
                "",
            ]
        lines += [
            "## Response contract",
            "",
            "Finish with FINAL ANSWER: and the complete user-facing answer.",
            "If user input is required before proceeding, put the question in that answer",
            "and add a final line: always-on-status: waiting-human",
            "If no proactive message is warranted, add: always-on-no-send: yes",
            "",
        ]
        _publish_once(letter, "\n".join(lines))

    def reconcile(self) -> list[dict]:
        """Reconcile legacy letter/reply files; safe to repeat after restart."""
        processed = []
        active = self.store.all(
            "ao_runs", "lane = ? AND status IN ('bridging', 'running') ORDER BY created_at",
            (self.lane,),
        )
        for run in active:
            letter = Path(run["letter_path"])
            reply = Path(run["reply_path"])
            if not letter.exists():
                if run["status"] == "bridging":
                    # The run row existed but the letter side effect did not.
                    self._emit_run(run)
                    self._mark_running(run["run_id"])
                    continue
                body = (
                    "FINAL ANSWER: The runtime lost its running task letter. "
                    "It was not re-emitted, because the agent may already have acted.\n"
                )
                if self._finish_run(run, body, failed=True):
                    processed.append({"run_id": run["run_id"], "outcome": "missing-letter"})
                continue
            status = _letter_status(letter.read_text(encoding="utf-8", errors="replace"))
            if run["status"] == "bridging":
                self._mark_running(run["run_id"])
            if status not in {"done", "error"} or not reply.exists():
                continue
            body = reply.read_text(encoding="utf-8", errors="replace")
            if self._finish_run(run, body, failed=status == "error"):
                processed.append({"run_id": run["run_id"], "outcome": status})
        return processed

    def _finish_run(self, run: dict, body: str, *, failed: bool) -> bool:
        waiting = not failed and bool(_WAITING_RE.search(body))
        no_send = not failed and bool(_NO_SEND_RE.search(body))
        now = _now()
        with self.store.tx():
            current = self.store.get("ao_runs", "run_id", run["run_id"])
            if not current or current["status"] not in {"bridging", "running"}:
                return False  # another reconciler already finalized this run
            self.memory.save(
                current["thread_key"], current["run_id"], Path(current["reply_path"]), body
            )
            self.store.update("ao_runs", "run_id", current["run_id"], {
                "status": "failed" if failed else "completed",
                "ended_at": now,
                "error": body[-2000:] if failed else None,
            })
            for event in self.store.all("ao_events", "run_id = ?", (current["run_id"],)):
                self.store.update("ao_events", "event_id", event["event_id"], {
                    "status": "error" if failed else "done", "completed_at": now,
                })
            wi = self.store.get("work_items", "work_item_id", current["work_item_id"])
            if wi["state"] == WorkItemState.RUNNING:
                target = (WorkItemState.FAILED if failed else
                          WorkItemState.WAITING_HUMAN if waiting else WorkItemState.COMPLETED)
                self.engine._transition(current["work_item_id"], target)
                self.store.update("work_items", "work_item_id", current["work_item_id"], {
                    "outcome": target,
                })
            # Defense in depth for replay/concurrent finalization. The active-
            # run recheck above normally makes the conflict impossible.
            self.store.conn.execute("""
                INSERT OR IGNORE INTO ao_outbox (
                    message_id, lane, work_item_id, run_id, kind, body, state,
                    destination, idempotency_key, created_at, sent_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """, (
                _new_id("aom"), self.lane, current["work_item_id"], current["run_id"],
                "error" if failed else "question" if waiting else "reply", body,
                "suppressed" if no_send else "pending", None,
                f"reply:{current['run_id']}", now, None,
            ))
            return True

    # --------------------------------------------------------------- outbox --

    def dispatch(self) -> list[dict]:
        """Deliver pending outbox rows, idempotently, when a directory is set."""
        if not self.delivery_dir:
            return []
        delivered = []
        pending = self.store.all(
            "ao_outbox", "lane = ? AND state = ? ORDER BY created_at",
            (self.lane, "pending"),
        )
        for message in pending:
            with self.store.tx():
                current = self.store.get("ao_outbox", "message_id", message["message_id"])
                if not current or current["state"] != "pending":
                    continue  # another dispatcher delivered it while we waited
                destination = self.delivery_dir / f"{current['message_id']}.md"
                if not destination.exists():
                    _atomic_write(destination, (
                        "---\n"
                        f"work_item: {current['work_item_id']}\n"
                        f"run: {current['run_id']}\n"
                        f"kind: {current['kind']}\n"
                        f"created_at: {current['created_at']}\n"
                        "---\n\n"
                        f"{current['body']}"
                    ))
                self.store.update("ao_outbox", "message_id", current["message_id"], {
                    "state": "sent", "destination": str(destination), "sent_at": _now(),
                })
                delivered.append({"message_id": current["message_id"],
                                  "destination": str(destination)})
        return delivered

    # --------------------------------------------------------------- driver --

    def scan_letters(self) -> list[dict]:
        results = []
        if not self.event_dir.is_dir():
            return results
        for path in sorted(self.event_dir.glob("*.json")):
            try:
                result = self.ingest(json.loads(path.read_text(encoding="utf-8")))
                results.append({"letter": path.name, **result})
            except Exception as exc:
                results.append({"letter": path.name, "created": False, "error": str(exc)})
        return results

    def run_once(self) -> dict:
        scanned = self.scan_letters()
        timers = self.fire_due_timers()
        reconciled = self.reconcile()
        run = self.pump()
        delivered = self.dispatch()
        return {
            "scanned": len(scanned),
            "timers_fired": len([t for t in timers if not t.get("error")]),
            "reconciled": reconciled,
            "started_run": run["run_id"] if run else None,
            "delivered": delivered,
            "errors": ([r for r in scanned if r.get("error")]
                       + [t for t in timers if t.get("error")]),
        }

    def run_loop(self, *, once: bool = False, interval: int = 10,
                 max_cycles: int | None = None) -> int:
        cycles = 0
        while True:
            cycles += 1
            failed = False
            try:
                result = self.run_once()
            except Exception as exc:
                # One malformed timer/event must not kill future availability.
                # The error is visible in the structured log and exit status.
                failed = True
                result = {"cycle_error": str(exc)}
            print(json.dumps(result, default=str), flush=True)
            if once or (max_cycles is not None and cycles >= max_cycles):
                return 1 if failed else 0
            time.sleep(max(1, interval))

    def status(self) -> dict:
        def counts(table: str, column: str) -> dict:
            rows = self.store.all(table, "lane = ?", (self.lane,))
            result = {}
            for row in rows:
                result[row[column]] = result.get(row[column], 0) + 1
            return result

        approvals = []
        for row in self.store.all("approvals", "state = ?", ("pending",)):
            wi = self.store.get("work_items", "work_item_id", row["work_item_id"])
            if wi and wi["project"] == self.lane:
                approvals.append({
                    "work_item_id": row["work_item_id"], "nonce": row["nonce"],
                    "content_hash": row["content_hash"],
                })
        return {
            "lane": self.lane,
            "auto_execute": self.auto_execute,
            "events": counts("ao_events", "status"),
            "runs": counts("ao_runs", "status"),
            "timers": counts("ao_timers", "status"),
            "outbox": counts("ao_outbox", "state"),
            "pending_approvals": approvals,
        }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m router_runner.always_on")
    parser.add_argument("--db", required=True)
    parser.add_argument("--project-dir", required=True, type=Path)
    sub = parser.add_subparsers(dest="command", required=True)
    run = sub.add_parser("run")
    run.add_argument("--once", action="store_true")
    run.add_argument("--interval", type=int, default=10)
    run.add_argument("--max-cycles", type=int)
    ingest = sub.add_parser("ingest")
    ingest.add_argument("letter", type=Path)
    approve = sub.add_parser("approve")
    approve.add_argument("work_item_id")
    approve.add_argument("nonce")
    timer = sub.add_parser("timer")
    timer.add_argument("spec", type=Path)
    sub.add_parser("dispatch")
    sub.add_parser("status")
    args = parser.parse_args(argv)

    runtime = AlwaysOnRuntime.from_project(args.db, args.project_dir)
    try:
        if args.command == "run":
            return runtime.run_loop(
                once=args.once, interval=args.interval, max_cycles=args.max_cycles
            )
        if args.command == "ingest":
            result = runtime.ingest(json.loads(args.letter.read_text(encoding="utf-8")))
        elif args.command == "approve":
            result = runtime.approve(args.work_item_id, args.nonce)
        elif args.command == "timer":
            result = runtime.add_timer(json.loads(args.spec.read_text(encoding="utf-8")))
        elif args.command == "dispatch":
            result = runtime.dispatch()
        else:
            result = runtime.status()
        print(json.dumps(result, indent=2, default=str))
        return 0
    finally:
        runtime.close()


if __name__ == "__main__":
    raise SystemExit(main())
