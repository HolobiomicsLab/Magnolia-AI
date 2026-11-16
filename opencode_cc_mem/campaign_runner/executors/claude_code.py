"""Governed Claude Code execution and stream-json parsing."""

from __future__ import annotations

import hashlib
import json
import stat
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Any, Callable, Mapping

from campaign_runner.governor import (
    Admission,
    AdmissionRequest,
    QuotaWaiting,
    _control_reason,
)
from campaign_runner.process import ProcessControl, ProcessStartError, run_process
from campaign_runner.security import redact_paths
from campaign_runner.workers import (
    EMPTY_OUTPUT_NOTE,
    InvocationGovernance,
    WorkerError,
    WorkerResult,
)

from .contracts import (
    LimitInfo,
    ResumeArtifact,
    RunOutcome,
    SessionIdentity,
    UsageRecord,
)
from .registry import ExecutorRefused

if TYPE_CHECKING:
    from campaign_runner.launch import PreparedLaunch


FORBIDDEN_ENV = frozenset({"CLAUDE_CONFIG_DIR", "ANTHROPIC_API_KEY"})
FORBIDDEN_FLAGS = frozenset(
    {
        "--bare",
        "--dangerously-skip-permissions",
        "--dangerously-skip-permissions-and-bypass-hook-trust",
        "--permission-mode",
    }
)
NATIVE_TOOLS = {
    "native_read": "Read",
    "native_glob": "Glob",
    "native_grep": "Grep",
    "native_edit": ("Edit", "Write"),
}
CLAUDE_HOOK_EVENTS = frozenset(
    {
        "SessionStart",
        "UserPromptSubmit",
        "PreToolUse",
        "PostToolUse",
        "PostToolUseFailure",
        "Stop",
        "SubagentStart",
        "SubagentStop",
        "SessionEnd",
    }
)
QuotaDetector = Callable[[Mapping[str, Any]], RunOutcome | None]


@dataclass
class ParsedClaudeRun:
    """Normalized data retained from one Claude stream-json invocation."""

    session_id: str | None = None
    reported_model: str | None = None
    final_text: str = ""
    all_text: str = ""
    tool_calls: int = 0
    tool_events: int = 0
    usage: UsageRecord = field(default_factory=UsageRecord)
    model_usage: Mapping[str, Any] = field(default_factory=dict)
    num_turns: int | None = None
    terminal_reason: str | None = None
    is_error: bool | None = None
    result_subtype: str | None = None
    result_seen: bool = False
    diagnostics: list[dict[str, Any]] = field(default_factory=list)
    outcome: RunOutcome | None = None


def parse_claude_stream(
    text: str,
    *,
    resumed: bool = False,
    quota_detector: QuotaDetector | None = None,
) -> ParsedClaudeRun:
    """Parse Claude stream-json without discarding partial output on damage."""
    parsed = ParsedClaudeRun()
    assistant_messages: list[str] = []
    result_text: str | None = None
    result_event: Mapping[str, Any] | None = None
    for line_number, raw_line in enumerate(text.splitlines(), start=1):
        line = raw_line.strip()
        if not line:
            continue
        try:
            event = json.loads(line)
        except json.JSONDecodeError as exc:
            parsed.diagnostics.append(
                {"kind": "invalid_json", "line": line_number, "column": exc.colno}
            )
            continue
        if not isinstance(event, Mapping):
            parsed.diagnostics.append({"kind": "non_object", "line": line_number})
            continue
        _parse_identity(event, parsed)
        event_type = event.get("type")
        if event_type == "assistant":
            _parse_assistant(event, parsed, assistant_messages)
        elif event_type == "user":
            parsed.tool_events += _tool_result_count(event)
        elif event_type == "result":
            parsed.result_seen = True
            result_event = event
            parsed.result_subtype = _text(event.get("subtype"))
            parsed.terminal_reason = _text(event.get("terminal_reason"))
            parsed.is_error = (
                event.get("is_error")
                if isinstance(event.get("is_error"), bool)
                else None
            )
            parsed.num_turns = _integer(event.get("num_turns"))
            parsed.model_usage = (
                event.get("modelUsage")
                if isinstance(event.get("modelUsage"), Mapping)
                else {}
            )
            recent_usage = event.get("model_usage_since_last_result")
            model_source = recent_usage if isinstance(recent_usage, Mapping) else parsed.model_usage
            if parsed.reported_model is None and len(model_source) == 1:
                parsed.reported_model = next(iter(model_source))
            result_text = event.get("result") if isinstance(event.get("result"), str) else None
            parsed.outcome = quota_detector(event) if quota_detector else None
    parsed.all_text = "\n".join(assistant_messages)
    parsed.final_text = result_text if result_text is not None else (
        assistant_messages[-1] if assistant_messages else ""
    )
    if not parsed.all_text and result_text:
        parsed.all_text = result_text
    if result_event is not None:
        parsed.usage = _usage_record(result_event, parsed, resumed=resumed)
    return parsed


