"""Shared values passed between the launch CLI and client renderers."""

from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path

from compchem_memory.harness.neutral import NeutralConfig


@dataclass
class LaunchContext:
    """Project and runtime values used to render a client launch."""

    root: Path
    project: str
    project_dir: Path
    python: str
    tools_port: int
    model: str | None
    setup: dict


@dataclass
class Launch:
    """A client command, environment additions, and files for the CLI to write."""

    argv: list[str]
    env: dict[str, str]
    files: dict[Path, bytes]
    conflicts: list[str] = field(default_factory=list)
    links: dict[Path, Path] = field(default_factory=dict)
    """Symlinks for the CLI to create: link path -> absolute target."""


Renderer = Callable[[NeutralConfig, LaunchContext], Launch]
RENDERERS: dict[str, Renderer] = {}
