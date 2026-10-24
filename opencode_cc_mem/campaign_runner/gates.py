"""Artifact checks and deterministic gate execution."""

from __future__ import annotations

import hashlib
import json
import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Mapping, Sequence

from .process import run_process
from .security import SecurityError, ensure_within


class GateError(Exception):
    """Raised when a gate cannot run."""


@dataclass
class OutputRecord:
    path: str
    sha256: str
    bytes: int


@dataclass
class GateResult:
    passed: bool
    errors: list[str] = field(default_factory=list)
    outputs: list[OutputRecord] = field(default_factory=list)
    exit_code: int | None = None
    timed_out: bool = False
    stdout_path: str | None = None
    stderr_path: str | None = None


def render_token(token: str, context: Mapping[str, str]) -> str:
    rendered = token
    for key, value in context.items():
        rendered = rendered.replace("{" + key + "}", value)
    return rendered


def render_argv(argv: Sequence[str], context: Mapping[str, str]) -> list[str]:
    return [render_token(token, context) for token in argv]


def _safe_resolve(base: Path, relative: str) -> Path:
    try:
        return ensure_within(base, relative)
    except SecurityError as exc:
        raise GateError(f"path escapes run directory: {relative}") from exc


def check_outputs(run_dir: Path, outputs: Sequence[str]) -> tuple[list[OutputRecord], list[str]]:
    records: list[OutputRecord] = []
    errors: list[str] = []
    for output in outputs:
        try:
            path = _safe_resolve(run_dir, output)
        except GateError as exc:
            errors.append(str(exc))
            continue
        if not path.is_file():
            errors.append(f"missing output: {output}")
            continue
        body = path.read_bytes()
        if not body:
            errors.append(f"empty output: {output}")
            continue
        records.append(
            OutputRecord(
                path=output,
                sha256=hashlib.sha256(body).hexdigest(),
                bytes=len(body),
            )
        )
    return records, errors


def run_gate(
    run_dir: Path,
    argv: Sequence[str],
    context: Mapping[str, str],
    attempt_dir: Path,
    timeout_seconds: int = 300,
) -> GateResult:
    rendered = render_argv(argv, context)
    stdout_path = attempt_dir / "gate.stdout.log"
    stderr_path = attempt_dir / "gate.stderr.log"
    environment = dict(os.environ)
    environment.update(
        {
            "MAGNOLIA_RUN_DIR": str(run_dir),
            "MAGNOLIA_PROJECT_DIR": str(context.get("project_dir", "")),
            "MAGNOLIA_STAGE": str(context.get("stage", "")),
            "MAGNOLIA_ATTEMPT": str(context.get("attempt", "")),
        }
    )
    try:
        outcome = run_process(
            rendered,
            cwd=run_dir,
            env=environment,
            timeout_seconds=timeout_seconds,
            stdout_path=stdout_path,
            stderr_path=stderr_path,
        )
    except OSError as exc:
        return GateResult(
            passed=False,
            errors=[f"cannot start gate: {exc}"],
            stdout_path=str(stdout_path),
            stderr_path=str(stderr_path),
        )
    errors: list[str] = []
    if outcome.timed_out:
        errors.append(f"gate timed out after {timeout_seconds}s")
    if outcome.returncode != 0:
        errors.append(f"gate exited with code {outcome.returncode}")
        tail = stderr_path.read_text(encoding="utf-8", errors="replace")[-2000:]
        if tail.strip():
            errors.append(tail.strip())
    return GateResult(
        passed=not errors,
        errors=errors,
        exit_code=outcome.returncode,
        timed_out=outcome.timed_out,
        stdout_path=str(stdout_path),
        stderr_path=str(stderr_path),
    )


def write_receipt(path: Path, payload: Mapping[str, object]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.tmp")
    temporary.write_text(
        json.dumps(payload, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    temporary.replace(path)