def _parse_identity(event: Mapping[str, Any], parsed: ParsedClaudeRun) -> None:
    if parsed.session_id is None:
        parsed.session_id = _text(event.get("session_id"))
    if event.get("type") == "system" and event.get("subtype") == "init":
        parsed.reported_model = _text(event.get("model")) or parsed.reported_model


def _parse_assistant(
    event: Mapping[str, Any], parsed: ParsedClaudeRun, messages: list[str]
) -> None:
    message = event.get("message")
    if not isinstance(message, Mapping):
        return
    content = message.get("content")
    if not isinstance(content, list):
        return
    chunks: list[str] = []
    for block in content:
        if not isinstance(block, Mapping):
            continue
        if block.get("type") == "text" and isinstance(block.get("text"), str):
            chunks.append(block["text"])
        elif block.get("type") == "tool_use":
            parsed.tool_calls += 1
            parsed.tool_events += 1
    if chunks:
        messages.append("".join(chunks))


def _tool_result_count(event: Mapping[str, Any]) -> int:
    message = event.get("message")
    content = message.get("content") if isinstance(message, Mapping) else None
    if not isinstance(content, list):
        return 0
    return sum(
        isinstance(block, Mapping) and block.get("type") == "tool_result"
        for block in content
    )


def _usage_record(
    event: Mapping[str, Any], parsed: ParsedClaudeRun, *, resumed: bool
) -> UsageRecord:
    usage = event.get("usage") if isinstance(event.get("usage"), Mapping) else {}
    details = usage.get("output_tokens_details")
    cache = {
        "read": usage.get("cache_read_input_tokens"),
        "write": usage.get("cache_creation_input_tokens"),
    }
    raw = {
        "schema_version": "claude-stream-json",
        "tokens": {
            "input": usage.get("input_tokens"),
            "output": usage.get("output_tokens"),
            "reasoning": details.get("thinking_tokens")
            if isinstance(details, Mapping)
            else None,
            "cache": cache,
        },
        "cost": _invocation_cost(event, resumed=resumed),
        "usage": usage,
        "modelUsage": parsed.model_usage,
        "model_usage_since_last_result": event.get(
            "model_usage_since_last_result"
        ),
    }
    return UsageRecord.from_raw(
        raw,
        turns=parsed.num_turns,
        reported_model=parsed.reported_model,
    )


def _invocation_cost(event: Mapping[str, Any], *, resumed: bool) -> float | int | None:
    recent = event.get("model_usage_since_last_result")
    if isinstance(recent, Mapping):
        costs = [
            value.get("costUSD")
            for value in recent.values()
            if isinstance(value, Mapping)
        ]
        numeric = [value for value in costs if _number(value) is not None]
        if numeric:
            return sum(numeric)
    return None if resumed else _number(event.get("total_cost_usd"))


