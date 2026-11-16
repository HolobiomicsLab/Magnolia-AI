"""Render a client launch and emit its NUL-framed command protocol."""

from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import sys
from collections.abc import Sequence
from datetime import datetime, timezone
from pathlib import Path

from compchem_memory.harness.files import atomic_write, validate_project_paths
from compchem_memory.harness.neutral import NeutralConfig, bind_memory_service, load
from compchem_memory.harness.protocol import RENDERERS, Launch, LaunchContext, Renderer


def main(argv: Sequence[str] | None = None) -> int:
    """Render one launch, returning the protocol's process exit code."""
    try:
        args = _parse(argv)
        if args.command == "route":
            return _print_route(args)
        if args.command == "validate-route":
            return _validate_route(args)
        if args.command == "usage":
            return _usage(args)
        launch, renderer = _render(args)
        _validate(launch)
        project_dir = args.root.resolve() / "projects" / args.project
        validate_project_paths(launch.files, project_dir)
        for path, content in launch.files.items():
            atomic_write(path, content)
        _link(launch.links, project_dir)
        for conflict in launch.conflicts:
            print(f"warning: {str(conflict).splitlines()[0]}", file=sys.stderr)
        _emit(launch)
        return 0
    except MissingBinary as error:
        return _error(2, str(error))
    except SystemExit as error:
        if error.code == 0:
            raise
        return _error(1, "invalid command line")
    except Exception as error:  # noqa: BLE001 - CLI failures use one stderr line
        return _error(1, str(error))


class MissingBinary(Exception):
    """A renderer's client executable is absent from PATH."""


def _parse(argv: Sequence[str] | None) -> argparse.Namespace:
    parser = _ArgumentParser(prog="python -m compchem_memory.harness")
    subcommands = parser.add_subparsers(dest="command", required=True)
    launch = subcommands.add_parser("launch")
    launch.add_argument("--harness", required=True, choices=("claude", "codex", "vibe"))
    launch.add_argument("--root", required=True, type=Path)
    launch.add_argument("--project", required=True)
    launch.add_argument("--python", required=True)
    launch.add_argument("--tools-port", required=True, type=int)
    launch.add_argument("--holder-pid", required=True, type=int)
    launch.add_argument("--model")
    launch.add_argument("--route-resolved", action="store_true")
    route = subcommands.add_parser("route")
    route.add_argument("--root", required=True, type=Path)
    route.add_argument("--harness", choices=("opencode", "claude", "codex", "vibe"))
    route.add_argument("--model")
    validate_route = subcommands.add_parser("validate-route")
    validate_route.add_argument("--root", required=True, type=Path)
    validate_route.add_argument(
        "--role", required=True,
        choices=("interactive", "worker", "reviewer", "helper", "evaluator"),
    )
    validate_route.add_argument("--model", required=True)
    usage = subcommands.add_parser("usage")
    usage.add_argument("--root", required=True, type=Path)
    usage.add_argument("action", nargs="?", choices=("resume",))
    usage.add_argument("subscription_key", nargs="?")
    return parser.parse_args(argv)


class _ArgumentParser(argparse.ArgumentParser):
    def error(self, message: str) -> None:
        raise ValueError(message)


def _render(args: argparse.Namespace) -> tuple[Launch, Renderer]:
    renderer = RENDERERS.get(args.harness)
    if renderer is None:
        raise ValueError(f"no renderer registered for harness: {args.harness}")
    if shutil.which(renderer.binary) is None:
        raise MissingBinary(f"missing client binary: {renderer.binary}")
    root = args.root.resolve()
    setup = _setup(root)
    model = args.model
    if not args.route_resolved:
        route = _resolve_interactive(root, setup, args.harness, args.model)
        model = route.model
    project_dir = root / "projects" / args.project
    context = LaunchContext(
        root=root,
        project=args.project,
        project_dir=project_dir,
        python=args.python,
        tools_port=args.tools_port,
        model=model,
        setup=setup,
        holder_pid=args.holder_pid,
    )
    neutral = load(root / "opencode.json.template", _substitutions(context), setup)
    if os.environ.get("MAGNOLIA_DRY_RUN") != "1" and args.harness == "vibe" and any(
        server.name == "compchem-memory" and server.enabled
        for server in neutral.servers
    ):
        from compchem_memory.harness.memory_service import (
            TOKEN_ENV,
            encode_handle,
            ensure_memory_service,
            release_memory_service,
            token_for_handle,
        )

        handle = ensure_memory_service(context)
        context.memory_service = handle.route()
        neutral = bind_memory_service(neutral, handle.route())
        try:
            rendered = renderer(neutral, context)
            token = token_for_handle(handle)
        except Exception:
            release_memory_service(handle)
            raise
        rendered.env[TOKEN_ENV] = token
        rendered.env["MAGNOLIA_MEMORY_SERVICE_HANDLE"] = encode_handle(handle)
        return rendered, renderer
    return renderer(neutral, context), renderer


