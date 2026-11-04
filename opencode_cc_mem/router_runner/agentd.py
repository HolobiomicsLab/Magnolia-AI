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


def demo_jobloop(db: str, out_root: str | None = None) -> int:
    """The full cluster-job loop: park on a job, mid-wait user info, watcher
    notice wakes the owner, delta package carries BOTH pieces of new knowledge."""
    import tempfile
    engine = get_engine(db, out_root)
    print("--- ingest + approve ---")
    req = ingest_letter(engine, NK2R_LETTER)
    wi = req["work_item_id"]
    engine.approve(wi, req["nonce"], "human:tao (simulated)")

    print("--- worker submits a cluster job and PARKS (no process waits) ---")
    engine.park_on_job(wi, "run_demo_0001")
    print(f"state={engine.store.get('work_items', 'work_item_id', wi)['state']}")

    print("--- mid-wait user follow-up: context post, no execution ---")
    engine.follow_up(wi, "human:tao", "while you wait: use s20000 next round")

    print("--- poller detects terminal; job_watcher emits the notice ---")
    with tempfile.TemporaryDirectory() as tmp:
        inbox = Path(tmp) / "inbox" / "from-jobs"
        _write_demo_notice(inbox, wi)
        summary = engine.consume_job_notices(inbox.parent, include_context=False)
        print(f"woken={summary['woken']}")
        print("--- duplicate sweep is idempotent ---")
        again = engine.consume_job_notices(inbox.parent)
        print(f"second sweep woken={again['woken']}")

    print("--- wake-up context package: job notice AND user follow-up in delta ---")
    package = engine.context_package(wi)
    for post in package["delta"]["new_posts"]:
        print(f"  delta[{post['actor']}]: {post['text']}")
    print(f"state={package['delta']['current_state']}")

    print("--- next batch executes with all knowledge present ---")
    outcome = engine.execute(wi)
    print(f"outcome={outcome['result']['outcome']} candidates={outcome['result']['receipt']['selected']}")
    print("DEMO-JOBLOOP OK")
    return 0


def _write_demo_notice(inbox: Path, work_item_id: str) -> None:
    """Same shape as compchem_tools job_watcher.emit_job_notice output."""
    inbox.mkdir(parents=True, exist_ok=True)
    notice = {
        "kind": "job_notice",
        "version": 1,
        "run_id": "run_demo_0001",
        "tool": "haddock3",
        "state": "COMPLETED",
        "category": "success",
        "job_id": "11454253",
        "work_item_id": work_item_id,
        "event": {
            "source_id": "jobs:poller",
            "source_event_id": "run_demo_0001:COMPLETED",
            "actor": "system:job-watcher",
        },
    }
    (inbox / "run_demo_0001__completed.json").write_text(json.dumps(notice, indent=2))


