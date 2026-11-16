"""opencode v2 worker execution and event parsing."""

from __future__ import annotations

import json
import os
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Mapping, Protocol

from .executors.contracts import (
    LimitInfo,
    ResumeArtifact,
    RunOutcome,
    SessionIdentity,
    UsageRecord,
)
from .governor import (
    Admission,
    AdmissionRequest,
    Governor,
    QuotaWaiting,
    _control_reason,
)
from .routing import ResolvedRoute

from .process import ProcessControl, ProcessStartError, run_process
from .security import redact_paths


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
    usage_observed: bool = False
    parse_errors: int = 0
    errors: list[dict[str, Any]] = field(default_factory=list)


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
    stopped_reason: str | None = None
    errors: list[dict[str, Any]] = field(default_factory=list)
    outcome: RunOutcome | None = None
    session_identity: SessionIdentity | None = None
    resume_artifact: ResumeArtifact | None = None


class WorkerExecutor(Protocol):
    def run(
        self,
        prompt: str,
        cwd: str | Path,
        attempt_dir: Path,
        stem: str = "worker",
        resume_session: str | None = None,
        retries: int | None = None,
        control: ProcessControl | None = None,
    ) -> WorkerResult: ...


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
            # Observed on OpenCode 2.3.0: a failed turn is reported as a
            # top-level {"type": "error", "error": {...}} line with no part.
            if event.get("type") == "error":
                error = event.get("error")
                parsed.errors.append(
                    error if isinstance(error, dict) else {"message": str(error)}
                )
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
            parsed.usage_observed = True
    parsed.all_text = "\n".join(item for message in order for item in texts[message])
    parsed.final_text = "\n".join(texts[order[-1]]) if order else ""
    return parsed


