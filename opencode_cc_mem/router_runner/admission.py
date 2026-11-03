"""Deterministic admission gate and content-bound human approval.

PROVISIONAL (decision #4): approval model is Option A (operator-terminal).
An approval request binds (content_hash, nonce); approval is single-use and
only valid for the exact WorkSpec content. A signature field can later carry
Option B (signed approval artifacts) without changing the Work Item model.

Free-text approver strings from letters are never accepted: approval only
happens through ControlEngine.approve() with the nonce shown to the operator.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field

from .models import AdmissionError, ApprovalError, TriggerSpec, WorkSpec


@dataclass(frozen=True)
class AdmissionPolicy:
    allowed_senders: frozenset[str]
    allowed_projects: frozenset[str]
    policy_version: str = "v0.1-provisional"


def deterministic_gate(
    trigger: TriggerSpec,
    policy: AdmissionPolicy,
    already_admitted: bool,
    human_stopped: bool,
) -> tuple[bool, str]:
    """The deterministic fast path. The model (later phases) may never
    override a failed gate. No LLM is involved anywhere in this prototype."""
    if not trigger.source_id or not trigger.source_event_id:
        return False, "schema: missing source identity"
    if not trigger.actor:
        return False, "schema: missing actor"
    if trigger.actor not in policy.allowed_senders:
        return False, f"sender not allowed: {trigger.actor}"
    if trigger.project_hint not in policy.allowed_projects:
        return False, f"project not allowed: {trigger.project_hint}"
    if already_admitted:
        return False, "duplicate: source event already admitted"
    if human_stopped:
        return False, "human stop: automatic resume denied"
    return True, "ok"


def new_approval_request(work_spec: WorkSpec) -> dict:
    """Issue a single-use approval request bound to the exact work content."""
    return {
        "nonce": uuid.uuid4().hex,
        "content_hash": work_spec.content_hash(),
    }


def verify_approval(approval_row: dict, work_spec: WorkSpec, nonce: str) -> None:
    """Raise ApprovalError unless the operator confirms the exact request."""
    if approval_row is None:
        raise ApprovalError("no approval request exists for this work item")
    if approval_row["state"] != "pending":
        raise ApprovalError(f"approval already {approval_row['state']} (single-use)")
    if nonce != approval_row["nonce"]:
        raise ApprovalError("nonce mismatch: approval not issued by the operator")
    if work_spec.content_hash() != approval_row["content_hash"]:
        raise ApprovalError(
            "content changed since the approval request: re-approval required"
        )
