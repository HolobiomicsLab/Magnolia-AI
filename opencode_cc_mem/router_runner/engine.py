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
from .job_service import TERMINAL_JOB_STATES
from .models import (
    ACTIVE_STATES,
    ApprovalError,
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
        job_service=None,
    ):
        self.store = store
        self.policy = policy
        self.adapter = adapter or CompositeAdapter()
        self.job_service = job_service

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
                    "job_run_id": None,
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
            if result["outcome"] == "completed":
                self._promote_findings(work_item_id)
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
            "findings_digest": self._findings_digest(work_item_id),
            "parent_chain": chain,
            "bounds": {"attempts": k, "parent_depth": 5},
        }

    def _findings_digest(self, work_item_id: str) -> dict:
        """Wake-ups see validated knowledge preferentially, intermediates as counts."""
        rows = self.findings(work_item_id)
        counts = {}
        for row in rows:
            counts[row["outcome_status"]] = counts.get(row["outcome_status"], 0) + 1
        return {
            "counts": counts,
            "final": [
                {"finding_id": r["finding_id"], "kind": r["kind"],
                 "status": r["outcome_status"], "text": r["text"]}
                for r in rows if r["outcome_status"] in self.FINAL_STATUSES
            ][:5],
        }

    # ---------- job parking and wake-up ----------

    def park_on_job(self, work_item_id: str, run_id: str) -> dict:
        """Worker submitted a cluster job: park, don't wait. No process stays
        alive — the job watcher (poller) owns the waiting from here."""
        with self.store.tx():
            wi = self._get_work_item(work_item_id)
            if wi["state"] == WorkItemState.QUEUED:
                self._transition(work_item_id, WorkItemState.RUNNING)
            self.store.update(
                "work_items", "work_item_id", work_item_id, {"job_run_id": run_id}
            )
            self._transition(work_item_id, WorkItemState.WAITING_JOB)
            self._record_decision(
                wi["trigger_id"], "passed", "parked", f"waiting on job {run_id}"
            )
            return self._get_work_item(work_item_id)

    def handle_job_notice(self, notice: dict) -> dict:
        """Consume one job_notice: wake the owning Work Item (WAITING_JOB ->
        QUEUED), attach evidence + a delta post. Idempotent per source event;
        notices without an owner are left for other consumers."""
        run_id = notice["run_id"]
        state = notice.get("state", "")
        event_key = (notice.get("event") or {}).get("source_event_id") or f"{run_id}:{state}"
        with self.store.tx():
            if self.store.get("consumed_notices", "source_event_id", event_key):
                return {"consumed": False, "reason": "duplicate", "run_id": run_id}
            wi = self.store.find_one(
                "work_items",
                "job_run_id = ? AND state = ?",
                (run_id, WorkItemState.WAITING_JOB),
            )
            if wi is None:
                return {"consumed": False, "reason": "no_owner", "run_id": run_id}
            work_item_id = wi["work_item_id"]
            self.store.insert(
                "consumed_notices",
                {"source_event_id": event_key, "run_id": run_id, "consumed_at": _now()},
            )
            self._link_evidence(work_item_id, "job_notice", run_id, state)
            self.store.insert(
                "thread_posts",
                {
                    "post_id": new_id("post"),
                    "work_item_id": work_item_id,
                    "actor": "system:job-watcher",
                    "text": f"job {run_id} terminal: {state} ({notice.get('category', '?')})",
                    "delivered": 0,
                    "created_at": _now(),
                },
            )
            self._record_decision(
                wi["trigger_id"], "passed", "job_notice", f"{run_id} -> {state}"
            )
            self._transition(work_item_id, WorkItemState.QUEUED)
            return {"consumed": True, "work_item_id": work_item_id, "state": state}

    def consume_job_notices(self, inbox_dir, include_context: bool = False) -> dict:
        """Sweep an inbox/from-jobs directory of notice JSON files."""
        import json as _json
        from pathlib import Path as _Path

        results = []
        base = _Path(inbox_dir)
        scan_dir = base / "from-jobs" if (base / "from-jobs").is_dir() else base
        for path in sorted(scan_dir.glob("*.json")):
            try:
                notice = _json.loads(path.read_text())
                outcome = self.handle_job_notice(notice)
                outcome["notice_file"] = path.name
            except Exception as exc:  # malformed notice must not block the sweep
                outcome = {"consumed": False, "reason": f"error: {exc}", "notice_file": path.name}
            results.append(outcome)
        summary = {
            "woken": [r["work_item_id"] for r in results if r.get("consumed")],
            "results": results,
        }
        if include_context:
            summary["context_packages"] = {
                wi: self.context_package(wi) for wi in summary["woken"]
            }
        return summary

    # ---------- findings with outcome status (assessment §11.5) ----------

    FINDING_KINDS = frozenset({"scientific", "operational"})
    OUTCOME_STATUSES = frozenset({"intermediate", "completed_campaign", "human_validated"})
    FINAL_STATUSES = frozenset({"completed_campaign", "human_validated"})

    def record_finding(
        self,
        work_item_id: str,
        kind: str,
        text: str,
        outcome_status: str = "intermediate",
        attempt: int | None = None,
    ) -> dict:
        """Write-path enforcement: every finding carries kind + outcome_status.

        Default is INTERMEDIATE (an observation from one attempt) — an
        intermediate success must never silently become a durable rule.
        """
        if kind not in self.FINDING_KINDS:
            raise RouterError(f"finding kind must be one of {sorted(self.FINDING_KINDS)}")
        if outcome_status not in self.OUTCOME_STATUSES:
            raise RouterError(
                f"outcome_status must be one of {sorted(self.OUTCOME_STATUSES)}"
            )
        self._get_work_item(work_item_id)
        if attempt is None:
            attempts = self.store.all("assignments", "work_item_id = ?", (work_item_id,))
            attempt = max((a["attempt"] for a in attempts), default=1)
        with self.store.tx():
            finding_id = new_id("fnd")
            self.store.insert(
                "findings",
                {
                    "finding_id": finding_id,
                    "work_item_id": work_item_id,
                    "attempt": attempt,
                    "kind": kind,
                    "outcome_status": outcome_status,
                    "text": text,
                    "created_at": _now(),
                },
            )
            self._link_evidence(work_item_id, "finding", finding_id, outcome_status)
            return self.store.get("findings", "finding_id", finding_id)

    def _promote_findings(self, work_item_id: str) -> int:
        """On COMPLETED outcome only: intermediate -> completed_campaign."""
        promoted = self.store.all(
            "findings",
            "work_item_id = ? AND outcome_status = ?",
            (work_item_id, "intermediate"),
        )
        for finding in promoted:
            self.store.update(
                "findings", "finding_id", finding["finding_id"],
                {"outcome_status": "completed_campaign"},
            )
        if promoted:
            wi = self._get_work_item(work_item_id)
            self._record_decision(
                wi["trigger_id"], "passed", "findings_promoted",
                f"{len(promoted)} finding(s) -> completed_campaign",
            )
        return len(promoted)

    def validate_finding(self, finding_id: str, approver: str) -> dict:
        """Human validation: the only path to the strongest lesson status."""
        finding = self.store.get("findings", "finding_id", finding_id)
        if finding is None:
            raise RouterError(f"unknown finding: {finding_id}")
        with self.store.tx():
            self.store.update(
                "findings", "finding_id", finding_id,
                {"outcome_status": "human_validated"},
            )
            self._record_decision(
                self._get_work_item(finding["work_item_id"])["trigger_id"],
                "passed", "finding_validated", f"{finding_id} by {approver}",
            )
            return self.store.get("findings", "finding_id", finding_id)

    def findings(
        self,
        work_item_id: str | None = None,
        kind: str | None = None,
        final_only: bool = False,
    ) -> list[dict]:
        """Read-path enforcement for protocol decisions: final_only excludes
        intermediate observations from guidance use."""
        clauses, params = ["1=1"], []
        if work_item_id:
            clauses.append("work_item_id = ?")
            params.append(work_item_id)
        if kind:
            clauses.append("kind = ?")
            params.append(kind)
        rows = self.store.all("findings", " AND ".join(clauses), tuple(params))
        if final_only:
            rows = [r for r in rows if r["outcome_status"] in self.FINAL_STATUSES]
        return rows

    def cancel_and_resubmit(
        self,
        work_item_id: str,
        new_spec: WorkSpec,
        reason: str,
        approver: str | None,
    ) -> dict:
        """The authorized kill/cleanup/resubmit path for mid-wait new information.

        Order of operations (each step is a recorded decision):
          1. Requires WAITING_JOB and an operator approver — a text message by
             itself never kills anything, and a spec change means new content.
          2. Terminal race: if the job already finished, do NOT cancel, clean,
             or resubmit — the results get fetched via the normal notice path.
          3. cancel() then directory-scoped cleanup() on the OLD run only.
          4. Record the amendment with the NEW content hash and approver.
          5. Resubmit as a NEW run; link the old run as superseded.
        """
        wi = self._get_work_item(work_item_id)
        if wi["state"] != WorkItemState.WAITING_JOB:
            raise RouterError(
                f"cancel_and_resubmit requires waiting_job, got {wi['state']}"
            )
        if approver is None:
            raise ApprovalError(
                "parameter change requires operator re-approval (approver=None)"
            )
        if self.job_service is None:
            raise RouterError("no job service configured")
        run_id = wi["job_run_id"]
        service = self.job_service

        if service.status(run_id) in TERMINAL_JOB_STATES:
            with self.store.tx():
                self._record_decision(
                    wi["trigger_id"], "passed", "cancel_skipped",
                    f"{run_id} already terminal; fetch instead of cancel/cleanup",
                )
            return {"resubmitted": False, "reason": "already_terminal", "run_id": run_id}

        with self.store.tx():
            service.cancel(run_id)
            self._record_decision(
                wi["trigger_id"], "passed", "cancelled", f"{run_id}: {reason}"
            )
            cleaned = service.cleanup(run_id)
            self._record_decision(
                wi["trigger_id"], "passed", "cleaned", f"{cleaned} (per-run dir only)"
            )
            new_hash = new_spec.content_hash()
            self._record_decision(
                wi["trigger_id"], "passed", "amended",
                f"{reason}; approved by {approver}; new content hash {new_hash[:16]}",
            )
            self.store.update(
                "work_items",
                "work_item_id",
                work_item_id,
                {
                    "project": new_spec.project,
                    "protocol": new_spec.protocol,
                    "protocol_hash": protocol_hash(new_spec.protocol),
                    "scope": new_spec.scope,
                    "budget": new_spec.budget,
                },
            )
            new_job = service.submit(new_id("run"), spec=new_spec.as_dict())
            new_run_id = new_job["run_id"]
            self.store.update(
                "work_items", "work_item_id", work_item_id, {"job_run_id": new_run_id}
            )
            self._link_evidence(work_item_id, "superseded_run", run_id, new_run_id)
            self._record_decision(
                wi["trigger_id"], "passed", "resubmitted",
                f"{run_id} -> {new_run_id}; state=waiting_job",
            )
        return {
            "resubmitted": True,
            "cancelled": run_id,
            "cleaned": cleaned,
            "new_run_id": new_run_id,
            "superseded": run_id,
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
