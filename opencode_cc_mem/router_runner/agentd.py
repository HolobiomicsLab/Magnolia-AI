"""agentd — the always-on agent system daemon (availability-first).

Assessment section 14: ALWAYS-ON AVAILABILITY (a deterministic daemon listens
for events) is reasonable now; always-on AUTONOMOUS EXECUTION is not. agentd
therefore ingests letters, applies the deterministic gate, and stages approval
requests — it NEVER executes work on its own. Execution requires an explicit
operator-terminal approval (Option A) followed by `agentd execute`.

Letters are JSON files in an inbox directory:
{
  "source_id": "inbox:literature",
  "source_event_id": "letter-001",
  "actor": "human:tao",
  "project_hint": "nk2r_challenge",
  "payload": {"title": "..."},
  "work_spec": {"project": "nk2r_challenge", "protocol": "nk2r-challenge-v0.1",
                "scope": "...", "budget": "..."}
}

Usage (from opencode_cc_mem/):
    python3 -m router_runner.agentd init --db /tmp/agentd.db
    python3 -m router_runner.agentd watch --inbox <dir> --db /tmp/agentd.db [--once]
    python3 -m router_runner.agentd ingest <letter.json> --db /tmp/agentd.db
    python3 -m router_runner.agentd approve <work_item_id> <nonce> --db /tmp/agentd.db
    python3 -m router_runner.agentd execute <work_item_id> --db /tmp/agentd.db
    python3 -m router_runner.agentd demo-nk2r --db /tmp/agentd-demo.db
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from .admission import AdmissionPolicy
from .campaign_adapter import CompositeAdapter
from .engine import ControlEngine
from .models import RouterError, TriggerSpec, WorkSpec
from .store import ControlStore


def default_policy() -> AdmissionPolicy:
    return AdmissionPolicy(
        allowed_senders=frozenset({"human:tao"}),
        allowed_projects=frozenset({"hsc70", "xiulian", "literature", "nk2r_challenge"}),
    )


def get_engine(db: str, out_root: str | None = None) -> ControlEngine:
    engine = ControlEngine(
        ControlStore(db), default_policy(), CompositeAdapter(out_root=out_root)
    )
    # Startup recovery sweep: interrupted attempts are requeued, never stolen
    # from a live owner. Runs on every daemon (re)start.
    engine.recover()
    return engine


def ingest_letter(engine: ControlEngine, letter: dict) -> dict:
    spec = TriggerSpec(
        source_id=letter["source_id"],
        source_event_id=letter["source_event_id"],
        actor=letter["actor"],
        project_hint=letter["project_hint"],
        payload=letter.get("payload", {}),
    )
    trigger, created = engine.ingest(spec)
    work = WorkSpec(**letter["work_spec"])
    request = engine.request_work(trigger["trigger_id"], work)
    return {"trigger": trigger["trigger_id"], "created": created, **request}


def watch(inbox: Path, db: str, once: bool = False, out_root: str | None = None) -> dict:
    """Scan letter files; stage candidates for approval. Never executes."""
    engine = get_engine(db, out_root)
    staged, errors = [], []
    for path in sorted(Path(inbox).glob("*.json")):
        try:
            result = ingest_letter(engine, json.loads(path.read_text()))
            staged.append({"letter": path.name, **result})
        except RouterError as exc:
            errors.append({"letter": path.name, "reason": str(exc)})
    summary = {"staged": staged, "rejected": errors, "executed": 0}
    print(json.dumps(summary, indent=2))
    # A real long-running loop would sleep and rescan here; --once is the
    # testable single sweep. No LLM, no autonomous execution in either mode.
    return summary


NK2R_LETTER = {
    "source_id": "inbox:literature",
    "source_event_id": "letter-nk2r-2026-11-03",
    "actor": "human:tao",
    "project_hint": "nk2r_challenge",
    "payload": {"title": "Try the NK2R peptide-design task"},
    "work_spec": {
        "project": "nk2r_challenge",
        "protocol": "nk2r-challenge-v0.1",
        "scope": "one NK2R funnel run (60 candidates, calibrated)",
        "budget": "local seconds, no external jobs",
    },
}


def demo_nk2r(db: str, out_root: str | None = None) -> int:
    engine = get_engine(db, out_root)
    print("--- ingest NK2R letter ---")
    req = ingest_letter(engine, NK2R_LETTER)
    print(f"work item {req['work_item_id']} awaiting approval (nonce {req['nonce'][:12]}...)")
    print("--- operator approval (simulated terminal) ---")
    wi = engine.approve(req["work_item_id"], req["nonce"], "human:tao (simulated)")
    print(f"state={wi['state']} mandate={wi['mandate_id']}")
    print("--- execute NK2R task pack ---")
    outcome = engine.execute(req["work_item_id"])
    result = outcome["result"]
    print(f"outcome={result['outcome']} selected={result['receipt']['selected']}")
    print(f"acceptance={result['gate_report']['acceptance']}")
    print(f"candidates: {result['candidate_file']}")
    print("--- receipted reply ---")
    print(engine.reply(req["work_item_id"], "NK2R funnel completed"))
    print("DEMO-NK2R OK")
    return 0 if result["outcome"] == "completed" else 2


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)
    for name in ("init", "watch", "ingest", "approve", "execute", "demo-nk2r", "context", "follow-up"):
        p = sub.add_parser(name)
        p.add_argument("--db", default="/tmp/agentd.db")
        if name == "watch":
            p.add_argument("--inbox", type=Path, required=True)
            p.add_argument("--once", action="store_true")
        if name == "ingest":
            p.add_argument("letter", type=Path)
        if name in ("approve", "execute", "context", "follow-up"):
            p.add_argument("work_item_id")
        if name == "approve":
            p.add_argument("nonce")
        if name == "follow-up":
            p.add_argument("--text", required=True)
            p.add_argument("--actor", default="human:tao")
        if name in ("watch", "execute", "demo-nk2r"):
            p.add_argument("--out-root", default=None)
    args = parser.parse_args()

    if args.command == "init":
        engine = get_engine(args.db)
        from .taskpacks import TASK_PACKS
        print(json.dumps({"db": args.db, "task_packs": sorted(TASK_PACKS)}, indent=2))
        return 0
    if args.command == "watch":
        watch(args.inbox, args.db, once=args.once, out_root=args.out_root)
        return 0
    if args.command == "ingest":
        engine = get_engine(args.db)
        print(json.dumps(ingest_letter(engine, json.loads(args.letter.read_text())), indent=2))
        return 0
    if args.command == "approve":
        engine = get_engine(args.db)
        wi = engine.approve(args.work_item_id, args.nonce, "human:tao (operator-terminal)")
        print(json.dumps({"state": wi["state"], "mandate": wi["mandate_id"]}, indent=2))
        return 0
    if args.command == "execute":
        engine = get_engine(args.db, args.out_root)
        print(json.dumps(engine.execute(args.work_item_id), indent=2, default=str))
        return 0
    if args.command == "demo-nk2r":
        return demo_nk2r(args.db, args.out_root)
    if args.command == "context":
        engine = get_engine(args.db)
        print(json.dumps(engine.context_package(args.work_item_id), indent=2, default=str))
        return 0
    if args.command == "follow-up":
        engine = get_engine(args.db)
        print(json.dumps(engine.follow_up(args.work_item_id, args.actor, args.text), indent=2))
        return 0
    return 1


if __name__ == "__main__":
    sys.exit(main())
