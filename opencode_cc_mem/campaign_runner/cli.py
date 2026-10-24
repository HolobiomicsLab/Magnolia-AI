"""Command-line interface for campaign state."""

from __future__ import annotations

import argparse
import json
import os
import signal
import sys
from pathlib import Path
from typing import Sequence

from .distill import drain_queue
from .jobs import CompchemJobBackend, FakeJobBackend, JobController
from .models import CampaignError, load_protocol, protocol_sha256
from .reviewers import Reviewer
from .runner import CampaignRunner, RunnerConfig
from .service import ensure_service, service_status, stop_service
from .state import CampaignState, CampaignStore
from .workers import OpenCodeExecutor


_ROOT = Path(__file__).resolve().parents[1]


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
    approve.add_argument("--minutes", type=float, default=0)

    approve_start = subparsers.add_parser(
        "approve-start", help="approve the initial campaign start"
    )
    approve_start.add_argument("run_dir")
    approve_start.add_argument("--approver", required=True)
    approve_start.add_argument("--reason", required=True)
    approve_start.add_argument("--minutes", type=float, default=0)

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

    distill = subparsers.add_parser(
        "distill-queue", help="distill completed campaign sessions into staging"
    )
    distill.add_argument("run_dir")
    distill.add_argument("--project-dir")
    distill.add_argument("--db")
    distill.add_argument("--limit", type=int, default=None)
    distill.add_argument("--preview", action="store_true")

    run = subparsers.add_parser("run", help="execute one campaign pass")
    _add_runtime_arguments(run)

    daemon = subparsers.add_parser("daemon", help="run the campaign loop")
    _add_runtime_arguments(daemon)
    daemon.add_argument("--interval", type=float, default=10.0)
    daemon.add_argument("--max-iterations", type=int, default=None)

    service = subparsers.add_parser("service", help="manage the project service")
    service.add_argument("action", choices=("start", "status", "stop"))
    service.add_argument("project_dir")
    service.add_argument("--opencode-bin")
    service.add_argument("--db")

    return parser