@dataclass(frozen=True)
class InvocationGovernance:
    """Opt-in governor inputs for each physical worker invocation."""

    governor: Governor
    route: ResolvedRoute
    parent_run_id: str
    project_digest: str
    role_cap: int | None = None
    session_identity: SessionIdentity | None = None
    usage_is_session_cumulative: bool = False


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
    governance: InvocationGovernance | None = None
    service_cap_check: Callable[[int], None] | None = None

    def run(
        self,
        prompt: str,
        cwd: str | Path,
        attempt_dir: Path,
        stem: str = "worker",
        resume_session: str | None = None,
        retries: int | None = None,
        control: ProcessControl | None = None,
    ) -> WorkerResult:
        attempt_dir.mkdir(parents=True, exist_ok=True)
        retry_limit = self.max_format_retries if retries is None else retries
        environment = dict(os.environ)
        if self.environment:
            environment.update(self.environment)
        environment["PWD"] = str(Path(cwd).resolve())
        if self.db_path:
            environment["OPENCODE_DB"] = self.db_path
        if self.max_steps is not None:
            try:
                config = json.loads(environment.get("OPENCODE_CONFIG_CONTENT", "{}"))
                if not isinstance(config, dict):
                    config = {}
            except json.JSONDecodeError:
                config = {}
            agents = config.setdefault("agents", {})
            if not isinstance(agents, dict):
                agents = {}
                config["agents"] = agents
            agent_config = agents.setdefault(self.agent, {})
            if not isinstance(agent_config, dict):
                agent_config = {}
                agents[self.agent] = agent_config
            agent_config["steps"] = self.max_steps
            environment["OPENCODE_CONFIG_CONTENT"] = json.dumps(config)
        session_id = resume_session
        final_text = ""
        all_text = ""
        total_tool_calls = 0
        total_usage = empty_usage()
        errors: list[dict[str, Any]] = []
        stdout_paths: list[Path] = []
        stderr_paths: list[Path] = []
        total_duration = 0.0
        returncode = 0
        timed_out = False
        stopped_reason: str | None = None
        typed_outcome: RunOutcome | None = None
        current_prompt = prompt
        attempts = 0
        usage_baseline: UsageRecord | None = None
        for attempt_index in range(retry_limit + 1):
            argv = self._argv(current_prompt, session_id)
            stdout_path = attempt_dir / f"{stem}_{attempt_index + 1}.jsonl"
            stderr_path = attempt_dir / f"{stem}_{attempt_index + 1}.err.log"
            admission = self._admit_invocation(
                attempt_index, stem, session_id, control
            )
            if isinstance(admission, QuotaWaiting):
                governance = self.governance
                assert governance is not None
                governance.governor.cancel_wait(admission)
                if admission.reason in {"quota", "daily_limit"}:
                    typed_outcome = RunOutcome(
                        "quota",
                        LimitInfo(
                            "daily_limit"
                            if admission.reason == "daily_limit"
                            else "governor quota",
                            admission.reset_at,
                            "usage governor",
                            admission.hold_id,
                        ),
                    )
                else:
                    typed_outcome = RunOutcome("cancelled")
                stopped_reason = _control_reason(control) or admission.reason
                break
            attempts += 1
            invocation_control = _invocation_control(control, admission)
            invocation_environment = environment
            if admission is not None and admission.effective_cap is not None:
                invocation_environment = _with_step_cap(
                    environment, self.agent, admission.effective_cap
                )
            effective_cap = (
                admission.effective_cap
                if admission is not None and admission.effective_cap is not None
                else self.max_steps
            )
            if self.service_cap_check is not None:
                if effective_cap is None:
                    if admission is not None:
                        admission.cancel_before_launch()
                    raise WorkerError("OpenCode service cap cannot be verified without a cap")
                try:
                    self.verify_service_cap(effective_cap)
                except BaseException:
                    if admission is not None:
                        admission.cancel_before_launch()
                    raise
            try:
                with admission or _NoAdmission():
                    try:
                        outcome = run_process(
                            argv=argv,
                            cwd=cwd,
                            env=invocation_environment,
                            timeout_seconds=self.timeout_seconds,
                            stdout_path=stdout_path,
                            stderr_path=stderr_path,
                            control=invocation_control,
                        )
                    except ProcessStartError:
                        if admission is not None:
                            admission._cancel_before_client_exec()
                        raise
                    if admission is not None and not outcome.client_started:
                        admission._cancel_before_client_exec()
                    parsed = parse_run_events(
                        stdout_path.read_text(encoding="utf-8", errors="replace")
                    )
                    usage_record, usage_baseline = self._usage_record(
                        parsed,
                        usage_baseline,
                        admission,
                    )
                    if admission is not None and outcome.client_started:
                        admission.release(usage=usage_record, outcome=outcome.outcome)
            except OSError as exc:
                raise WorkerError(f"cannot start worker: {exc}") from exc
            stdout_paths.append(stdout_path)
            stderr_paths.append(stderr_path)
            total_duration += outcome.duration_seconds
            returncode = outcome.returncode
            timed_out = outcome.timed_out
            stopped_reason = outcome.stopped_reason
            typed_outcome = outcome.outcome
            if parsed.session_id:
                session_id = parsed.session_id
            final_text = parsed.final_text
            all_text = parsed.all_text
            total_tool_calls += parsed.tool_calls
            errors.extend(parsed.errors)
            if self.governance is None:
                _merge_usage(total_usage, parsed.usage)
            elif usage_record is not None:
                _merge_usage_record(total_usage, usage_record)
            if typed_outcome and typed_outcome.overrides_repair:
                break
            if outcome.stopped_reason:
                break
            if outcome.timed_out or outcome.returncode != 0:
                break
            if final_text.strip():
                break
            if not session_id:
                break
            current_prompt = EMPTY_OUTPUT_NOTE
            session_id = parsed.session_id
        (attempt_dir / f"{stem}_final.md").write_text(final_text, encoding="utf-8")
        redact_paths([*stdout_paths, *stderr_paths, attempt_dir / f"{stem}_final.md"])
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
            stopped_reason=stopped_reason,
            errors=errors,
            outcome=typed_outcome,
        )

    def verify_service_cap(self, effective_cap: int) -> None:
        """Recheck an attached service at every admitted invocation."""
        if self.service_cap_check is not None:
            self.service_cap_check(effective_cap)

    def _admit_invocation(
        self,
        attempt_index: int,
        stem: str,
        session_id: str | None,
        control: ProcessControl | None,
    ) -> Admission | QuotaWaiting | None:
        governance = self.governance
        if governance is None:
            return None
        invocation_id = (
            f"{governance.parent_run_id}:{stem}:{attempt_index + 1}:{uuid.uuid4().hex}"
        )
        identity = governance.session_identity
        if identity is not None and session_id and identity.session_id != session_id:
            identity = None
        request = AdmissionRequest(
            route=governance.route,
            invocation_id=invocation_id,
            parent_run_id=governance.parent_run_id,
            project_digest=governance.project_digest,
            run_kind=governance.route.role,
            role_cap=governance.role_cap or self.max_steps,
            session_identity=identity,
        )
        while True:
            admitted = governance.governor.admit(request, control)
            if isinstance(admitted, Admission):
                return admitted
            if admitted.reason in {"quota", "daily_limit", "cancelled"}:
                return admitted
            if not governance.governor.wait(admitted, control):
                return QuotaWaiting(
                    admitted.key,
                    admitted.caller_id,
                    "cancelled",
                )

    def _usage_record(
        self,
        parsed: ParsedRun,
        baseline: UsageRecord | None,
        admission: Admission | None,
    ) -> tuple[UsageRecord | None, UsageRecord | None]:
        if admission is None or not parsed.usage_observed:
            return None, baseline
        raw = _raw_usage(parsed.usage)
        absolute = UsageRecord.from_raw(raw)
        cumulative = bool(
            self.governance and self.governance.usage_is_session_cumulative
        )
        record = UsageRecord.from_raw(
            raw,
            baseline=baseline if cumulative else None,
            session_identity=admission.request.session_identity,
            invocation_id=admission.request.invocation_id,
            parent_run_id=admission.request.parent_run_id,
            subscription_key=admission.request.route.subscription_key,
            run_kind=admission.request.run_kind,
            project_digest=admission.request.project_digest,
        )
        return record, absolute if cumulative else baseline

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


