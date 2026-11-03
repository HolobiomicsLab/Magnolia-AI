"""Fake campaign adapter for the vertical slice.

Only pre-registered protocols may run (assessment section 17: the adapter
exposes a narrow interface, never arbitrary protocol or shell execution).
The adapter is deterministic, uses no LLM, and submits no real jobs.

'hsc70-met-proxy-v1' stands in for the future live pilot protocol (decision
#5: hsc70 with an existing approved protocol; Nle explicitly out of scope).
"""

from __future__ import annotations

from .models import RouterError, canonical_hash, new_id

PROTOCOLS = {
    "hsc70-met-proxy-v1": {
        "kind": "docking-campaign",
        "stages": ["setup", "dock", "score"],
        "oracle": "fake-known-answer",
    },
}


def protocol_hash(name: str) -> str:
    if name not in PROTOCOLS:
        raise RouterError(f"protocol not pre-registered: {name}")
    return canonical_hash({"name": name, "definition": PROTOCOLS[name]})


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
