"""CLI for non-MCP memory operations (HPC-compatible, no server required)."""

import argparse
import json
import sys
from datetime import datetime, timezone
from pathlib import Path

from compchem_memory.storage import ensure_project_store, scaffold_obsidian_vault


def cmd_assess(args: argparse.Namespace) -> int:
    """Auto-assess a scientific tool run and record results."""
    from compchem_memory.learning.assessor import assess_run
    from compchem_memory.tiers.project import ProjectManager

    command = args.command
    exit_code = args.exit_code
    project_dir = args.project_dir or "."

    # 1. Detect tool name from command string
    tool = _detect_tool(command)
    if not tool:
        return 0  # Not a known scientific tool — skip silently

    local_dir = ensure_project_store(project_dir)
    sessions_dir = local_dir / "sessions"

    # 2. Detect run directory
    run_dir = _detect_run_dir(command, tool)

    # 3. Run assessment
    if run_dir:
        try:
            assessment = assess_run(run_dir, tool, exit_code)
        except Exception as e:
            print(f"Assessment error: {e}", file=sys.stderr)
            return 0
    else:
        # No run directory found — record basic info to session only
        assessment = {
            "tool": tool,
            "exit_code": exit_code,
            "overall": "pass" if exit_code == 0 else "fail",
            "metrics": {},
            "quality_flags": [],
            "note": "no run directory detected",
        }

    # 4. Record run YAML
    #    Pass the project root (not .magnolia/) since ProjectManager._project_store
    #    adds .magnolia/ itself.
    try:
        proj_m = ProjectManager(Path.home() / ".magnolia")
        ts = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")
        run_id = f"{tool}_{ts}"
        proj_m.record_run(
            str(Path(project_dir).resolve()),
            run_id=run_id,
            tool=tool,
            status=assessment.get("overall", "pass" if exit_code == 0 else "failed"),
            metrics=assessment.get("metrics", {}),
            quality_flags=assessment.get("quality_flags", []),
        )
    except Exception as e:
        print(f"Run record error: {e}", file=sys.stderr)

    # 5. Log assessment event to session JSONL
    ts = datetime.now(timezone.utc).isoformat()
    fname = datetime.now(timezone.utc).strftime("%Y-%m-%d.jsonl")
    path = sessions_dir / fname
    entry = {
        "timestamp": ts,
        "event_type": "run_assessment",
        "tool": tool,
        "run_dir": str(run_dir) if run_dir else "",
        "overall": assessment.get("overall", "unknown"),
        "metrics": assessment.get("metrics", {}),
        "quality_flags": assessment.get("quality_flags", []),
    }
    with open(path, "a") as f:
        f.write(json.dumps(entry) + "\n")

    overall = assessment.get("overall", "unknown")
    flags = assessment.get("quality_flags", [])
    flag_str = f" flags: {flags}" if flags else ""
    print(f"Assessed {tool}: {overall}{flag_str}")
    return 0


# Tool detection: map command first-word to known tool names
_TOOL_ALIASES = {
    "haddock3": "haddock3",
    "run_haddock3": "haddock3",
    "boltzgen": "boltzgen",
    "gnina": "gnina",
    "gmx": "gromacs",
    "xtb": "xtb",
}


def _detect_tool(command: str) -> str:
    """Extract tool name from command string. Returns '' if unknown."""
    first_word = command.strip().split()[0] if command.strip() else ""
    # Handle paths like /path/to/haddock3
    basename = Path(first_word).name
    for pattern, tool_name in _TOOL_ALIASES.items():
        if pattern in basename.lower():
            return tool_name
    return ""


def _detect_run_dir(command: str, tool: str) -> str | None:
    """Heuristic: find the run output directory from command or cwd."""
    from compchem_memory.learning.assessor import _expected_outputs

    # Check if command contains a runs/ path argument
    for part in command.split():
        if "runs/" in part:
            candidate = Path(part)
            if candidate.exists():
                return str(candidate)

    # Check cwd for recently created output matching tool patterns
    cwd = Path.cwd()
    expected = _expected_outputs(tool)
    for d in sorted(cwd.glob("*"), key=lambda p: p.stat().st_mtime, reverse=True)[:5]:
        if not d.is_dir():
            continue
        for pattern in expected:
            if list(d.glob(pattern)) or list(d.glob(f"**/{pattern}")):
                return str(d)

    return None


