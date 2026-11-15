"""Render a client launch and emit its NUL-framed command protocol."""

from __future__ import annotations

import argparse
import json
import os
import shutil
import sys
from collections.abc import Sequence
from pathlib import Path

from compchem_memory.harness.files import atomic_write, validate_project_paths
from compchem_memory.harness.neutral import NeutralConfig, load
from compchem_memory.harness.protocol import RENDERERS, Launch, LaunchContext, Renderer


def main(argv: Sequence[str] | None = None) -> int:
    """Render one launch, returning the protocol's process exit code."""
    try:
        args = _parse(argv)
        launch, renderer = _render(args)
        _validate(launch)
        project_dir = args.root.resolve() / "projects" / args.project
        validate_project_paths(launch.files, project_dir)
        for conflict in launch.conflicts:
            print(f"warning: {str(conflict).splitlines()[0]}", file=sys.stderr)
        for path, content in launch.files.items():
            atomic_write(path, content)
        _link(launch.links, project_dir)
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
    launch.add_argument("--model")
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
    # main_model is an OpenCode provider/model id: it never reaches another client.
    client_models = setup.get("models")
    client_model = client_models.get(args.harness) if isinstance(client_models, dict) else None
    model = args.model or (client_model if isinstance(client_model, str) and client_model else None)
    project_dir = root / "projects" / args.project
    context = LaunchContext(
        root=root,
        project=args.project,
        project_dir=project_dir,
        python=args.python,
        tools_port=args.tools_port,
        model=model,
        setup=setup,
    )
    neutral = load(root / "opencode.json.template", _substitutions(context), setup)
    return renderer(neutral, context), renderer


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