def _load_job_watcher():
    """Load the PRODUCTION job_watcher from compchem-tools for the local demo."""
    import importlib.util
    path = (
        Path(__file__).resolve().parents[1]
        / "mcp-servers" / "compchem-tools" / "src" / "compchem_tools" / "tools" / "job_watcher.py"
    )
    spec = importlib.util.spec_from_file_location("job_watcher", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def demo_local(db: str, workdir: str | None = None, out_root: str | None = None) -> int:
    """One-command local end-to-end tour. No Slurm, no LLM, no network.

    Uses the PRODUCTION job_watcher.emit_job_notice for the terminal event,
    the real NK2R funnel for both design batches, and the real findings
    lifecycle (intermediate -> completed_campaign on completion).
    """
    import shutil
    import tempfile
    work = Path(workdir) if workdir else Path(tempfile.mkdtemp(prefix="agentd-local-demo-"))
    if workdir:
        shutil.rmtree(work, ignore_errors=True)
        work.mkdir(parents=True)
    engine = get_engine(db, out_root or (work / "runs"))
    project = work / "project"
    project.mkdir(parents=True, exist_ok=True)

    print("=== ALWAYS-ON AGENT SYSTEM — LOCAL DEMO (no Slurm, no LLM) ===")
    print(f"workdir: {work}\n")

    print("[1/8] A letter arrives; the deterministic watch stages it (no execution)")
    req = ingest_letter(engine, NK2R_LETTER)
    wi = req["work_item_id"]
    print(f"      work item {wi} state=candidate (awaiting YOUR approval)")

    print("[2/8] Operator approval (simulated terminal): Decision + Mandate")
    engine.approve(wi, req["nonce"], "human:tao (simulated)")

    print("[3/8] Worker submits batch 1 as a cluster job and PARKS (nobody waits)")
    engine.park_on_job(wi, "run_local_demo_1")
    print("      state=waiting_job")

    print("[4/8] You send new information MID-WAIT (context, not authority)")
    engine.follow_up(wi, "human:tao", "next round: prefer s20000 sampling")

    print("[5/8] The job finishes; the REAL job_watcher emits the notice")
    watcher = _load_job_watcher()
    emission = watcher.emit_job_notice(
        str(project),
        {
            "run_id": "run_local_demo_1",
            "tool": "haddock3",
            "lifecycle": "completed",
            "remote": {"local_run_dir": str(work / "job"), "remote_run_dir": None},
        },
        state="COMPLETED",
        category="success",
        job_id="local-demo",
    )
    print(f"      notice: {emission['path']}")

    print("[6/8] Wake-up: the delta carries BOTH the job outcome and your message")
    summary = engine.consume_job_notices(project / "inbox")
    assert summary["woken"] == [wi], summary
    package = engine.context_package(wi)
    for post in package["delta"]["new_posts"]:
        print(f"      delta[{post['actor']}]: {post['text']}")
    engine.record_finding(wi, "scientific", "batch 1: tachykinin-motif candidates dominate the top ranks")

    print("[7/8] Batch 1 executes (real funnel); the finding is promoted on completion")
    first = engine.execute(wi)
    print(f"      outcome={first['result']['outcome']} candidates={first['result']['receipt']['selected']}")
    for finding in engine.findings(wi):
        print(f"      finding[{finding['outcome_status']}]: {finding['text']}")

    print("[8/8] Your follow-up opens a LINKED Work Item for round 2 (fresh approval)")
    follow = engine.follow_up(wi, "human:tao", "round 2 with the new sampling")
    wi2 = follow["work_item_id"]
    engine.approve(wi2, follow["nonce"], "human:tao (simulated)")
    engine.record_finding(wi2, "operational", "s20000 is the right round-2 sampling (per user note)")
    second = engine.execute(wi2)
    print(f"      outcome={second['result']['outcome']} parent={follow['parent']}")
    digest = engine.context_package(wi2)["findings_digest"]
    for finding in digest["final"]:
        print(f"      finding[{finding['status']}]: {finding['text']}")

    print("\n=== ARTIFACTS ===")
    print(f"  control store : {db}")
    print(f"  batch 1       : {first['result']['candidate_file']}")
    print(f"  batch 2       : {second['result']['candidate_file']}")
    print(f"  notice        : {emission['path']}")
    print("DEMO-LOCAL OK")
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)
    for name in ("init", "watch", "ingest", "approve", "execute", "demo-nk2r", "context", "follow-up", "consume-jobs", "demo-jobloop", "demo-local"):
        p = sub.add_parser(name)
        p.add_argument("--db", default="/tmp/agentd.db")
        if name in ("watch", "consume-jobs"):
            p.add_argument("--inbox", type=Path, required=True)
        if name == "watch":
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
        if name == "demo-local":
            p.add_argument("--workdir", default=None)
        if name in ("watch", "execute", "demo-nk2r", "demo-jobloop", "demo-local"):
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
    if args.command == "consume-jobs":
        engine = get_engine(args.db)
        print(json.dumps(engine.consume_job_notices(args.inbox, include_context=True), indent=2, default=str))
        return 0
    if args.command == "demo-jobloop":
        return demo_jobloop(args.db, args.out_root)
    if args.command == "demo-local":
        return demo_local(args.db, args.workdir, args.out_root)
    return 1


if __name__ == "__main__":
    sys.exit(main())
