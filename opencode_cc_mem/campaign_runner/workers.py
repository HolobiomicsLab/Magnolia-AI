"""opencode v2 worker execution and event parsing."""

from __future__ import annotations

import json
import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Mapping

from .process import run_process


EMPTY_OUTPUT_NOTE = (
    "Your previous turn returned no final text. Reply now with the complete "
    "result. Do not continue investigating."
)


class WorkerError(Exception):
    """Raised when a worker cannot be executed."""


@dataclass
class ParsedRun:
    session_id: str | None = None
    final_text: str = ""
    all_text: str = ""
    tool_calls: int = 0
    usage: dict[str, float] = field(default_factory=dict)
    parse_errors: int = 0


@dataclass
class WorkerResult:
    returncode: int
    timed_out: bool
    duration_seconds: float
    session_id: str | None
    final_text: str
    all_text: str
    tool_calls: int
    usage: dict[str, float]
    attempts: int
    stdout_paths: list[Path]
    stderr_paths: list[Path]


def empty_usage() -> dict[str, float]:
    return {
        "input_tokens": 0.0,
        "output_tokens": 0.0,
        "reasoning_tokens": 0.0,
        "cache_read_tokens": 0.0,
        "cache_write_tokens": 0.0,
        "cost_usd": 0.0,
    }


def _add_usage(total: dict[str, float], event_part: Mapping[str, Any]) -> None:
    tokens = event_part.get("tokens")
    if isinstance(tokens, dict):
        total["input_tokens"] += float(tokens.get("input", 0) or 0)
        total["output_tokens"] += float(tokens.get("output", 0) or 0)
        total["reasoning_tokens"] += float(tokens.get("reasoning", 0) or 0)
        cache = tokens.get("cache")
        if isinstance(cache, dict):
            total["cache_read_tokens"] += float(cache.get("read", 0) or 0)
            total["cache_write_tokens"] += float(cache.get("write", 0) or 0)
    cost = event_part.get("cost")
    if isinstance(cost, (int, float)) and not isinstance(cost, bool):
        total["cost_usd"] += float(cost)


def parse_run_events(text: str) -> ParsedRun:
    parsed = ParsedRun(usage=empty_usage())
    texts: dict[str, list[str]] = {}
    order: list[str] = []
    for line in text.splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            event = json.loads(line)
        except json.JSONDecodeError:
            parsed.parse_errors += 1
            continue
        if not isinstance(event, dict):
            parsed.parse_errors += 1
            continue
        if parsed.session_id is None:
            session_id = event.get("sessionID")
            if isinstance(session_id, str):
                parsed.session_id = session_id
        part = event.get("part")
        if not isinstance(part, dict):
            continue
        part_type = part.get("type")
        if part_type == "text":
            body = part.get("text")
            if isinstance(body, str) and body:
                message_id = str(part.get("messageID") or "_")
                if message_id not in texts:
                    order.append(message_id)
                    texts[message_id] = []
                texts[message_id].append(body)
        elif part_type == "tool":
            parsed.tool_calls += 1
        elif part_type == "step-finish":
            _add_usage(parsed.usage, part)
    parsed.all_text = "\n".join(item for message in order for item in texts[message])
    parsed.final_text = "\n".join(texts[order[-1]]) if order else ""
    return parsed


@dataclass
class OpenCodeExecutor:
    binary: str
    model: str
    agent: str
    server_url: str | None = None
    db_path: str | None = None
    environment: Mapping[str, str] | None = None
    timeout_seconds: int = 1800
    max_format_retries: int = 2
    max_steps: int | None = None

    def run(
        self,
        prompt: str,
        cwd: str | Path,
        attempt_dir: Path,
        stem: str = "worker",
        resume_session: str | None = None,
        retries: int | None = None,
    ) -> WorkerResult:
        attempt_dir.mkdir(parents=True, exist_ok=True)
        retry_limit = self.max_format_retries if retries is None else retries
        environment = dict(os.environ)
        if self.environment:
            environment.update(self.environment)
        if self.db_path:
            environment["OPENCODE_DB"] = self.db_path
        if self.max_steps is not None:
            environment["OPENCODE_CONFIG_CONTENT"] = json.dumps(
                {"agents": {self.agent: {"steps": self.max_steps}}}
            )
        session_id = resume_session
        final_text = ""
        all_text = ""
        total_tool_calls = 0
        total_usage = empty_usage()
        stdout_paths: list[Path] = []
        stderr_paths: list[Path] = []
        total_duration = 0.0
        returncode = 0
        timed_out = False
        current_prompt = prompt
        attempts = 0
        for attempt_index in range(retry_limit + 1):
            attempts += 1
            argv = self._argv(current_prompt, session_id)
            stdout_path = attempt_dir / f"{stem}_{attempt_index + 1}.jsonl"
            stderr_path = attempt_dir / f"{stem}_{attempt_index + 1}.err.log"
            try:
                outcome = run_process(
                    argv=argv,
                    cwd=cwd,
                    env=environment,
                    timeout_seconds=self.timeout_seconds,
                    stdout_path=stdout_path,
                    stderr_path=stderr_path,
                )
            except OSError as exc:
                raise WorkerError(f"cannot start worker: {exc}") from exc
            stdout_paths.append(stdout_path)
            stderr_paths.append(stderr_path)
            total_duration += outcome.duration_seconds
            returncode = outcome.returncode
            timed_out = outcome.timed_out
            parsed = parse_run_events(
                stdout_path.read_text(encoding="utf-8", errors="replace")
            )
            if parsed.session_id:
                session_id = parsed.session_id
            final_text = parsed.final_text
            all_text = parsed.all_text
            total_tool_calls += parsed.tool_calls
            _merge_usage(total_usage, parsed.usage)
            if outcome.timed_out or outcome.returncode != 0:
                break
            if final_text.strip():
                break
            if not session_id:
                break
            current_prompt = EMPTY_OUTPUT_NOTE
            session_id = parsed.session_id
        (attempt_dir / f"{stem}_final.md").write_text(final_text, encoding="utf-8")
        return WorkerResult(
            returncode=returncode,
            timed_out=timed_out,
            duration_seconds=total_duration,
            session_id=session_id,
            final_text=final_text,
            all_text=all_text,
            tool_calls=total_tool_calls,
            usage=total_usage,
            attempts=attempts,
            stdout_paths=stdout_paths,
            stderr_paths=stderr_paths,
        )

    def _argv(self, prompt: str, session_id: str | None) -> list[str]:
        argv = [
            self.binary,
            "run",
            "--model",
            self.model,
            "--agent",
            self.agent,
            "--format",
            "json",
            "--auto",
        ]
        if self.server_url:
            argv.extend(["--server", self.server_url])
        if session_id:
            argv.extend(["--session", session_id])
        argv.append(prompt)
        return argv


def _merge_usage(total: dict[str, float], item: Mapping[str, Any]) -> None:
    for key in total:
        value = item.get(key)
        if isinstance(value, (int, float)) and not isinstance(value, bool):
            total[key] += float(value)