@dataclass
class ClaudeCodeExecutor:
    """Run one Claude Code role through immutable launch and governor contracts."""

    prepared: "PreparedLaunch"
    model: str
    role: str
    max_turns: int
    timeout_seconds: int = 1800
    governance: InvocationGovernance | None = None
    resume_permitted: bool = False
    resume_artifact: ResumeArtifact | None = None
    quota_detector: QuotaDetector | None = None

    def __post_init__(self) -> None:
        if not self.prepared.argv:
            raise WorkerError("Claude launch is missing its executable")
        if not self.model:
            raise WorkerError("Claude launch requires an explicit model")
        if self.max_turns < 1:
            raise WorkerError("Claude launch requires a positive turn cap")
        if FORBIDDEN_ENV & set(self.prepared.env):
            names = ", ".join(sorted(FORBIDDEN_ENV & set(self.prepared.env)))
            raise WorkerError(f"forbidden Claude child environment: {names}")
        forbidden = next(
            (
                flag
                for value in self.prepared.argv
                for flag in FORBIDDEN_FLAGS
                if value == flag or value.startswith(flag + "=")
            ),
            None,
        )
        if forbidden:
            raise WorkerError(f"forbidden Claude launch flag: {forbidden}")
        if len(self.prepared.argv) != 1:
            raise WorkerError("Claude prepared argv must contain exactly one executable")
        if self.resume_artifact is not None:
            self.resume_permitted = False
            self._accept_resume_artifact(self.resume_artifact)
            self.resume_permitted = True

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
        """Run at most one same-session empty-output retry."""
        if retries not in (None, 0, 1):
            raise WorkerError("Claude permits at most one empty-output retry")
        if self.resume_artifact is not None:
            if resume_session not in (None, self.resume_artifact.session_id):
                raise ExecutorRefused("Claude resume session differs from its artifact")
            resume_session = self.resume_artifact.session_id
        if resume_session is not None and not self.resume_permitted:
            if self.resume_artifact is None:
                raise ExecutorRefused(
                    "Claude resume requires the same prepared configuration identity"
                )
        if resume_session is not None:
            self._require_bound_session(resume_session)
        retry_limit = 1 if retries is None else retries
        attempt_dir.mkdir(parents=True, exist_ok=True)
        session_id = resume_session
        current_prompt = prompt
        total_usage = _unknown_usage()
        all_text: list[str] = []
        errors: list[dict[str, Any]] = []
        stdout_paths: list[Path] = []
        stderr_paths: list[Path] = []
        attempts = 0
        duration = 0.0
        returncode = 0
        timed_out = False
        stopped_reason = None
        outcome = RunOutcome("failure")
        final_text = ""
        tool_calls = 0
        identity = self._identity(resume_session)
        for attempt_index in range(retry_limit + 1):
            if session_id is not None:
                self._require_bound_session(session_id)
            admission = self._admit(attempt_index, stem, identity, control)
            if isinstance(admission, QuotaWaiting):
                outcome, stopped_reason = self._waiting_outcome(admission, control)
                break
            effective_cap = (
                admission.effective_cap
                if admission is not None and admission.effective_cap is not None
                else self.max_turns
            )
            stdout_path = attempt_dir / f"{stem}_{attempt_index + 1}.jsonl"
            stderr_path = attempt_dir / f"{stem}_{attempt_index + 1}.err.log"
            try:
                with admission or _NoAdmission():
                    argv = self._argv(current_prompt, session_id, effective_cap)
                    invocation_control = self._control(control, admission)
                    was_resumed = session_id is not None
                    process = run_process(
                        argv,
                        cwd,
                        self.prepared.env,
                        self.timeout_seconds,
                        stdout_path,
                        stderr_path,
                        invocation_control,
                    )
                    if admission is not None and not process.client_started:
                        admission._cancel_before_client_exec()
                    if process.client_started:
                        attempts += 1
                    parsed = parse_claude_stream(
                        stdout_path.read_text(encoding="utf-8", errors="replace"),
                        resumed=was_resumed,
                        quota_detector=self.quota_detector,
                    )
                    outcome = self._outcome(process, parsed)
                    if (
                        not outcome.overrides_repair
                        and session_id is not None
                        and parsed.session_id is not None
                        and parsed.session_id != session_id
                    ):
                        parsed.diagnostics.append(
                            {
                                "kind": "resume_identity_mismatch",
                                "expected": session_id,
                                "observed": parsed.session_id,
                            }
                        )
                        outcome = RunOutcome("refused")
                    if parsed.session_id is not None and outcome.kind != "refused":
                        if session_id is None:
                            self._bind_session(parsed.session_id)
                        else:
                            self._require_bound_session(parsed.session_id)
                    identity = self._identity(parsed.session_id or session_id)
                    usage = self._usage(
                        parsed.usage,
                        identity,
                        admission,
                        process.duration_seconds,
                        outcome,
                    )
                    if admission is not None and process.client_started:
                        admission.release(usage=usage, outcome=outcome)
            except ProcessStartError as exc:
                if admission is not None:
                    admission._cancel_before_client_exec()
                raise WorkerError(f"cannot start Claude: {exc}") from exc
            except OSError as exc:
                raise WorkerError(f"cannot start Claude: {exc}") from exc
            stdout_paths.append(stdout_path)
            stderr_paths.append(stderr_path)
            duration += process.duration_seconds
            returncode = process.returncode
            timed_out = process.timed_out
            stopped_reason = process.stopped_reason
            session_id = parsed.session_id or session_id
            final_text = parsed.final_text
            if parsed.all_text:
                all_text.append(parsed.all_text)
            tool_calls += parsed.tool_calls
            errors.extend(parsed.diagnostics)
            _merge_usage(total_usage, parsed.usage)
            if outcome.overrides_repair or process.stopped_reason:
                break
            if outcome.kind != "success" or final_text.strip():
                break
            if not parsed.result_seen or not session_id:
                break
            current_prompt = EMPTY_OUTPUT_NOTE
        final_path = attempt_dir / f"{stem}_final.md"
        final_path.write_text(final_text, encoding="utf-8")
        redact_paths([*stdout_paths, *stderr_paths, final_path])
        return WorkerResult(
            returncode,
            timed_out,
            duration,
            session_id,
            final_text,
            "\n".join(all_text),
            tool_calls,
            total_usage,
            attempts,
            stdout_paths,
            stderr_paths,
            stopped_reason,
            errors,
            outcome,
            identity,
            self._resume_artifact(session_id),
        )

    def _argv(self, prompt: str, session_id: str | None, max_turns: int) -> list[str]:
        settings = self._native_artifact("claude-settings.json", "hooks")
        mcp = self._native_artifact("claude-mcp.json", "mcpServers")
        argv = [
            self.prepared.argv[0],
            "-p",
            prompt,
            "--setting-sources",
            "project",
            "--settings",
            str(settings),
            "--mcp-config",
            str(mcp),
            "--strict-mcp-config",
            "--output-format",
            "stream-json",
            "--verbose",
            "--max-turns",
            str(min(self.max_turns, max_turns)),
            "--model",
            self.model,
        ]
        if session_id:
            argv.extend(["--resume", session_id])
        native, mcp_tools = self._allowed_tools()
        if self.role in {"helper", "evaluator"}:
            argv.extend(["--tools", ",".join(native)])
        if native or mcp_tools:
            argv.extend(["--allowedTools", *native, *mcp_tools])
        return argv

    def _allowed_tools(self) -> tuple[list[str], list[str]]:
        policy_path = self.prepared.invocation_dir / "policy.json"
        policy = json.loads(policy_path.read_text(encoding="utf-8"))
        capabilities = policy.get("effective_capabilities", [])
        native: list[str] = []
        for capability in capabilities:
            mapped = NATIVE_TOOLS.get(capability)
            if isinstance(mapped, tuple):
                native.extend(mapped)
            elif isinstance(mapped, str):
                native.append(mapped)
        mcp_tools = [
            _claude_mcp_name(item)
            for item in policy.get("mcp_tool_allowlist", [])
            if isinstance(item, str)
        ]
        return native, mcp_tools

    def _native_artifact(self, name: str, required_key: str) -> Path:
        path = self.prepared.invocation_dir / name
        expected = self.prepared.files.get(path)
        try:
            artifact_stat = path.lstat()
            body = path.read_bytes()
            payload = json.loads(body)
        except (OSError, ValueError, TypeError) as exc:
            raise ExecutorRefused(
                f"missing or invalid Claude-native launch artifact: {name}"
            ) from exc
        if (
            expected is None
            or not stat.S_ISREG(artifact_stat.st_mode)
            or artifact_stat.st_mode & 0o222
            or body != expected
            or not isinstance(payload, Mapping)
            or not isinstance(payload.get(required_key), Mapping)
        ):
            raise ExecutorRefused(
                f"missing or invalid Claude-native launch artifact: {name}"
            )
        if name == "claude-settings.json" and not CLAUDE_HOOK_EVENTS.issubset(
            payload[required_key]
        ):
            raise ExecutorRefused(
                f"missing or invalid Claude-native launch artifact: {name}"
            )
        return path

    def _binding_identity(self) -> dict[str, str]:
        project = self.prepared.env.get("MAGNOLIA_PROJECT_IDENTITY")
        if not project:
            raise ExecutorRefused("Claude launch is missing its project identity")
        digest = hashlib.sha256(
            (
                self.prepared.policy_digest
                + "\0"
                + self.prepared.config_digest
            ).encode()
        ).hexdigest()
        return {
            "client": "claude-code",
            "configuration_identity_digest": digest,
            "role": self.role,
            "project_identity": project,
        }

    def _binding_path(self) -> Path:
        return self.prepared.invocation_dir / "session-bindings.json"

    def _resume_artifact(self, session_id: str | None) -> ResumeArtifact | None:
        if session_id is None:
            return None
        binding = self._binding_identity()
        return ResumeArtifact(
            "claude-code",
            session_id,
            binding["configuration_identity_digest"],
            binding["role"],
            binding["project_identity"],
        )

    def _accept_resume_artifact(self, artifact: ResumeArtifact) -> None:
        if artifact != self._resume_artifact(artifact.session_id):
            raise ExecutorRefused(
                "Claude resume artifact does not match client, role, policy, or project"
            )
        self._require_bound_session(artifact.session_id)

    def _load_bindings(self) -> dict[str, Any]:
        path = self._binding_path()
        if not path.exists():
            return {
                "schema_version": "magnolia-claude-session-bindings-v1",
                "sessions": {},
            }
        try:
            binding_stat = path.lstat()
            payload = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError, TypeError) as exc:
            raise ExecutorRefused("invalid Claude session binding state") from exc
        if (
            not stat.S_ISREG(binding_stat.st_mode)
            or not isinstance(payload, dict)
            or not isinstance(payload.get("sessions"), dict)
        ):
            raise ExecutorRefused("invalid Claude session binding state")
        return payload

    def _bind_session(self, session_id: str) -> None:
        payload = self._load_bindings()
        expected = self._binding_identity()
        current = payload["sessions"].get(session_id)
        if current is not None and current != expected:
            raise ExecutorRefused("Claude session has a different bound identity")
        if current == expected:
            return
        payload["sessions"][session_id] = expected
        path = self._binding_path()
        temporary = path.with_name(f".{path.name}.{uuid.uuid4().hex}.tmp")
        temporary.write_text(
            json.dumps(payload, sort_keys=True, separators=(",", ":")) + "\n",
            encoding="utf-8",
        )
        temporary.chmod(0o600)
        temporary.replace(path)

    def _require_bound_session(self, session_id: str) -> None:
        current = self._load_bindings()["sessions"].get(session_id)
        if current is None:
            raise ExecutorRefused(f"unknown Claude session: {session_id}")
        if current != self._binding_identity():
            raise ExecutorRefused("Claude session has a different bound identity")

    def _identity(self, session_id: str | None) -> SessionIdentity | None:
        if session_id is None:
            return None
        digest = hashlib.sha256(
            (
                self.prepared.policy_digest
                + "\0"
                + self.prepared.config_digest
                + "\0"
                + self.role
            ).encode()
        ).hexdigest()
        project = self.governance.project_digest if self.governance else "ungoverned"
        return SessionIdentity("claude-code", self.model, session_id, project, self.role, digest)

    def _admit(
        self,
        attempt_index: int,
        stem: str,
        identity: SessionIdentity | None,
        control: ProcessControl | None,
    ) -> Admission | QuotaWaiting | None:
        if self.governance is None:
            return None
        governance = self.governance
        request = AdmissionRequest(
            governance.route,
            f"{governance.parent_run_id}:{stem}:{attempt_index + 1}:{uuid.uuid4().hex}",
            governance.parent_run_id,
            governance.project_digest,
            self.role,
            governance.role_cap or self.max_turns,
            identity,
        )
        while True:
            admitted = governance.governor.admit(request, control)
            if isinstance(admitted, Admission):
                return admitted
            if admitted.reason in {"quota", "daily_limit", "cancelled"}:
                return admitted
            if not governance.governor.wait(admitted, control):
                return QuotaWaiting(admitted.key, admitted.caller_id, "cancelled")

    def _waiting_outcome(
        self, waiting: QuotaWaiting, control: ProcessControl | None
    ) -> tuple[RunOutcome, str]:
        assert self.governance is not None
        self.governance.governor.cancel_wait(waiting)
        reason = _control_reason(control) or waiting.reason
        if waiting.reason not in {"quota", "daily_limit"}:
            return RunOutcome("cancelled"), reason
        return (
            RunOutcome(
                "quota",
                LimitInfo(
                    "daily_limit"
                    if waiting.reason == "daily_limit"
                    else "governor quota",
                    waiting.reset_at,
                    "usage governor",
                    waiting.hold_id,
                ),
            ),
            reason,
        )

    def _control(
        self, control: ProcessControl | None, admission: Admission | None
    ) -> ProcessControl:
        def on_start(pid: int, pgid: int) -> None:
            if admission is not None:
                admission.mark_started(pid=pid, pgid=pgid)
            if control and control.on_start:
                control.on_start(pid, pgid)

        def observe(stdout: Path, stderr: Path) -> RunOutcome | None:
            parsed = parse_claude_stream(
                stdout.read_text(encoding="utf-8", errors="replace"),
                quota_detector=self.quota_detector,
            )
            if parsed.outcome is not None:
                return parsed.outcome
            return control.observe(stdout, stderr) if control and control.observe else None

        return ProcessControl(
            should_stop=control.should_stop if control else None,
            on_start=on_start,
            on_heartbeat=control.on_heartbeat if control else None,
            observe=observe,
            poll_interval=control.poll_interval if control else 0.05,
            hold_before_exec=admission is not None,
        )

    def _outcome(self, process: Any, parsed: ParsedClaudeRun) -> RunOutcome:
        if parsed.outcome is not None:
            return parsed.outcome
        if process.outcome is not None and process.outcome.kind in {
            "quota",
            "timeout",
            "cancelled",
        }:
            return process.outcome
        failed_result = parsed.is_error is True or (
            parsed.result_subtype is not None
            and parsed.result_subtype != "success"
        )
        if process.returncode != 0 or failed_result or not parsed.result_seen:
            return RunOutcome("failure")
        return RunOutcome("success")

    def _usage(
        self,
        usage: UsageRecord,
        identity: SessionIdentity | None,
        admission: Admission | None,
        duration: float,
        outcome: RunOutcome,
    ) -> UsageRecord:
        usage.session_identity = identity
        usage.client = "claude-code"
        usage.resolved_model = self.model
        usage.duration_seconds = duration
        usage.outcome = outcome
        if admission is not None:
            usage.invocation_id = admission.request.invocation_id
            usage.parent_run_id = admission.request.parent_run_id
            usage.subscription_key = admission.request.route.subscription_key
            usage.run_kind = admission.request.run_kind
            usage.project_digest = admission.request.project_digest
        return usage