def cmd_log_bash(args: argparse.Namespace) -> int:
    """Log a bash command execution to the project's session JSONL."""
    project_dir = args.project_dir or "."
    local_dir = ensure_project_store(project_dir)
    sessions_dir = local_dir / "sessions"

    ts = datetime.now(timezone.utc).isoformat()
    fname = datetime.now(timezone.utc).strftime("%Y-%m-%d.jsonl")
    path = sessions_dir / fname

    entry = {
        "timestamp": ts,
        "event_type": "bash_execution",
        "command": args.command,
        "exit_code": args.exit_code,
        "working_dir": args.working_dir or str(Path.cwd()),
        "tags": args.tags or [],
    }
    if args.result_summary:
        entry["result_summary"] = args.result_summary
    if args.error:
        entry["error"] = args.error

    with open(path, "a") as f:
        f.write(json.dumps(entry) + "\n")

    print(f"Logged bash execution to {path}")
    return 0


def cmd_log_job(args: argparse.Namespace) -> int:
    project_dir = args.project_dir or "."
    local_dir = ensure_project_store(project_dir)
    sessions_dir = local_dir / "sessions"

    ts = datetime.now(timezone.utc).isoformat()
    fname = datetime.now(timezone.utc).strftime("%Y-%m-%d.jsonl")
    path = sessions_dir / fname

    entry = {
        "timestamp": ts,
        "event_type": "job_submission",
        "command": args.command,
        "working_dir": args.working_dir or str(Path.cwd()),
        "scheduler": args.scheduler,
        "job_id": args.job_id or "",
        "ncores": args.ncores,
        "memory": args.memory,
        "time_limit": args.time_limit,
        "partition": args.partition or "",
        "job_name": args.job_name or "",
        "tags": args.tags or [],
    }

    if args.result_summary:
        entry["result_summary"] = args.result_summary

    with open(path, "a") as f:
        f.write(json.dumps(entry) + "\n")

    print(f"Logged job submission to {path}")
    return 0


def cmd_log_event(args: argparse.Namespace) -> int:
    """Log an arbitrary event (for HPC jobs, etc.)."""
    project_dir = args.project_dir or "."
    local_dir = ensure_project_store(project_dir)
    queue_dir = local_dir / "queue"

    ts = datetime.now(timezone.utc).isoformat()
    fname = datetime.now(timezone.utc).strftime("%Y-%m-%d_%H%M%S_%f.jsonl")
    path = queue_dir / fname

    entry = {
        "timestamp": ts,
        "event_type": args.event_type,
        "data": json.loads(args.data or "{}"),
    }

    with open(path, "w") as f:
        f.write(json.dumps(entry) + "\n")

    print(f"Queued event to {path}")
    return 0


def cmd_sync_queue(args: argparse.Namespace) -> int:
    """Ingest queued events from HPC jobs into the local session log."""
    project_dir = args.project_dir or "."
    local_dir = ensure_project_store(project_dir)
    queue_dir = local_dir / "queue"
    sessions_dir = local_dir / "sessions"

    if not queue_dir.exists():
        print("No queue directory found.")
        return 0

    fname = datetime.now(timezone.utc).strftime("%Y-%m-%d.jsonl")
    session_path = sessions_dir / fname

    ingested = 0
    for queued_file in sorted(queue_dir.glob("*.jsonl")):
        with open(queued_file) as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                with open(session_path, "a") as out:
                    out.write(line + "\n")
                ingested += 1
        if args.delete_after:
            queued_file.unlink()

    print(f"Ingested {ingested} events into {session_path}")
    return 0


def cmd_init_vault(args: argparse.Namespace) -> int:
    """Scaffold Obsidian vault configuration for the project."""
    project_dir = args.project_dir or "."
    obsidian_dir = scaffold_obsidian_vault(project_dir)
    print(f"Obsidian vault scaffolded at {obsidian_dir}")
    print("Open this project directory in Obsidian to use it as a vault.")
    return 0


def cmd_generate_daily_note(args: argparse.Namespace) -> int:
    """Generate a daily note pre-populated with that day's memory data."""
    from compchem_memory.notebook import generate_notebook

    project_dir = args.project_dir or "."
    local_dir = ensure_project_store(project_dir)

    target_date = args.date or datetime.now(timezone.utc).strftime("%Y-%m-%d")

    md_content = generate_notebook(
        project_dir=str(Path(project_dir).resolve()),
        start_date=target_date,
        end_date=target_date,
    )

    daily_dir = local_dir / "daily-notes"
    daily_dir.mkdir(parents=True, exist_ok=True)
    note_path = daily_dir / f"{target_date}.md"

    if note_path.exists() and not args.overwrite:
        print(f"Daily note already exists at {note_path}. Use --overwrite to replace.")
        return 1

    note_path.write_text(md_content)
    print(f"Daily note generated at {note_path}")
    return 0