def _campaign_modules(root: Path):
    """Import installation-local campaign contracts from any caller cwd."""
    installation = str(root.resolve())
    if installation not in sys.path:
        sys.path.insert(0, installation)
    from campaign_runner.governor import Governor
    from campaign_runner.routing import resolve_route

    return Governor, resolve_route


def _resolve_interactive(root: Path, setup: dict, harness: str | None, model: str | None):
    _, resolve_route = _campaign_modules(root)
    inputs = dict(setup)
    if harness:
        inputs["harness_flag"] = harness
    if model:
        inputs["model_flag"] = model
    return resolve_route("interactive", inputs)


def _print_route(args: argparse.Namespace) -> int:
    root = args.root.resolve()
    route = _resolve_interactive(root, _setup(root), args.harness, args.model)
    harness = "claude" if route.client == "claude-code" else route.client
    print(json.dumps({
        "harness": harness,
        "client": route.client,
        "model": route.model,
        "source": route.source,
        "native_default": route.native_default,
    }, separators=(",", ":")))
    return 0


def _validate_route(args: argparse.Namespace) -> int:
    _, resolve_route = _campaign_modules(args.root.resolve())
    resolve_route(args.role, {"routes": {args.role: args.model}})
    return 0


_SUBSCRIPTION_KEY = re.compile(
    r"(?:claude-code|codex|vibe|opencode:[A-Za-z0-9._-]+)\Z"
)


def _display_subscription_key(value: object) -> str:
    """Render only the documented budget identity forms."""
    return value if isinstance(value, str) and _SUBSCRIPTION_KEY.fullmatch(value) else "unknown"


def _usage(args: argparse.Namespace) -> int:
    root = args.root.resolve()
    Governor, resolve_route = _campaign_modules(root)
    governor = Governor()
    if args.action == "resume":
        key = args.subscription_key
        if not isinstance(key, str) or _SUBSCRIPTION_KEY.fullmatch(key) is None:
            raise ValueError("invalid subscription key")
        if not governor.resume_key(key):
            raise ValueError("no releasable observed quota hold for that subscription")
        print("released observed quota hold; policy budgets and pending work are unchanged")
        return 0
    if args.subscription_key is not None:
        raise ValueError("usage accepts only the optional resume action")

    setup = _setup(root)
    print("Routes:")
    for role in ("interactive", "worker", "reviewer", "helper", "evaluator"):
        try:
            route = resolve_route(role, setup)
            model = route.model if route.model is not None else "native default"
            print(f"  {role}: {route.client}/{model} [{route.source}]")
        except Exception as exc:  # a missing route is reportable, not fatal
            print(f"  {role}: unavailable [{str(exc).splitlines()[0]}]")

    report = governor.report(datetime.now(timezone.utc).date())
    print("Usage (UTC):")
    _print_period("day", report["day"])
    _print_period("week", report["week"])
    print("Live waits:")
    if not report["waits"] and not report["holds"]:
        print("  none")
    for wait in report["waits"]:
        reset = wait.get("reset_at") or "manual resume required"
        key = _display_subscription_key(wait.get("key"))
        print(
            f"  {wait['caller_id']}: {key} reason={wait['reason']} "
            f"reset={reset}"
        )
        if str(wait.get("caller_id", "")).startswith("evaluator:"):
            print(
                "    pending evaluation: python -m router_runner.agentd "
                "resume-eval <id> --db <path>"
            )
    waiting_keys = {wait["key"] for wait in report["waits"]}
    for hold in report["holds"]:
        if hold["key"] in waiting_keys:
            continue
        reset = hold.get("reset_at") or "manual resume required"
        key = _display_subscription_key(hold.get("key"))
        print(
            f"  hold: {key} reason={hold.get('reason', 'quota')} "
            f"reset={reset}"
        )
    return 0