def _claude_mcp_name(value: str) -> str:
    if not value.startswith("mcp:") or "." not in value:
        raise WorkerError(f"invalid Claude MCP capability: {value}")
    server, tool = value[4:].split(".", 1)
    return f"mcp__{server}__{tool}"


def _unknown_usage() -> dict[str, float | None]:
    return {
        "input_tokens": None,
        "output_tokens": None,
        "reasoning_tokens": None,
        "cache_read_tokens": None,
        "cache_write_tokens": None,
        "cost_usd": None,
    }


def _merge_usage(total: dict[str, float | None], usage: UsageRecord) -> None:
    values = {
        "input_tokens": usage.input_tokens,
        "output_tokens": usage.output_tokens,
        "reasoning_tokens": usage.reasoning_tokens,
        "cache_read_tokens": usage.cached_tokens,
        "cache_write_tokens": usage.cache_write_tokens,
        "cost_usd": usage.cost,
    }
    for key, value in values.items():
        if isinstance(value, (int, float)) and not isinstance(value, bool):
            total[key] = float(total[key] or 0) + float(value)


def _text(value: Any) -> str | None:
    return value if isinstance(value, str) else None


def _integer(value: Any) -> int | None:
    return value if isinstance(value, int) and not isinstance(value, bool) else None


def _number(value: Any) -> float | int | None:
    return value if isinstance(value, (int, float)) and not isinstance(value, bool) else None


class _NoAdmission:
    def __enter__(self) -> None:
        return None

    def __exit__(self, exc_type: Any, exc: Any, traceback: Any) -> bool:
        return False
