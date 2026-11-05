"""Demonstration driver for the Router vertical slice.

Run from opencode_cc_mem/:
    python3 -m router_runner.cli demo [--db /tmp/router-demo.db]

Walks the assessment section-20 slice with printed evidence at each step,
including negative cases (forged approval, duplicate intake, illegal resume)
and a simulated restart. No LLM, no real jobs, no network.
"""

from __future__ import annotations

import argparse
import os
import sys

from .admission import AdmissionPolicy
from .engine import ControlEngine
from .models import (
    ApprovalError,
    IllegalTransition,
    RouterError,
    TriggerSpec,
    WorkSpec,
)
from .store import ControlStore


def build_engine(db_path: str) -> ControlEngine:
    policy = AdmissionPolicy(
        allowed_senders=frozenset({os.environ.get("MAGNOLIA_AGENTD_OPERATOR", "operator")}),
        allowed_projects=frozenset({"demo"}),
    )
    return ControlEngine(ControlStore(db_path), policy)


def step(title: str) -> None:
    print(f"\n--- {title} ---")


def demo(db_path: str) -> int:
    engine = build_engine(db_path)
    print(f"control store: {db_path}")

    step("1. Ingest one synthetic idea letter as an immutable Trigger")
    letter = TriggerSpec(
        source_id="inbox:demo",
        source_event_id="letter-2026-11-03-001",
        actor=os.environ.get("MAGNOLIA_AGENTD_OPERATOR", "operator"),
        project_hint="demo",
        payload={"title": "Demo: run a docking pilot", "body": "synthetic demo"},
    )
    trigger, created = engine.ingest(letter)
    print(f"trigger {trigger['trigger_id']} created={created}")

    step("2. Duplicate intake is idempotent (same source event)")
    dup, created_dup = engine.ingest(letter)
    assert dup["trigger_id"] == trigger["trigger_id"] and not created_dup
    print(f"same trigger returned ({dup['trigger_id']}), created={created_dup}")

    step("3. Deterministic gate + candidate Work Item + approval request")
    spec = WorkSpec(
        project="demo",
        protocol="demo-docking-v1",
        scope="one fake docking campaign",
        budget="0 core-hours (fake adapter)",
    )
    req = engine.request_work(trigger["trigger_id"], spec)
    wi = req["work_item_id"]
    print(f"work item {wi} state=candidate")
    print(f"approval nonce={req['nonce'][:12]}... content_hash={req['content_hash'][:16]}...")

    step("4. Forged approval (wrong nonce) is rejected")
    try:
        engine.approve(wi, nonce="0" * 32, approver="attacker")
    except ApprovalError as e:
        print(f"rejected as expected: {e}")

    step("5. Operator-terminal approval -> Decision + Mandate -> queued")
    wi_row = engine.approve(wi, nonce=req["nonce"], approver=os.environ.get("MAGNOLIA_AGENTD_OPERATOR", "operator") + " (simulated)")
    print(f"state={wi_row['state']} mandate={wi_row['mandate_id']}")

    step("6. Execute: Effect stored BEFORE the run; receipt + evidence links")
    outcome = engine.execute(wi)
    result = outcome["result"]
    print(f"run={result['run_id']} outcome={result['outcome']}")
    print(f"gates={result['gate_report']['gates_passed']}")

    step("7. Re-execution is idempotent (no duplicate campaign)")
    again = engine.execute(wi)
    print(f"already_completed={again['already_completed']} (same stored receipt)")

    step("8. One receipted reply; second reply is not re-sent")
    print(engine.reply(wi, "pilot campaign completed (fake)"))
    print(engine.reply(wi, "pilot campaign completed (fake)"))

    step("9. Restart: new engine on the same DB recovers everything")
    engine.store.close()
    engine2 = build_engine(db_path)
    wi2 = engine2.store.get("work_items", "work_item_id", wi)
    links = engine2.store.all("evidence_links", "work_item_id = ?", (wi,))
    print(f"after restart: state={wi2['state']} outcome={wi2['outcome']} evidence_links={len(links)}")

    step("10. Illegal transition is rejected (completed -> stopped)")
    try:
        engine2.stop(wi, by=os.environ.get("MAGNOLIA_AGENTD_OPERATOR", "operator"))
    except IllegalTransition as e:
        print(f"rejected as expected: {e}")

    step("11. Human stop prevents automatic resume (second letter)")
    letter2 = TriggerSpec(
        source_id="inbox:demo",
        source_event_id="letter-2026-11-03-002",
        actor=os.environ.get("MAGNOLIA_AGENTD_OPERATOR", "operator"),
        project_hint="demo",
        payload={"title": "second idea"},
    )
    t2, _ = engine2.ingest(letter2)
    req2 = engine2.request_work(t2["trigger_id"], spec)
    engine2.approve(req2["work_item_id"], req2["nonce"], os.environ.get("MAGNOLIA_AGENTD_OPERATOR", "operator") + " (simulated)")
    engine2.stop(req2["work_item_id"], by=os.environ.get("MAGNOLIA_AGENTD_OPERATOR", "operator"))
    print(f"work item {req2['work_item_id']} state=stopped")
    try:
        engine2.execute(req2["work_item_id"])
    except IllegalTransition as e:
        print(f"resume rejected as expected: {e}")

    step("Summary")
    for table in ("triggers", "work_items", "mandates", "decisions", "effects", "evidence_links"):
        print(f"  {table}: {len(engine2.store.all(table))}")
    engine2.store.close()
    print("\nDEMO OK")
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("command", choices=["demo"])
    parser.add_argument("--db", default="/tmp/router-demo.db")
    args = parser.parse_args()
    if args.command == "demo":
        return demo(args.db)
    return 1


if __name__ == "__main__":
    sys.exit(main())
