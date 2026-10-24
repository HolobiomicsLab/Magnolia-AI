"""Command-line interface for campaign state."""

from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path
from typing import Sequence

from .jobs import CompchemJobBackend, FakeJobBackend, JobController
from .models import CampaignError, load_protocol, protocol_sha256
from .reviewers import Reviewer
from .runner import CampaignRunner, RunnerConfig
from .state import CampaignState, CampaignStore
from .workers import OpenCodeExecutor


def _print_state(state: CampaignState) -> None:
    print(json.dumps(state.to_dict(), indent=2, sort_keys=True))


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="magnolia-campaign")
    subparsers = parser.add_subparsers(dest="command", required=True)

    validate = subparsers.add_parser("validate", help="validate a protocol file")
    validate.add_argument("protocol")

    init = subparsers.add_parser("init", help="initialize a campaign")
    init.add_argument("protocol")
    init.add_argument("--run-dir", required=True)
    init.add_argument("--brief", required=True)

    status = subparsers.add_parser("status", help="show campaign state")
    status.add_argument("run_dir")

    approve = subparsers.add_parser("approve", help="approve a waiting campaign")
    approve.add_argument("run_dir")
    approve.add_argument("--approver", required=True)
    approve.add_argument("--reason", required=True)

    pause = subparsers.add_parser("pause", help="pause a campaign")
    pause.add_argument("run_dir")
    pause.add_argument("--reason", default="operator pause")

    resume = subparsers.add_parser("resume", help="resume a paused campaign")
    resume.add_argument("run_dir")
    resume.add_argument("--reason", default="operator resume")

    halt = subparsers.add_parser("halt", help="halt a campaign and cancel its jobs")
    halt.add_argument("run_dir")
    halt.add_argument("--reason", default="operator halt")

    reconcile = subparsers.add_parser("reconcile", help="repair state from journal")
    reconcile.add_argument("run_dir")

    run = subparsers.add_parser("run", help="execute one campaign pass")
    _add_runtime_arguments(run)

    daemon = subparsers.add_parser("daemon", help="run the campaign loop")
    _add_runtime_arguments(daemon)
    daemon.add_argument("--interval", type=float, default=10.0)
    daemon.add_argument("--max-iterations", type=int, default=None)

    return parser


def _add_runtime_arguments(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("run_dir")
    parser.add_argument("--project-dir")
    parser.add_argument("--opencode-bin")
    parser.add_argument("--server")
    parser.add_argument("--db")
    parser.add_argument("--job-backend", choices=("compchem", "fake"), default="compchem")


def _default_binary() -> str:
    return os.environ.get(
        "MAGNOLIA_OPENCODE_V2_BIN",
        str(Path.home() / ".local/share/magnolia-v2/bin/opencode"),
    )


def _default_db() -> str:
    return os.environ.get(
        "OPENCODE_DB",
        str(Path.home() / ".local/share/opencode/opencode-v2.db"),
    )


def _derive_project_dir(run_dir: Path) -> Path:
    if run_dir.parent.name != "runs":
        raise CampaignError(
            f"cannot derive project directory from {run_dir}; pass --project-dir"
        )
    return run_dir.parent.parent


def _build_runner(args: argparse.Namespace) -> CampaignRunner:
    store = CampaignStore(args.run_dir)
    protocol = store.verify_protocol()
    project_dir = (
        Path(args.project_dir).resolve()
        if getattr(args, "project_dir", None)
        else _derive_project_dir(store.run_dir)
    )
    if not project_dir.is_dir():
        raise CampaignError(f"project directory does not exist: {project_dir}")
    binary = getattr(args, "opencode_bin", None) or _default_binary()
    server = getattr(args, "server", None) or os.environ.get("MAGNOLIA_OPENCODE_SERVER")
    database = getattr(args, "db", None) or _default_db()
    environment = {
        "MAGNOLIA_PROJECT_DIR": str(project_dir),
        "MAGNOLIA_ROOT": str(project_dir.parents[1]),
    }

    def worker_factory(model: str, timeout: int) -> OpenCodeExecutor:
        return OpenCodeExecutor(
            binary=binary,
            model=model,
            agent="campaign-worker",
            server_url=server,
            db_path=database,
            environment=environment,
            timeout_seconds=timeout,
            max_steps=protocol.worker.max_steps,
        )

    reviewer = None
    if protocol.reviewer.enabled:
        reviewer = Reviewer(
            binary=binary,
            server_url=server,
            db_path=database,
            environment=environment,
            timeout_seconds=protocol.reviewer.timeout_seconds,
            max_retries=protocol.reviewer.repair_attempts,
            max_steps=protocol.reviewer.max_steps,
        )
    backend = (
        FakeJobBackend()
        if getattr(args, "job_backend", "compchem") == "fake"
        else CompchemJobBackend()
    )
    jobs = JobController(
        store,
        project_dir,
        backend,
        max_parallel_jobs=protocol.budget.max_parallel_jobs,
    )
    return CampaignRunner(
        store,
        project_dir,
        RunnerConfig(
            worker_factory=worker_factory,
            jobs=jobs,
            reviewer=reviewer,
            usage_db=database,
        ),
    )


def main(argv: Sequence[str] | None = None) -> int:
    parser = _build_parser()
    args = parser.parse_args(argv)
    try:
        if args.command == "validate":
            protocol = load_protocol(args.protocol)
            print(
                json.dumps(
                    {
                        "id": protocol.id,
                        "project": protocol.project,
                        "sha256": protocol_sha256(protocol),
                        "stages": [stage.id for stage in protocol.stages],
                    },
                    indent=2,
                    sort_keys=True,
                )
            )
            return 0
        if args.command == "init":
            store = CampaignStore(args.run_dir)
            state = store.initialize(args.protocol, args.brief)
            _print_state(state)
            return 0
        store = CampaignStore(args.run_dir)
        if args.command == "status":
            _print_state(store.load())
            return 0
        if args.command == "approve":
            _print_state(store.approve(args.approver, args.reason))
            return 0
        if args.command == "pause":
            _print_state(store.pause(args.reason))
            return 0
        if args.command == "resume":
            _print_state(store.resume(args.reason))
            return 0
        if args.command == "halt":
            try:
                runner = _build_runner(args)
                runner.cancel_jobs(args.reason)
            except Exception as exc:
                print(f"warning: job cancellation failed: {exc}", file=sys.stderr)
            _print_state(store.halt(args.reason))
            return 0
        if args.command == "reconcile":
            _print_state(store.reconcile())
            return 0
        if args.command == "run":
            runner = _build_runner(args)
            print(json.dumps(runner.run_once(), indent=2, sort_keys=True))
            return 0
        if args.command == "daemon":
            runner = _build_runner(args)
            try:
                runner.run_forever(args.interval, args.max_iterations)
            except KeyboardInterrupt:
                print("daemon interrupted", file=sys.stderr)
                return 130
            _print_state(store.load())
            return 0
    except CampaignError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    parser.error(f"unknown command {args.command}")
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
