"""Fake campaign adapter for the vertical slice.

Only pre-registered protocols may run (assessment section 17: the adapter
exposes a narrow interface, never arbitrary protocol or shell execution).
The adapter is deterministic, uses no LLM, and submits no real jobs.

'hsc70-met-proxy-v1' stands in for the future live pilot protocol (decision
#5: hsc70 with an existing approved protocol; Nle explicitly out of scope).
"""

from __future__ import annotations

from .models import RouterError, canonical_hash, new_id
from .taskpacks import TASK_PACKS, run_task_pack, task_pack_hash

PROTOCOLS = {
    "hsc70-met-proxy-v1": {
        "kind": "docking-campaign",
        "stages": ["setup", "dock", "score"],
        "oracle": "fake-known-answer",
    },
}


def protocol_hash(name: str) -> str:
    if name in PROTOCOLS:
        return canonical_hash({"name": name, "definition": PROTOCOLS[name]})
    if name in TASK_PACKS:
        return task_pack_hash(name)
    raise RouterError(f"protocol not pre-registered: {name}")


class FakeCampaignAdapter:
    def run(self, work_item: dict, mandate: dict) -> dict:
        """Produce a deterministic run record + gate report + receipt.

        In the real system this is the campaign supervisor's job; the Router
        never advances campaign state itself (invariant 5).
        """
        name = work_item["protocol"]
        phash = protocol_hash(name)  # raises if not pre-registered
        run_id = new_id("run")
        gate_report = {
            "gates_passed": ["startup_approval", "prejob_check", "fake_oracle"],
            "oracle": PROTOCOLS[name]["oracle"],
        }
        return {
            "run_id": run_id,
            "protocol": name,
            "protocol_hash": phash,
            "outcome": "completed",
            "gate_report": gate_report,
            "receipt": {
                "work_item_id": work_item["work_item_id"],
                "mandate_id": mandate["mandate_id"],
                "run_id": run_id,
                "scope": mandate["scope"],
                "result": "fake-known-answer-ok",
            },
        }


class TaskPackAdapter:
    """Runs real (local) task-pack stages; no Slurm, no external side effects."""

    def __init__(self, out_root=None):
        self.out_root = out_root

    def run(self, work_item: dict, mandate: dict) -> dict:
        return run_task_pack(work_item["protocol"], work_item, mandate, self.out_root)


class CompositeAdapter:
    """Dispatch: fake protocols for control-plane demos, task packs for real work."""

    def __init__(self, out_root=None):
        self.fake = FakeCampaignAdapter()
        self.packs = TaskPackAdapter(out_root)

    def run(self, work_item: dict, mandate: dict) -> dict:
        if work_item["protocol"] in PROTOCOLS:
            return self.fake.run(work_item, mandate)
        return self.packs.run(work_item, mandate)