def cmd_compact_session(args: argparse.Namespace) -> int:
    """Compact old session logs into summary notes to prevent unbounded growth."""
    from compchem_memory.compaction import maybe_compact_session

    project_dir = args.project_dir or "."
    local_dir = ensure_project_store(project_dir)
    sessions_dir = local_dir / "sessions"

    compacted = 0
    for session_file in sorted(sessions_dir.glob("*.jsonl")):
        result = maybe_compact_session(session_file)
        if result:
            print(f"Compacted {session_file.name}")
            compacted += 1

    if compacted:
        print(f"Compacted {compacted} session file(s).")
    else:
        print("No compaction needed.")
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(prog="compchem-memory")
    subparsers = parser.add_subparsers(dest="command", required=True)

    # log-bash
    p_bash = subparsers.add_parser("log-bash", help="Log a bash command")
    p_bash.add_argument("--project-dir", default=".")
    p_bash.add_argument("--command", required=True)
    p_bash.add_argument("--exit", type=int, dest="exit_code", default=0)
    p_bash.add_argument("--working-dir", default=str(Path.cwd()))
    p_bash.add_argument("--result-summary", default="")
    p_bash.add_argument("--error", default="")
    p_bash.add_argument("--tags", nargs="*", default=[])
    p_bash.set_defaults(func=cmd_log_bash)

    # assess
    p_assess = subparsers.add_parser(
        "assess", help="Auto-assess a scientific tool run"
    )
    p_assess.add_argument("--project-dir", default=".")
    p_assess.add_argument("--command", required=True, help="Full command that was run")
    p_assess.add_argument(
        "--exit-code", type=int, default=0, help="Exit code of the command"
    )
    p_assess.set_defaults(func=cmd_assess)

    # log-job
    p_job = subparsers.add_parser(
        "log-job", help="Log a job submission with resource details"
    )
    p_job.add_argument("--project-dir", default=".")
    p_job.add_argument("--command", required=True, help="Full command submitted")
    p_job.add_argument("--working-dir", default=str(Path.cwd()))
    p_job.add_argument("--scheduler", default="local", help="slurm, pbs, or local")
    p_job.add_argument("--job-id", default="", help="Job ID returned by scheduler")
    p_job.add_argument("--job-name", default="", help="Human-readable job name")
    p_job.add_argument("--ncores", type=int, default=0, help="Number of CPU cores")
    p_job.add_argument("--memory", default="", help="Memory allocation (e.g. 32GB)")
    p_job.add_argument(
        "--time-limit", default="", help="Wall time limit (e.g. 24:00:00)"
    )
    p_job.add_argument("--partition", default="", help="Scheduler partition/queue")
    p_job.add_argument("--result-summary", default="")
    p_job.add_argument("--tags", nargs="*", default=[])
    p_job.set_defaults(func=cmd_log_job)

    # log-event
    p_event = subparsers.add_parser("log-event", help="Log an arbitrary event")
    p_event.add_argument("--project-dir", default=".")
    p_event.add_argument("--event-type", required=True)
    p_event.add_argument("--data", default="{}")
    p_event.set_defaults(func=cmd_log_event)

    # sync-queue
    p_sync = subparsers.add_parser("sync-queue", help="Ingest queued HPC events")
    p_sync.add_argument("--project-dir", default=".")
    p_sync.add_argument("--delete-after", action="store_true")
    p_sync.set_defaults(func=cmd_sync_queue)

    # init-vault
    p_vault = subparsers.add_parser("init-vault", help="Scaffold Obsidian vault config")
    p_vault.add_argument("--project-dir", default=".")
    p_vault.set_defaults(func=cmd_init_vault)

    # compact-session
    p_compact = subparsers.add_parser(
        "compact-session", help="Compact old session logs into summary notes"
    )
    p_compact.add_argument("--project-dir", default=".")
    p_compact.set_defaults(func=cmd_compact_session)

    # generate-daily-note
    p_daily = subparsers.add_parser(
        "generate-daily-note", help="Generate daily lab note"
    )
    p_daily.add_argument("--project-dir", default=".")
    p_daily.add_argument("--date", default="", help="Date YYYY-MM-DD (default: today)")
    p_daily.add_argument("--overwrite", action="store_true")
    p_daily.set_defaults(func=cmd_generate_daily_note)

    args = parser.parse_args()
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