def _merge_usage_record(total: dict[str, float], usage: UsageRecord) -> None:
    values = {
        "input_tokens": usage.input_tokens,
        "output_tokens": usage.output_tokens,
        "reasoning_tokens": usage.reasoning_tokens,
        "cache_read_tokens": usage.cached_tokens,
        "cache_write_tokens": usage.cache_write_tokens,
        "cost_usd": usage.cost,
    }
    _merge_usage(total, values)


def _raw_usage(usage: Mapping[str, float]) -> dict[str, Any]:
    return {
        "schema_version": "opencode-usage-v1",
        "tokens": {
            "input": usage.get("input_tokens"),
            "output": usage.get("output_tokens"),
            "reasoning": usage.get("reasoning_tokens"),
            "cache": {
                "read": usage.get("cache_read_tokens"),
                "write": usage.get("cache_write_tokens"),
            },
        },
        "cost": usage.get("cost_usd"),
    }


def _with_step_cap(
    environment: Mapping[str, str], agent: str, cap: int
) -> dict[str, str]:
    updated = dict(environment)
    try:
        config = json.loads(updated.get("OPENCODE_CONFIG_CONTENT", "{}"))
    except json.JSONDecodeError:
        config = {}
    if not isinstance(config, dict):
        config = {}
    agents = config.setdefault("agents", {})
    if not isinstance(agents, dict):
        agents = {}
        config["agents"] = agents
    agent_config = agents.setdefault(agent, {})
    if not isinstance(agent_config, dict):
        agent_config = {}
        agents[agent] = agent_config
    agent_config["steps"] = cap
    updated["OPENCODE_CONFIG_CONTENT"] = json.dumps(config)
    return updated


def _invocation_control(
    control: ProcessControl | None, admission: Admission | None
) -> ProcessControl | None:
    if admission is None:
        return control

    def on_start(pid: int, pgid: int) -> None:
        if control and control.on_start:
            control.on_start(pid, pgid)
        admission.mark_started(pid=pid, pgid=pgid)

    return ProcessControl(
        should_stop=control.should_stop if control else None,
        on_start=on_start,
        on_heartbeat=control.on_heartbeat if control else None,
        observe=control.observe if control else None,
        poll_interval=control.poll_interval if control else 0.2,
        hold_before_exec=True,
    )


class _NoAdmission:
    def __enter__(self) -> None:
        return None

    def __exit__(self, exc_type: Any, exc: Any, traceback: Any) -> bool:
        return False
