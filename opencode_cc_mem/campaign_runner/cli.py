"""Command-line interface for campaign state."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import signal
import sys
import uuid
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Any, Mapping, Sequence

from .distill import drain_queue
from .executors.contracts import ExecContext
from .executors.capabilities import load_role_capabilities
from .executors.opencode import OpenCodeServiceExecutor
from .executors.registry import executor_for
from .jobs import CompchemJobBackend, FakeJobBackend, JobController
from .launch import resolve_python
from .governor import Governor
from .models import CampaignError, Protocol, ProtocolError, load_protocol, protocol_sha256
from .reviewers import ReviewError, Reviewer, choose_reviewer_model
from .routing import ResolvedRoute, RouteError, resolve_route
from .runner import CampaignRunner, RunnerConfig
from .service import ensure_service, merge_agent_steps, service_status, stop_service
from .state import CampaignState, CampaignStore
from .workers import InvocationGovernance, WorkerExecutor


_ROOT = Path(__file__).resolve().parents[1]


def _memory_setup(root: Path = _ROOT) -> dict[str, Any]:
    """Load the installation's memory provider selection for managed Vibe HTTP."""
    path = root / ".magnolia" / "llm-setup.json"
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    return value if isinstance(value, dict) else {}


def _memory_route(
    route: ResolvedRoute, python_executable: str | None = None
) -> dict[str, Any]:
    """Return authenticated managed Vibe or observed-shape stdio memory."""
    if route.client == "vibe":
        return {"transport": "managed"}
    return {
        "transport": "stdio",
        "command": python_executable or resolve_python(_ROOT),
        "args": ["-m", "compchem_memory.server"],
        "env_vars": [
            "MAGNOLIA_ROOT",
            "MAGNOLIA_PROJECT_DIR",
            "MAGNOLIA_RULES_DIR",
            "MAGNOLIA_TRANSCRIPT_TOOL_CHARS",
            "MAGNOLIA_DISTILL_INTERVAL_MIN",
            "MAGNOLIA_MEMORY_MODEL",
            "MAGNOLIA_MEMORY_PROVIDER",
        ],
    }


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


_WORKER_AGENT = "campaign-worker"
_REVIEWER_AGENT = "campaign-reviewer"


@dataclass(frozen=True)
class CampaignRoutes:
    """Resolved executable routes for every selectable campaign role."""

    worker: Mapping[str, ResolvedRoute]
    reviewer: Mapping[str, ResolvedRoute]


def resolve_campaign_routes(
    protocol: Protocol,
    *,
    require_reviewers: bool = False,
) -> CampaignRoutes:
    """Resolve stage and reviewer routes before any service or client launch."""
    workers: dict[str, ResolvedRoute] = {}
    reviewers: dict[str, ResolvedRoute] = {}
    worker_config = {
        "model": protocol.worker.model,
        "hard_stage_model": protocol.worker.hard_stage_model,
    }
    for stage in protocol.stages:
        if stage.type != "opencode":
            continue
        try:
            worker_route = resolve_route(
                "worker",
                {
                    "worker": worker_config,
                    "stage": {"model": stage.model, "hard": stage.hard},
                    "hard": stage.hard,
                },
            )
        except RouteError as exc:
            raise ProtocolError(f"stage {stage.id} worker route: {exc}") from exc
        workers[stage.id] = worker_route
        if not stage.review or not protocol.reviewer.enabled:
            continue
        if protocol.reviewer.model is None:
            if require_reviewers:
                raise ProtocolError(
                    f"stage {stage.id} reviewer route: no reviewer model configured"
                )
            continue
        try:
            selected = choose_reviewer_model(
                worker_route.model or "",
                protocol.reviewer.model,
                protocol.reviewer.alternate_model,
            )
            reviewers[stage.id] = resolve_route(
                "reviewer", {"reviewer": {"model": selected}}
            )
        except (ReviewError, RouteError) as exc:
            if require_reviewers:
                raise ProtocolError(f"stage {stage.id} reviewer route: {exc}") from exc
    return CampaignRoutes(workers, reviewers)


def opencode_service_roles(routes: CampaignRoutes) -> set[str]:
    """Return campaign roles that can actually select the OpenCode service."""
    roles: set[str] = set()
    if any(route.client == "opencode" for route in routes.worker.values()):
        roles.add("worker")
    if any(route.client == "opencode" for route in routes.reviewer.values()):
        roles.add("reviewer")
    return roles


def _service_settings(
    protocol: Protocol,
    governor_cap: int | None = None,
    roles: set[str] | None = None,
) -> tuple[str, list[str]]:
    """OPENCODE_CONFIG_CONTENT and required agents for the dedicated service.

    Observed on OpenCode 2.3.0: the step limit in OPENCODE_CONFIG_CONTENT
    reaches a dedicated service only from the service's own environment at
    start, so the limits of both agents are given to the service."""
    selected_roles = roles if roles is not None else {"worker", "reviewer"}
    clamp = lambda value: min(value, governor_cap) if governor_cap else value
    steps = {}
    if "worker" in selected_roles:
        steps[_WORKER_AGENT] = clamp(protocol.worker.max_steps)
    if protocol.reviewer.enabled and "reviewer" in selected_roles:
        steps[_REVIEWER_AGENT] = clamp(protocol.reviewer.max_steps)
    config = merge_agent_steps(os.environ.get("OPENCODE_CONFIG_CONTENT"), steps)
    return config, list(steps)