def _add_runtime_arguments(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("run_dir")
    parser.add_argument("--project-dir")
    parser.add_argument("--opencode-bin")
    parser.add_argument("--server")
    parser.add_argument("--db")
    parser.add_argument("--job-backend", choices=("compchem", "fake"), default="compchem")
    parser.add_argument("--distill-on-complete", action="store_true")


def _install_signal_handlers() -> None:
    def interrupt(signum, frame):
        raise KeyboardInterrupt

    signal.signal(signal.SIGTERM, interrupt)


def _print_stop_errors(errors: Sequence[dict]) -> None:
    for error in errors:
        print(f"stop error: {error}", file=sys.stderr)


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


def _validate_new_campaign(protocol_path: str, run_dir: str) -> None:
    resolved_protocol = Path(protocol_path).resolve()
    resolved_run = Path(run_dir).resolve()
    project_dir = _derive_project_dir(resolved_run)
    protocol = load_protocol(resolved_protocol)
    if protocol.project != project_dir.name:
        raise CampaignError(
            f"protocol project '{protocol.project}' does not match "
            f"project directory '{project_dir.name}'"
        )
    allowed_roots = [
        (_ROOT / "campaigns").resolve(),
        (project_dir / "campaigns").resolve(),
    ]
    if not any(
        resolved_protocol == root or resolved_protocol.is_relative_to(root)
        for root in allowed_roots
    ):
        raise CampaignError(
            "protocol must live under campaigns/ or the project's campaigns/ directory"
        )


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
    try:
        store.run_dir.relative_to((project_dir / "runs").resolve())
    except ValueError as exc:
        raise CampaignError(
            f"run directory must be inside {project_dir / 'runs'}"
        ) from exc
    if protocol.project != project_dir.name:
        raise CampaignError(
            f"protocol project '{protocol.project}' does not match "
            f"project directory '{project_dir.name}'"
        )
    if (
        getattr(args, "job_backend", "compchem") == "fake"
        and os.environ.get("MAGNOLIA_TESTING") != "1"
    ):
        raise CampaignError("fake job backend requires MAGNOLIA_TESTING=1")
    binary = getattr(args, "opencode_bin", None) or _default_binary()
    server = getattr(args, "server", None) or os.environ.get("MAGNOLIA_OPENCODE_SERVER")
    database = getattr(args, "db", None) or _default_db()
    environment = {
        "MAGNOLIA_PROJECT_DIR": str(project_dir),
        "MAGNOLIA_ROOT": str(project_dir.parents[1]),
    }
    uses_models = any(stage.type == "opencode" for stage in protocol.stages)
    if (
        uses_models
        and not server
        and getattr(args, "command", None) in {"run", "daemon"}
    ):
        try:
            record = ensure_service(project_dir, binary, database)
        except Exception as exc:
            raise CampaignError(f"dedicated opencode service unavailable: {exc}") from exc
        server = f"http://127.0.0.1:{record['port']}"
        environment["OPENCODE_SERVER_PASSWORD"] = str(record["password"])
        environment["OPENCODE_SERVER_USERNAME"] = "opencode"

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
            distill_on_complete=getattr(args, "distill_on_complete", False),
            distill_db=database,
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
        if args.command == "service":
            project_dir = Path(args.project_dir).resolve()
            binary = args.opencode_bin or _default_binary()
            database = args.db or _default_db()
            if args.action == "start":
                record = ensure_service(project_dir, binary, database)
                print(
                    json.dumps(
                        {key: value for key, value in record.items() if key != "password"},
                        indent=2,
                    )
                )
            elif args.action == "status":
                print(json.dumps(service_status(project_dir), indent=2))
            else:
                print(json.dumps({"stopped": stop_service(project_dir)}, indent=2))
            return 0
        if args.command == "init":
            _validate_new_campaign(args.protocol, args.run_dir)
            store = CampaignStore(args.run_dir)
            state = store.initialize(args.protocol, args.brief)
            _print_state(state)
            return 0
        if args.command == "distill-queue":
            run_dir = Path(args.run_dir).resolve()
            project_dir = (
                Path(args.project_dir).resolve()
                if args.project_dir
                else _derive_project_dir(run_dir)
            )
            print(
                json.dumps(
                    drain_queue(
                        run_dir,
                        project_dir,
                        args.db or _default_db(),
                        commit=not args.preview,
                        limit=args.limit,
                    ),
                    indent=2,
                    sort_keys=True,
                )
            )
            return 0
        store = CampaignStore(args.run_dir)
        if args.command == "status":
            _print_state(store.load())
            return 0
        if args.command == "approve":
            _print_state(
                store.approve(args.approver, args.reason, minutes=args.minutes)
            )
            return 0
        if args.command == "approve-start":
            _print_state(
                store.approve_start(
                    args.approver,
                    args.reason,
                    minutes=args.minutes,
                )
            )
            return 0
        if args.command == "pause":
            state = store.load()
            if state.state == "running":
                (store.run_dir / "PAUSE").write_text(args.reason + "\n")
                _print_state(state)
                return 0
            _print_state(store.pause(args.reason))
            return 0
        if args.command == "resume":
            (store.run_dir / "PAUSE").unlink(missing_ok=True)
            _print_state(store.resume(args.reason))
            return 0
        if args.command == "halt":
            try:
                runner = _build_runner(args)
                state, errors = runner.request_stop(
                    args.reason,
                    stop_processes=True,
                )
            except Exception as exc:
                errors = [{"scope": "halt", "error": str(exc)}]
                state = store.load()
            _print_state(state)
            if errors:
                _print_stop_errors(errors)
                return 2
            return 0
        if args.command == "reconcile":
            _print_state(store.reconcile())
            return 0
        if args.command == "run":
            runner = _build_runner(args)
            _install_signal_handlers()
            try:
                print(json.dumps(runner.run_once(), indent=2, sort_keys=True))
            except KeyboardInterrupt:
                _, errors = runner.request_stop(
                    "run interrupted",
                    stop_processes=True,
                )
                _print_stop_errors(errors)
                return 130
            return 0
        if args.command == "daemon":
            runner = _build_runner(args)
            _install_signal_handlers()
            try:
                runner.run_forever(args.interval, args.max_iterations)
            except KeyboardInterrupt:
                _, errors = runner.request_stop(
                    "daemon interrupted",
                    stop_processes=True,
                )
                _print_stop_errors(errors)
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
