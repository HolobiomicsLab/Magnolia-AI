"""Domain objects and legal state transitions for the Router prototype.

State names follow assessment section 8.2 and are PROVISIONAL (decision #2).
The transition table is code, never prompt text.
"""

from __future__ import annotations

import hashlib
import json
import uuid
from dataclasses import dataclass, field


def new_id(prefix: str) -> str:
    return f"{prefix}_{uuid.uuid4().hex[:12]}"


def canonical_hash(obj: dict) -> str:
    """Content hash used to bind approvals and protocols to exact content."""
    return hashlib.sha256(
        json.dumps(obj, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()


class WorkItemState:
    CANDIDATE = "candidate"
    AUTHORIZED = "authorized"
    QUEUED = "queued"
    RUNNING = "running"
    WAITING_HUMAN = "waiting_human"
    COMPLETED = "completed"
    FAILED = "failed"
    STOPPED = "stopped"
    IGNORED = "ignored"


# Legal Work Item transitions (assessment section 8.2, provisional).
WORK_ITEM_TRANSITIONS = {
    WorkItemState.CANDIDATE: {WorkItemState.AUTHORIZED, WorkItemState.IGNORED},
    WorkItemState.AUTHORIZED: {WorkItemState.QUEUED, WorkItemState.STOPPED},
    WorkItemState.QUEUED: {WorkItemState.RUNNING, WorkItemState.STOPPED},
    WorkItemState.RUNNING: {
        WorkItemState.COMPLETED,
        WorkItemState.FAILED,
        WorkItemState.WAITING_HUMAN,
        WorkItemState.STOPPED,
        # Recovery edge: an interrupted attempt (dead owner) is requeued.
        WorkItemState.QUEUED,
    },
    WorkItemState.WAITING_HUMAN: {WorkItemState.QUEUED, WorkItemState.STOPPED},
    WorkItemState.COMPLETED: set(),
    WorkItemState.FAILED: set(),
    WorkItemState.STOPPED: set(),  # a human stop prevents automatic resume
    WorkItemState.IGNORED: set(),
}

ACTIVE_STATES = {
    WorkItemState.CANDIDATE,
    WorkItemState.AUTHORIZED,
    WorkItemState.QUEUED,
    WorkItemState.RUNNING,
    WorkItemState.WAITING_HUMAN,
}


class AssignmentState:
    PENDING = "pending"
    RUNNING = "running"
    ENDED = "ended"
    # A crashed attempt: owner process died before finalizing. Recovery may
    # reconcile its external run instead of blindly re-executing.
    INTERRUPTED = "interrupted"


@dataclass(frozen=True)
class TriggerSpec:
    """One immutable input event (assessment section 8.1)."""

    source_id: str
    source_event_id: str
    actor: str
    project_hint: str
    payload: dict = field(default_factory=dict)


@dataclass(frozen=True)
class WorkSpec:
    """The exact content a human approval is bound to."""

    project: str
    protocol: str
    scope: str
    budget: str

    def as_dict(self) -> dict:
        return {
            "project": self.project,
            "protocol": self.protocol,
            "scope": self.scope,
            "budget": self.budget,
        }

    def content_hash(self) -> str:
        return canonical_hash(self.as_dict())


class RouterError(Exception):
    """Base class for control-plane rejections."""


class IllegalTransition(RouterError):
    pass


class ApprovalError(RouterError):
    pass


class AdmissionError(RouterError):
    pass