def _build_runner(args: argparse.Namespace) -> CampaignRunner:
    store = CampaignStore(args.run_dir)
    protocol = store.verify_protocol()
    project_dir = (
        Path(os.path.realpath(args.project_dir))
        if getattr(args, "project_dir", None)
        else Path(os.path.realpath(_derive_project_dir(store.run_dir)))
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
        **os.environ,
        "MAGNOLIA_PROJECT_DIR": str(project_dir),
        "MAGNOLIA_ROOT": str(project_dir.parents[1]),
    }
    memory_setup = _memory_setup()
    routes = resolve_campaign_routes(protocol, require_reviewers=True)
    service_roles = opencode_service_roles(routes)
    governor = Governor()
    governor_cap = governor.policy.max_turns_per_run
    role_capabilities = load_role_capabilities()
    project_digest = hashlib.sha256(str(project_dir).encode("utf-8")).hexdigest()
    parent_run_id = f"campaign:{protocol.id}:{store.run_dir.name}"

    def governance(route: ResolvedRoute, role_cap: int) -> InvocationGovernance:
        return InvocationGovernance(
            governor,
            route,
            parent_run_id,
            project_digest,
            role_cap=role_cap,
        )

    def execution_context(
        route: ResolvedRoute,
        role: str,
        timeout: int,
        role_cap: int,
        agent: str,
    ) -> ExecContext:
        python_executable = resolve_python(_ROOT, environment)
        client_binary = {
            "claude-code": os.environ.get("MAGNOLIA_CLAUDE_BIN", "claude"),
            "codex": os.environ.get("MAGNOLIA_CODEX_BIN", "codex"),
            "vibe": os.environ.get("MAGNOLIA_VIBE_BIN", "vibe"),
        }.get(route.client, binary)
        return ExecContext(
            route=route,
            role=role,
            project_dir=project_dir,
            run_dir=store.run_dir,
            control_dir=store.control_dir,
            work_dir=store.run_dir / "work",
            timeout_seconds=timeout,
            role_cap=role_cap,
            governor_cap=governor_cap,
            opencode_binary=binary,
            opencode_server=server,
            opencode_db=database,
            agent=agent,
            role_capabilities=role_capabilities,
            installation_root=_ROOT,
            workspace_root=project_dir.parent,
            project_identity=project_digest,
            python_executable=python_executable,
            tools_port=int(os.environ.get("MAGNOLIA_TOOLS_PORT", "8766")),
            setup=memory_setup,
            memory_service=_memory_route(
                route, python_executable
            ),
            invocation_id=uuid.uuid4().hex,
            launch_argv=(client_binary,),
            environment=environment,
            governance=governance(route, role_cap),
        )

    if (
        service_roles
        and not server
        and getattr(args, "command", None) in {"run", "daemon"}
    ):
        config_content, required_agents = _service_settings(
            protocol, governor_cap, service_roles
        )
        try:
            service_route = next(
                route
                for route in (*routes.worker.values(), *routes.reviewer.values())
                if route.client == "opencode"
            )
            service_executor = executor_for(
                service_route,
                "service",
                ExecContext(
                    route=service_route,
                    role="service",
                    project_dir=project_dir,
                    opencode_binary=binary,
                    opencode_db=database,
                    governor_cap=governor_cap,
                ),
            )
            assert isinstance(service_executor, OpenCodeServiceExecutor)
            record = service_executor.ensure(
                config_content=config_content,
                required_agents=required_agents,
            )
        except Exception as exc:
            raise CampaignError(f"dedicated opencode service unavailable: {exc}") from exc
        server = f"http://127.0.0.1:{record['port']}"
        environment["OPENCODE_SERVER_PASSWORD"] = str(record["password"])
        environment["OPENCODE_SERVER_USERNAME"] = "opencode"

    def worker_factory(
        model: str,
        timeout: int,
        resume_from: Path | None = None,
        resume_artifact: Any = None,
    ) -> WorkerExecutor:
        route = resolve_route("worker", {"worker": {"model": model}})
        executor = executor_for(
            route,
            "worker",
            replace(
                execution_context(
                    route,
                    "worker",
                    timeout,
                    protocol.worker.max_steps,
                    _WORKER_AGENT,
                ),
                resume_from=resume_from,
                resume_artifact=resume_artifact,
            ),
        )
        return executor

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
            context_factory=lambda route, cwd, attempt_dir: execution_context(
                route,
                "reviewer",
                protocol.reviewer.timeout_seconds,
                protocol.reviewer.max_steps,
                _REVIEWER_AGENT,
            ),
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
            quota_governor=governor,
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
            state = store.load()
            if state.state == "waiting_quota":
                _print_state(_build_runner(args).resume_quota(manual=True))
            else:
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
