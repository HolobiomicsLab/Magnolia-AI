"""ControlEngine: the single writer of live Work Item state.

Implements the vertical slice of assessment section 20:
Trigger -> deterministic gate -> candidate + approval request ->
operator approval -> Decision + Mandate -> queued -> assignment +
durable Effect (stored BEFORE execution) -> fake campaign run ->
receipt + outcome + evidence links.

All multi-step writes are one store transaction. Restart-safe: state lives
in SQLite; re-creating the engine on the same DB resumes with no loss and
no duplication (idempotency keys on effects).
"""

from __future__ import annotations

import os
from datetime import datetime, timezone

from .admission import (
    AdmissionPolicy,
    deterministic_gate,
    new_approval_request,
    verify_approval,
)
from .campaign_adapter import CompositeAdapter, protocol_hash
from .models import (
    ACTIVE_STATES,
    AssignmentState,
    IllegalTransition,
    RouterError,
    TriggerSpec,
    WorkItemState,
    WorkSpec,
    WORK_ITEM_TRANSITIONS,
    new_id,
)
from .store import ControlStore, dumps


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _pid_alive(pid: int | None) -> bool:
    """Owner-alive check for attempt leases (local prototype; see lease.py
    in campaign_runner for the production pattern this mirrors)."""
    if not pid:
        return False
    try:
        os.kill(pid, 0)
        return True
    except PermissionError:
        return True  # exists but owned by another user
    except OSError:
        return False