def _number(value: object) -> str:
    if isinstance(value, float) and value.is_integer():
        return str(int(value))
    return str(value)


def _print_period(label: str, period: dict) -> None:
    span = period.get("date") or f"{period['from']}..{period['through']}"
    usage = period["usage"]
    unknown = period["unknown"]
    fields = (
        ("turns", "turns"),
        ("input_tokens", "input"),
        ("output_tokens", "output"),
        ("reasoning_tokens", "reasoning"),
        ("cached_tokens", "cache-read"),
        ("cache_write_tokens", "cache-write"),
        ("cost", "cost"),
    )
    values = " ".join(f"{shown}={_number(usage[name])}" for name, shown in fields)
    missing = sum(unknown.values())
    print(f"  {label} {span}: runs={period['runs']} {values} unknown={missing}")
    print("    by subscription:")
    _print_groups(period["by_subscription"])
    print("    by project digest:")
    _print_groups(period["by_project"])


def _print_groups(groups: dict) -> None:
    if not groups:
        print("      none")
        return
    for name, summary in groups.items():
        usage = summary["usage"]
        unknown = sum(summary["unknown"].values())
        print(
            f"      {name}: runs={summary['runs']} input={_number(usage['input_tokens'])} "
            f"output={_number(usage['output_tokens'])} "
            f"cache-read={_number(usage['cached_tokens'])} unknown={unknown}"
        )


def _setup(root: Path) -> dict:
    path = root / ".magnolia" / "llm-setup.json"
    if not path.exists():
        return {}
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    return value if isinstance(value, dict) else {}


def _substitutions(context: LaunchContext) -> dict[str, str]:
    memory_enabled = bool(context.setup.get("memory_enabled")) and bool(
        context.setup.get("memory_model")
    )
    return {
        "@@PROJECT@@": context.project,
        "@@MAGNOLIA_ROOT@@": str(context.root.parent),
        "@@PYTHON@@": context.python,
        "@@MEMORY_ENABLED@@": "true" if memory_enabled else "false",
        "@@COMPCHEM_TOOLS_PORT@@": str(context.tools_port),
    }


def _link(links: dict[Path, Path], project_dir: Path) -> None:
    """Point each link at its target; replace a stale link, never a real file or directory."""
    boundary = project_dir.resolve()
    for link, target in links.items():
        if not link.parent.resolve().is_relative_to(boundary):
            print(
                f"warning: skill link skipped, {link.parent} leaves the project",
                file=sys.stderr,
            )
            continue
        if link.is_symlink():
            if os.readlink(link) == str(target):
                continue
        elif link.exists():
            print(
                f"warning: skill link skipped, {link} exists and is not a link",
                file=sys.stderr,
            )
            continue
        link.parent.mkdir(parents=True, exist_ok=True)
        temporary = link.with_name(f".{link.name}.{os.getpid()}.tmp")
        temporary.unlink(missing_ok=True)
        try:
            temporary.symlink_to(target)
            os.replace(temporary, link)
        finally:
            temporary.unlink(missing_ok=True)


def _emit(launch: Launch) -> None:
    items = [*launch.argv, "", *(f"{key}={value}" for key, value in launch.env.items())]
    sys.stdout.buffer.write(b"\0".join(item.encode("utf-8") for item in items) + b"\0")
    sys.stdout.buffer.flush()


def _validate(launch: Launch) -> None:
    if any(not item for item in launch.argv):
        raise ValueError("argv items must not be empty")
    if any("\0" in item for item in launch.argv):
        raise ValueError("argv items must not contain NUL")
    if any("\0" in key or "\0" in value for key, value in launch.env.items()):
        raise ValueError("env keys and values must not contain NUL")
    if any(not key for key in launch.env):
        raise ValueError("env keys must not be empty")
    if any("=" in key for key in launch.env):
        raise ValueError("env keys must not contain =")


def _error(code: int, message: str) -> int:
    line = str(message).splitlines()[0] if str(message) else "harness launch failed"
    print(line, file=sys.stderr)
    return code


if __name__ == "__main__":
    raise SystemExit(main())