class ControlEngine:
    def __init__(
        self,
        store: ControlStore,
        policy: AdmissionPolicy,
        adapter: CompositeAdapter | None = None,
    ):
        self.store = store
        self.policy = policy
        self.adapter = adapter or CompositeAdapter()

    # ---------- intake ----------

    def ingest(self, spec: TriggerSpec) -> tuple[dict, bool]:
        """Idempotent intake: (source_id, source_event_id) is unique.

        Returns (trigger_row, created). A repeated event returns the existing
        trigger and never creates a second Work Item downstream.
        """
        with self.store.tx():
            existing = self.store.find_one(
                "triggers",
                "source_id = ? AND source_event_id = ?",
                (spec.source_id, spec.source_event_id),
            )
            if existing:
                return existing, False
            trigger_id = new_id("trg")
            self.store.insert(
                "triggers",
                {
                    "trigger_id": trigger_id,
                    "source_id": spec.source_id,
                    "source_event_id": spec.source_event_id,
                    "actor": spec.actor,
                    "project_hint": spec.project_hint,
                    "payload": dumps(spec.payload),
                    "payload_hash": WorkSpec(
                        project=spec.project_hint, protocol="", scope="", budget=""
                    ).content_hash(),
                    "received_at": _now(),
                },
            )
            return self.store.get("triggers", "trigger_id", trigger_id), True

    # ---------- admission ----------

    def request_work(self, trigger_id: str, work_spec: WorkSpec) -> dict:
        """Deterministic gate, then candidate Work Item + approval request.

        No Mandate exists before an admitted Decision (invariant 3).
        """
        trigger = self.store.get("triggers", "trigger_id", trigger_id)
        if trigger is None:
            raise RouterError(f"unknown trigger: {trigger_id}")
        spec = TriggerSpec(
            source_id=trigger["source_id"],
            source_event_id=trigger["source_event_id"],
            actor=trigger["actor"],
            project_hint=trigger["project_hint"],
        )
        with self.store.tx():
            admitted = self.store.find_one(
                "work_items", "trigger_id = ?", (trigger_id,)
            )
            stopped = admitted and admitted["state"] == WorkItemState.STOPPED
            ok, reason = deterministic_gate(
                spec, self.policy, already_admitted=bool(admitted), human_stopped=stopped
            )
            if not ok:
                self._record_decision(trigger_id, "failed", "rejected", reason)
                raise RouterError(f"admission failed: {reason}")
            self._record_decision(trigger_id, "passed", "needs_approval", reason)
            work_item_id = new_id("wi")
            now = _now()
            self.store.insert(
                "work_items",
                {
                    "work_item_id": work_item_id,
                    "project": work_spec.project,
                    "trigger_id": trigger_id,
                    "mandate_id": None,
                    "state": WorkItemState.CANDIDATE,
                    "protocol": work_spec.protocol,
                    "protocol_hash": protocol_hash(work_spec.protocol),
                    "scope": work_spec.scope,
                    "budget": work_spec.budget,
                    "outcome": None,
                    "thread_id": new_id("thr"),
                    "parent_work_item_id": None,
                    "created_at": now,
                    "updated_at": now,
                },
            )
            req = new_approval_request(work_spec)
            self.store.insert(
                "approvals",
                {
                    "approval_id": new_id("appr"),
                    "work_item_id": work_item_id,
                    "content_hash": req["content_hash"],
                    "nonce": req["nonce"],
                    "approver": None,
                    "state": "pending",
                    "created_at": now,
                    "approved_at": None,
                },
            )
            return {
                "work_item_id": work_item_id,
                "nonce": req["nonce"],
                "content_hash": req["content_hash"],
            }

    def approve(self, work_item_id: str, nonce: str, approver: str) -> dict:
        """Operator-terminal approval (Option A, provisional).

        Binds (nonce, content hash); single-use. On success: admitted Decision,
        Mandate, and candidate -> authorized -> queued in ONE transaction.
        """
        with self.store.tx():
            wi = self._get_work_item(work_item_id)
            work_spec = WorkSpec(
                project=wi["project"],
                protocol=wi["protocol"],
                scope=wi["scope"],
                budget=wi["budget"],
            )
            approval = self.store.find_one(
                "approvals", "work_item_id = ?", (work_item_id,)
            )
            verify_approval(approval, work_spec, nonce)
            now = _now()
            self.store.update(
                "approvals",
                "approval_id",
                approval["approval_id"],
                {"state": "used", "approver": approver, "approved_at": now},
            )
            self._record_decision(
                wi["trigger_id"], "passed", "admitted", f"approved by {approver}"
            )
            mandate_id = new_id("mdt")
            self.store.insert(
                "mandates",
                {
                    "mandate_id": mandate_id,
                    "work_item_id": work_item_id,
                    "issuer": approver,
                    "scope": wi["scope"],
                    "strength": "execute-once",
                    "policy_version": self.policy.policy_version,
                    "state": "active",
                    "created_at": now,
                },
            )
            self.store.update(
                "work_items", "work_item_id", work_item_id, {"mandate_id": mandate_id}
            )
            self._transition(work_item_id, WorkItemState.AUTHORIZED)
            self._transition(work_item_id, WorkItemState.QUEUED)
            return self._get_work_item(work_item_id)

    # ---------- execution ----------

    def recover(self) -> list[str]:
        """Startup recovery sweep: every RUNNING Work Item whose attempt owner
        is dead is marked interrupted and returned to QUEUED. Live owners are
        left alone (a second engine must not steal a running attempt)."""
        recovered = []
        for wi in self.store.all("work_items", "state = ?", (WorkItemState.RUNNING,)):
            self._recover_running(wi["work_item_id"])
            recovered.append(wi["work_item_id"])
        return recovered

    def _recover_running(self, work_item_id: str) -> None:
        running = self.store.all(
            "assignments", "work_item_id = ? AND state = ?",
            (work_item_id, AssignmentState.RUNNING),
        )
        for asg in running:
            if _pid_alive(asg.get("owner_pid")):
                raise RouterError(
                    f"attempt {asg['attempt']} already in progress "
                    f"(owner pid {asg['owner_pid']} is alive)"
                )
        if not running:
            # RUNNING with no live assignment record: treat as interrupted.
            running = []
        with self.store.tx():
            for asg in running:
                self.store.update(
                    "assignments", "assignment_id", asg["assignment_id"],
                    {"state": AssignmentState.INTERRUPTED, "ended_at": _now()},
                )
                self._record_decision(
                    self._get_work_item(work_item_id)["trigger_id"],
                    "passed", "recovered",
                    f"attempt {asg['attempt']} interrupted (owner dead); requeued",
                )
            self._transition(work_item_id, WorkItemState.QUEUED)

    def _last_unfinished_attempt(self, work_item_id: str) -> dict | None:
        """Latest interrupted attempt whose external run may already exist
        (Effect stored, no receipt). Reconciliation candidate."""
        interrupted = self.store.all(
            "assignments", "work_item_id = ? AND state = ?",
            (work_item_id, AssignmentState.INTERRUPTED),
        )
        for asg in sorted(interrupted, key=lambda a: a["attempt"], reverse=True):
            effect = self.store.get(
                "effects", "idempotency_key", f"run:{work_item_id}:{asg['attempt']}"
            )
            if effect and not effect["receipt"]:
                return asg
        return None

    def execute(self, work_item_id: str) -> dict:
        """Run the campaign through the adapter, exactly once.

        Crash safety: (1) a live attempt owner blocks re-execution; (2) a dead
        owner is recovered to QUEUED; (3) before any NEW side effect, an
        interrupted attempt is reconciled against the adapter — if the external
        run already exists, its result is finalized instead of duplicated.
        """
        wi = self._get_work_item(work_item_id)
        if wi["state"] == WorkItemState.COMPLETED:
            effect = self.store.find_one(
                "effects",
                "work_item_id = ? AND kind = ?",
                (work_item_id, "campaign_run"),
            )
            return {"already_completed": True, "receipt": effect["receipt"]}
        if wi["state"] == WorkItemState.RUNNING:
            self._recover_running(work_item_id)
            wi = self._get_work_item(work_item_id)
        mandate = self.store.get("mandates", "mandate_id", wi["mandate_id"] or "")
        if mandate is None:
            raise RouterError("no Mandate: only admitted work may execute")

        previous = self._last_unfinished_attempt(work_item_id)
        if previous is not None:
            result = self.adapter.reconcile(wi, mandate, previous["attempt"])
            if result is not None:
                return self._finalize(
                    work_item_id, previous["attempt"], previous["assignment_id"],
                    result, reconciled=True,
                )
            # No external run found: fall through to a fresh attempt. Unknown
            # submission outcomes belong to human review in the real adapter.

        attempt = (
            len(self.store.all("assignments", "work_item_id = ?", (work_item_id,))) + 1
        )
        idem_key = f"run:{work_item_id}:{attempt}"
        with self.store.tx():
            self._transition(work_item_id, WorkItemState.RUNNING)
            assignment_id = new_id("asg")
            now = _now()
            self.store.insert(
                "assignments",
                {
                    "assignment_id": assignment_id,
                    "work_item_id": work_item_id,
                    "executor_kind": "composite-adapter",
                    "attempt": attempt,
                    "state": AssignmentState.RUNNING,
                    "owner_pid": os.getpid(),
                    "created_at": now,
                    "ended_at": None,
                },
            )
            self.store.insert(
                "effects",
                {
                    "effect_id": new_id("eff"),
                    "work_item_id": work_item_id,
                    "kind": "campaign_run",
                    "idempotency_key": idem_key,
                    "payload": dumps({"protocol": wi["protocol"], "attempt": attempt}),
                    "state": "stored",
                    "receipt": None,
                    "created_at": now,
                },
            )
        # Side effect happens AFTER the effect is durably stored.
        result = self.adapter.run(wi, mandate, attempt=attempt)
        return self._finalize(work_item_id, attempt, assignment_id, result)

    def _finalize(
        self,
        work_item_id: str,
        attempt: int,
        assignment_id: str,
        result: dict,
        reconciled: bool = False,
    ) -> dict:
        """Post-side-effect writes: receipt, assignment close, evidence, state."""
        idem_key = f"run:{work_item_id}:{attempt}"
        with self.store.tx():
            self.store.update(
                "effects",
                "idempotency_key",
                idem_key,
                {"state": "done", "receipt": dumps(result["receipt"])},
            )
            self.store.update(
                "assignments",
                "assignment_id",
                assignment_id,
                {"state": AssignmentState.ENDED, "ended_at": _now()},
            )
            self._link_evidence(work_item_id, "run_record", result["run_id"], None)
            self._link_evidence(
                work_item_id,
                "gate_report",
                result["run_id"],
                dumps(result["gate_report"]),
            )
            if result.get("candidate_file"):
                self._link_evidence(
                    work_item_id, "ranked_candidates", result["run_id"], result["candidate_file"]
                )
            wi = self._get_work_item(work_item_id)
            if wi["state"] == WorkItemState.QUEUED:
                self._transition(work_item_id, WorkItemState.RUNNING)
            self._transition(
                work_item_id,
                WorkItemState.COMPLETED
                if result["outcome"] == "completed"
                else WorkItemState.FAILED,
            )
            self.store.update(
                "work_items", "work_item_id", work_item_id, {"outcome": result["outcome"]}
            )
        return {"already_completed": False, "result": result, "reconciled": reconciled}

    def reply(self, work_item_id: str, text: str) -> dict:
        """One receipted reply per Work Item (durable outbox record)."""
        self._get_work_item(work_item_id)
        idem_key = f"reply:{work_item_id}"
        with self.store.tx():
            existing = self.store.get("effects", "idempotency_key", idem_key)
            if existing:
                return {"already_sent": True, "receipt": existing["receipt"]}
            receipt = dumps({"work_item_id": work_item_id, "delivered": True})
            self.store.insert(
                "effects",
                {
                    "effect_id": new_id("eff"),
                    "work_item_id": work_item_id,
                    "kind": "reply",
                    "idempotency_key": idem_key,
                    "payload": dumps({"text": text}),
                    "state": "done",
                    "receipt": receipt,
                    "created_at": _now(),
                },
            )
            return {"already_sent": False, "receipt": receipt}

    def stop(self, work_item_id: str, by: str) -> dict:
        """Human stop: authoritative, prevents automatic resume (invariant 8)."""
        with self.store.tx():
            self._transition(work_item_id, WorkItemState.STOPPED)
            self._record_decision(
                self._get_work_item(work_item_id)["trigger_id"],
                "passed",
                "stopped",
                f"human stop by {by}",
            )
            return self._get_work_item(work_item_id)

    # ---------- follow-ups and wake-up context ----------

    def follow_up(self, work_item_id: str, actor: str, text: str, work_spec: WorkSpec | None = None) -> dict:
        """Route a human follow-up.

        ACTIVE Work Item: a context-only thread post (no new authority, no new
        execution). TERMINAL Work Item (completed/failed/stopped): never
        resurrect — open a NEW Work Item linked by Thread + parent, with fresh
        approval (assessment: follow-up ownership and hand-back).
        """
        wi = self._get_work_item(work_item_id)
        if wi["state"] in ACTIVE_STATES:
            with self.store.tx():
                post_id = new_id("post")
                self.store.insert(
                    "thread_posts",
                    {
                        "post_id": post_id,
                        "work_item_id": work_item_id,
                        "actor": actor,
                        "text": text,
                        "delivered": 0,
                        "created_at": _now(),
                    },
                )
                return {"kind": "post", "work_item_id": work_item_id, "post_id": post_id}

        spec = work_spec or WorkSpec(
            project=wi["project"], protocol=wi["protocol"], scope=wi["scope"], budget=wi["budget"]
        )
        trigger, _ = self.ingest(
            TriggerSpec(
                source_id=f"followup:{work_item_id}",
                source_event_id=new_id("evt"),
                actor=actor,
                project_hint=wi["project"],
                payload={"text": text, "parent": work_item_id},
            )
        )
        req = self.request_work(trigger["trigger_id"], spec)
        with self.store.tx():
            self.store.update(
                "work_items",
                "work_item_id",
                req["work_item_id"],
                {
                    "thread_id": wi["thread_id"] or work_item_id,
                    "parent_work_item_id": work_item_id,
                },
            )
        return {"kind": "work_item", **req, "parent": work_item_id}

    def context_package(self, work_item_id: str, k: int = 3, mark_delivered: bool = True) -> dict:
        """Bounded rolling wake-up package (NOT a transcript replay).

        Contract: Work Item + Mandate + latest K attempt receipts + undelivered
        thread posts (the delta) + evidence index + parent chain. Size stays
        ~constant across long campaigns; older history via evidence handles.
        """
        wi = self._get_work_item(work_item_id)
        mandate = self.store.get("mandates", "mandate_id", wi["mandate_id"] or "")
        effects = self.store.all(
            "effects", "work_item_id = ? AND kind = ?", (work_item_id, "campaign_run")
        )
        attempts = sorted(effects, key=lambda e: e["created_at"], reverse=True)[:k]
        posts = self.store.all(
            "thread_posts", "work_item_id = ? AND delivered = 0", (work_item_id,)
        )
        links = self.store.all("evidence_links", "work_item_id = ?", (work_item_id,))
        chain, cursor = [], wi["parent_work_item_id"]
        while cursor and len(chain) < 5:
            chain.append(cursor)
            parent = self.store.get("work_items", "work_item_id", cursor)
            cursor = parent["parent_work_item_id"] if parent else None
        if mark_delivered and posts:
            with self.store.tx():
                for post in posts:
                    self.store.update(
                        "thread_posts", "post_id", post["post_id"], {"delivered": 1}
                    )
        return {
            "work_item": wi,
            "mandate": mandate,
            "latest_attempts": [
                {"key": e["idempotency_key"], "state": e["state"], "receipt": e["receipt"]}
                for e in attempts
            ],
            "delta": {"new_posts": posts, "current_state": wi["state"]},
            "evidence_index": [
                {"kind": l["kind"], "ref_id": l["ref_id"], "path": l["path"]} for l in links
            ],
            "parent_chain": chain,
            "bounds": {"attempts": k, "parent_depth": 5},
        }

    # ---------- internals ----------

    def _get_work_item(self, work_item_id: str) -> dict:
        wi = self.store.get("work_items", "work_item_id", work_item_id)
        if wi is None:
            raise RouterError(f"unknown work item: {work_item_id}")
        return wi

    def _transition(self, work_item_id: str, new_state: str) -> None:
        wi = self._get_work_item(work_item_id)
        current = wi["state"]
        if new_state not in WORK_ITEM_TRANSITIONS[current]:
            raise IllegalTransition(f"{current} -> {new_state} is not legal")
        if not self.store.cas_state(work_item_id, current, new_state, _now()):
            raise IllegalTransition(f"state drifted concurrently at {current}")

    def _record_decision(self, trigger_id: str, gate: str, outcome: str, reason: str):
        self.store.insert(
            "decisions",
            {
                "decision_id": new_id("dec"),
                "trigger_id": trigger_id,
                "gate_result": gate,
                "outcome": outcome,
                "reason": reason,
                "policy_version": self.policy.policy_version,
                "created_at": _now(),
            },
        )

    def _link_evidence(self, work_item_id: str, kind: str, ref_id: str, detail: str | None):
        self.store.insert(
            "evidence_links",
            {
                "work_item_id": work_item_id,
                "kind": kind,
                "ref_id": ref_id,
                "path": None,
                "hash": detail,
            },
        )
