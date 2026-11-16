"""Governed Vibe execution, trust precheck, and export parsing."""

from __future__ import annotations

import hashlib
import json
import re
import stat
import tomllib
import uuid
import weakref
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Mapping

from campaign_runner.governor import Admission, AdmissionRequest, QuotaWaiting, _control_reason
from campaign_runner.process import ProcessControl, ProcessStartError, run_process
from campaign_runner.security import redact_paths
from campaign_runner.workers import InvocationGovernance, WorkerError, WorkerResult
from compchem_memory.harness.render_vibe import READ_ONLY_DISABLED_TOOLS

from .contracts import (
    LimitInfo,
    ResumeArtifact,
    RunOutcome,
    SessionIdentity,
    UsageRecord,
    binding_identity_matches,
)
from .registry import ExecutorRefused


FORBIDDEN_ENV = frozenset({"VIBE_HOME", "ANTHROPIC_API_KEY"})
_FALLBACK_MODEL = re.compile(r"Model '.+?' is not available [—-]+ running '([^']+)'", re.I)


@dataclass
class ParsedVibeRun:
    session_id: str | None = None
    reported_model: str | None = None
    final_text: str = ""
    all_text: str = ""
    turns: int | None = None
    result_seen: bool = False
    usage: UsageRecord = field(default_factory=UsageRecord)
    diagnostics: list[dict[str, Any]] = field(default_factory=list)
    outcome: RunOutcome = field(default_factory=lambda: RunOutcome("failure"))
    export_outcome: str | None = None
    exit_code: int | None = None


def parse_vibe_export(
    text: str, *, stdout_text: str = "", stderr_text: str = ""
) -> ParsedVibeRun:
    """Parse one per-invocation ``export.json`` without inventing counters."""
    parsed = ParsedVibeRun()
    try:
        payload = json.loads(text)
    except json.JSONDecodeError as exc:
        parsed.diagnostics.append(
            {"kind": "invalid_json", "line": exc.lineno, "column": exc.colno}
        )
        return parsed
    if not isinstance(payload, Mapping):
        parsed.diagnostics.append({"kind": "non_object"})
        return parsed
    parsed.result_seen = payload.get("schema_version") is not None
    parsed.session_id = _text(payload.get("session_id"))
    parsed.turns = _integer(payload.get("steps"))
    parsed.exit_code = _integer(payload.get("exit_code"))
    parsed.export_outcome = _text(payload.get("outcome"))
    config = payload.get("config")
    if isinstance(config, Mapping):
        parsed.reported_model = _text(config.get("active_model")) or None
    for notice in _vibe_notices(payload, stderr_text):
        match = _FALLBACK_MODEL.search(notice)
        if match:
            parsed.reported_model = match.group(1)
        parsed.diagnostics.append({"kind": "client_notice", "message": notice})
    parsed.final_text, parsed.all_text = _vibe_text(payload, stdout_text)
    usage = payload.get("usage") if isinstance(payload.get("usage"), Mapping) else {}
    raw = {
        "schema_version": "vibe-export-json",
        "tokens": {
            "input": usage.get("input_tokens"),
            "output": usage.get("output_tokens"),
            "reasoning": None,
            "cache": {"read": usage.get("cached_input_tokens"), "write": None},
        },
        "cost": payload.get("cost_usd"),
    }
    parsed.usage = UsageRecord.from_raw(
        raw,
        turns=parsed.turns,
        reported_model=parsed.reported_model,
    )
    parsed.outcome = _vibe_outcome(parsed.export_outcome, parsed.exit_code)
    error = payload.get("error")
    if error not in (None, ""):
        parsed.diagnostics.append({"kind": "client_error", "message": str(error)})
        if parsed.outcome.kind == "success":
            parsed.outcome = RunOutcome("failure")
    return parsed


def require_vibe_trust(project_dir: Path, setup: Mapping[str, Any]) -> Path:
    """Apply Vibe 3.1.2's nearest persisted ancestor decision, read-only."""
    trust_path = Path(
        str(setup.get("vibe_trust_path", Path.home() / ".vibe" / "trusted_folders.toml"))
    )
    try:
        payload = tomllib.loads(trust_path.read_text(encoding="utf-8"))
    except (OSError, tomllib.TOMLDecodeError) as exc:
        raise ExecutorRefused(
            "Vibe project is not persistently trusted; trust it interactively without --trust"
        ) from exc
    trusted = payload.get("trusted", [])
    untrusted = payload.get("untrusted", [])
    if not isinstance(trusted, list) or not isinstance(untrusted, list):
        raise ExecutorRefused("invalid Vibe trusted_folders.toml")
    trusted_set = {str(Path(str(value)).expanduser().resolve()) for value in trusted}
    untrusted_set = {str(Path(str(value)).expanduser().resolve()) for value in untrusted}
    current = project_dir.expanduser().resolve()
    while True:
        value = str(current)
        if value in trusted_set:
            return current
        if value in untrusted_set:
            raise ExecutorRefused(
                f"Vibe project is blocked by nearest untrusted ancestor: {current}"
            )
        if current.parent == current:
            break
        current = current.parent
    raise ExecutorRefused(
        "Vibe project is not persistently trusted; trust it interactively without --trust"
    )


def require_vibe_project_source(project_dir: Path, model: str) -> None:
    """Verify the trusted project files that Vibe 3.1.2 will actually load."""
    try:
        config = tomllib.loads(
            (project_dir / ".vibe" / "config.toml").read_text(encoding="utf-8")
        )
        hook_config = tomllib.loads(
            (project_dir / ".vibe" / "hooks.toml").read_text(encoding="utf-8")
        )
    except (OSError, tomllib.TOMLDecodeError) as exc:
        raise ExecutorRefused("Vibe trusted project config is missing or invalid") from exc
    if config.get("active_model") != model:
        raise ExecutorRefused(
            "Vibe trusted project config does not isolate the selected model"
        )
    hooks = hook_config.get("hooks")
    strict = (
        [
            hook
            for hook in hooks
            if isinstance(hook, Mapping)
            and hook.get("type") == "pre_tool"
            and hook.get("strict") is True
        ]
        if isinstance(hooks, list)
        else []
    )
    if not strict:
        raise ExecutorRefused("Vibe trusted project config lacks the required strict pre_tool hook")


@dataclass
class VibeExecutor:
    prepared: Any
    model: str
    role: str
    max_turns: int
    timeout_seconds: int = 1800
    governance: InvocationGovernance | None = None
    max_price: float | None = None
    resume_artifact: ResumeArtifact | None = None
    _memory_service_finalizer: Any = field(init=False, repr=False, default=None)

    def __post_init__(self) -> None:
        try:
            self._validate_prepared()
        except Exception:
            self._release_memory_service()
            raise
        self._memory_service_finalizer = weakref.finalize(
            self,
            VibeExecutor._release_prepared_memory_service,
            self.prepared,
        )

    def _validate_prepared(self) -> None:
        if self.role == "worker":
            raise ExecutorRefused("D4: Vibe worker denial matrix and resume gate have not passed")
        if self.role not in {"reviewer", "helper", "evaluator"}:
            raise ExecutorRefused(f"Vibe does not implement the {self.role} role")
        if not self.prepared.argv or len(self.prepared.argv) != 1:
            raise WorkerError("Vibe prepared argv must contain exactly one executable")
        if not self.model or self.max_turns < 1:
            raise WorkerError("Vibe requires an explicit model and positive turn cap")
        if FORBIDDEN_ENV & set(self.prepared.env):
            raise WorkerError("forbidden Vibe child environment")
        if self.prepared.env.get("VIBE_ENABLE_TELEMETRY") != "false":
            raise ExecutorRefused("Vibe telemetry must be disabled with VIBE_ENABLE_TELEMETRY=false")
        self._native_artifact(".vibe/config.toml")
        self._native_artifact(".vibe/hooks.toml")
        self._verify_project_artifacts()
        if self.resume_artifact is not None:
            self._accept_resume_artifact(self.resume_artifact)

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
        try:
            return self._run(
                prompt,
                cwd,
                attempt_dir,
                stem=stem,
                resume_session=resume_session,
                retries=retries,
                control=control,
            )
        finally:
            if not self._is_reviewer_attempt(stem):
                self._release_memory_service()

    def _is_reviewer_attempt(self, stem: str) -> bool:
        """Keep one service lease across the reviewer's format-repair loop."""
        return self.role == "reviewer" and (
            stem == "reviewer" or stem.startswith("reviewer_retry_")
        )

    def _release_memory_service(self) -> None:
        finalizer = self._memory_service_finalizer
        if finalizer is not None and finalizer.alive:
            finalizer()
            return
        self._release_prepared_memory_service(self.prepared)

    @staticmethod
    def _release_prepared_memory_service(prepared: Any) -> None:
        handle = getattr(prepared, "memory_service_handle", None)
        if handle is None:
            return
        from compchem_memory.harness.memory_service import release_memory_service

        release_memory_service(handle)
        object.__setattr__(prepared, "memory_service_handle", None)

    def _run(
        self,
        prompt: str,
        cwd: str | Path,
        attempt_dir: Path,
        stem: str = "worker",
        resume_session: str | None = None,
        retries: int | None = None,
        control: ProcessControl | None = None,
    ) -> WorkerResult:
        if self.resume_artifact is not None:
            if resume_session not in (None, self.resume_artifact.session_id):
                raise ExecutorRefused("Vibe resume session differs from its artifact")
            resume_session = self.resume_artifact.session_id
        elif resume_session is not None:
            raise ExecutorRefused("Vibe resume requires a bound resume artifact")
        if retries not in (None, 0):
            raise ExecutorRefused("Vibe empty retry requires live-certified same-session resume")
        attempt_dir.mkdir(parents=True, exist_ok=True)
        stdout_path = attempt_dir / f"{stem}_1.jsonl"
        stderr_path = attempt_dir / f"{stem}_1.err.log"
        output_dir = attempt_dir / f"{stem}_1.out"
        identity = self._identity(resume_session) if resume_session else None
        admission = self._admit(stem, identity, control)
        if isinstance(admission, QuotaWaiting):
            outcome, stopped_reason = self._waiting_outcome(admission, control)
            return self._empty_result(outcome, stopped_reason, identity)
        effective_cap = min(
            self.max_turns,
            admission.effective_cap
            if admission is not None and admission.effective_cap is not None
            else self.max_turns,
        )
        try:
            with admission or _NoAdmission():
                argv = self._argv(prompt, output_dir, effective_cap, resume_session)
                process = run_process(
                    argv,
                    cwd,
                    self.prepared.env,
                    self.timeout_seconds,
                    stdout_path,
                    stderr_path,
                    self._control(control, admission),
                )
                if admission is not None and not process.client_started:
                    admission._cancel_before_client_exec()
                export_path = output_dir / "export.json"
                try:
                    export_text = export_path.read_text(encoding="utf-8", errors="replace")
                except OSError:
                    export_text = ""
                stdout_text = stdout_path.read_text(encoding="utf-8", errors="replace")
                stderr_text = stderr_path.read_text(encoding="utf-8", errors="replace")
                parsed = parse_vibe_export(
                    export_text, stdout_text=stdout_text, stderr_text=stderr_text
                )
                outcome = parsed.outcome
                if process.outcome is not None and process.outcome.kind in {"quota", "timeout", "cancelled"}:
                    outcome = process.outcome
                elif parsed.reported_model is None:
                    parsed.diagnostics.append({
                        "kind": "model_unverified",
                        "selected": self.model,
                    })
                    outcome = RunOutcome("failure")
                elif parsed.reported_model != self.model:
                    parsed.diagnostics.append(
                        {
                            "kind": "model_mismatch",
                            "selected": self.model,
                            "reported": parsed.reported_model,
                        }
                    )
                    outcome = RunOutcome("model_mismatch")
                elif process.returncode != 0 and outcome.kind == "success":
                    outcome = RunOutcome("failure")
                if parsed.session_id:
                    if resume_session and parsed.session_id != resume_session:
                        parsed.diagnostics.append(
                            {
                                "kind": "resume_identity_mismatch",
                                "expected": resume_session,
                                "observed": parsed.session_id,
                            }
                        )
                        outcome = RunOutcome("refused")
                    elif not resume_session:
                        self._bind_session(parsed.session_id)
                    identity = self._identity(parsed.session_id)
                usage = self._usage(parsed.usage, identity, admission, process.duration_seconds, outcome)
                if admission is not None and process.client_started:
                    admission.release(usage=usage, outcome=outcome)
        except ProcessStartError as exc:
            if admission is not None:
                admission._cancel_before_client_exec()
            raise WorkerError(f"cannot start Vibe: {exc}") from exc
        final_path = attempt_dir / f"{stem}_final.md"
        final_path.write_text(parsed.final_text, encoding="utf-8")
        paths = [stdout_path, stderr_path, final_path]
        if export_path.exists():
            paths.append(export_path)
        redact_paths(paths)
        total_usage = _unknown_usage()
        _merge_usage(total_usage, parsed.usage)
        result_session_id = parsed.session_id or resume_session
        artifact_session_id = (
            resume_session
            if resume_session is not None and outcome.kind == "refused"
            else result_session_id
        )
        return WorkerResult(
            process.returncode,
            process.timed_out,
            process.duration_seconds,
            result_session_id,
            parsed.final_text,
            parsed.all_text,
            0,
            total_usage,
            1 if process.client_started else 0,
            [stdout_path],
            [stderr_path],
            process.stopped_reason,
            parsed.diagnostics,
            outcome,
            identity,
            self._resume_artifact(artifact_session_id),
        )

    def _argv(
        self,
        prompt: str,
        output_dir: Path,
        cap: int,
        resume_session: str | None = None,
    ) -> list[str]:
        argv = [
            self.prepared.argv[0],
            "-p",
            prompt,
            "--output",
            "json",
            "--workdir",
            str(self.prepared.discovery_dir),
            "--max-turns",
            str(cap),
            "--output-dir",
            str(output_dir),
            "--auto-approve",
        ]
        if resume_session:
            self._require_bound_session(resume_session)
            argv.extend(["--resume", resume_session])
        for tool in READ_ONLY_DISABLED_TOOLS:
            argv.extend(["--disabled-tools", tool])
        if self.max_price is not None:
            argv.extend(["--max-price", str(self.max_price)])
        return argv

    def _native_artifact(self, relative: str) -> Path:
        path = self.prepared.invocation_dir / relative
        expected = self.prepared.files.get(path)
        try:
            mode = path.lstat().st_mode
            body = path.read_bytes()
            payload = tomllib.loads(body.decode())
        except (OSError, tomllib.TOMLDecodeError, UnicodeDecodeError) as exc:
            raise ExecutorRefused(f"missing or invalid Vibe-native launch artifact: {relative}") from exc
        if expected is None or body != expected or not stat.S_ISREG(mode) or mode & 0o222:
            raise ExecutorRefused(f"invalid Vibe-native launch artifact: {relative}")
        if relative.endswith("hooks.toml"):
            hooks = payload.get("hooks")
            strict = [hook for hook in hooks if isinstance(hook, Mapping) and hook.get("type") == "pre_tool" and hook.get("strict") is True] if isinstance(hooks, list) else []
            if not strict:
                raise ExecutorRefused("Vibe required strict pre_tool hook is absent")
        elif payload.get("active_model") != self.model:
            raise ExecutorRefused("Vibe native config does not isolate the selected model")
        return path

    def _verify_project_artifacts(self) -> None:
        """Refuse when Vibe would read bytes other than the bound identity."""
        for relative in (".vibe/config.toml", ".vibe/hooks.toml"):
            expected_path = self.prepared.invocation_dir / relative
            project_path = self.prepared.discovery_dir / relative
            expected = self.prepared.files.get(expected_path)
            try:
                actual = project_path.read_bytes()
            except OSError as exc:
                raise ExecutorRefused(
                    f"Vibe project file {relative} is missing; render the expected role config"
                ) from exc
            if expected is None or actual != expected:
                raise ExecutorRefused(
                    f"Vibe project file {relative} differs from the run-owned role identity; "
                    "synchronize the project config before launch"
                )

    def _identity(self, session_id: str) -> SessionIdentity:
        digest = self._binding_identity()["configuration_identity_digest"]
        project = self.governance.project_digest if self.governance else "ungoverned"
        return SessionIdentity("vibe", self.model, session_id, project, self.role, digest)

    def _binding_path(self) -> Path:
        return self.prepared.invocation_dir / "vibe-session-bindings.json"

    def _binding_identity(self) -> dict[str, str]:
        project = self.prepared.env.get("MAGNOLIA_PROJECT_IDENTITY")
        digest = hashlib.sha256(
            f"{self.prepared.policy_digest}\0{self.prepared.config_digest}\0{self.role}\0{self.model}".encode()
        ).hexdigest()
        return {
            "client": "vibe",
            "configuration_identity_digest": digest,
            "role": self.role,
            "project_identity": project or "",
        }

    def _load_bindings(self) -> dict[str, Any]:
        path = self._binding_path()
        if not path.exists():
            return {
                "schema_version": "magnolia-vibe-session-bindings-v1",
                "sessions": {},
            }
        try:
            mode = path.lstat().st_mode
            payload = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError) as exc:
            raise ExecutorRefused("invalid Vibe session binding state") from exc
        if (
            not stat.S_ISREG(mode)
            or not isinstance(payload, dict)
            or not isinstance(payload.get("sessions"), dict)
        ):
            raise ExecutorRefused("invalid Vibe session binding state")
        return payload

    def _bind_session(self, session_id: str) -> None:
        payload = self._load_bindings()
        expected = self._binding_identity()
        current = payload["sessions"].get(session_id)
        if current is not None and not binding_identity_matches(current, expected):
            raise ExecutorRefused("Vibe session has a different bound identity")
        if current is not None:
            return
        payload["sessions"][session_id] = expected
        temporary = self._binding_path().with_name(
            f".{self._binding_path().name}.{uuid.uuid4().hex}.tmp"
        )
        temporary.write_text(
            json.dumps(payload, sort_keys=True, separators=(",", ":")) + "\n",
            encoding="utf-8",
        )
        temporary.chmod(0o600)
        temporary.replace(self._binding_path())

    def _require_bound_session(self, session_id: str) -> None:
        current = self._load_bindings()["sessions"].get(session_id)
        if current is None:
            raise ExecutorRefused(f"unknown Vibe session: {session_id}")
        if not binding_identity_matches(current, self._binding_identity()):
            raise ExecutorRefused("Vibe session has a different bound identity")

    def _resume_artifact(self, session_id: str | None) -> ResumeArtifact | None:
        if session_id is None:
            return None
        binding = self._binding_identity()
        return ResumeArtifact(
            "vibe",
            session_id,
            binding["configuration_identity_digest"],
            binding["role"],
            binding["project_identity"],
        )

    def _accept_resume_artifact(self, artifact: ResumeArtifact) -> None:
        if artifact != self._resume_artifact(artifact.session_id):
            raise ExecutorRefused(
                "Vibe resume artifact does not match client, role, policy, or project"
            )
        self._require_bound_session(artifact.session_id)

    def _admit(self, stem: str, identity: SessionIdentity | None, control: ProcessControl | None) -> Admission | QuotaWaiting | None:
        if self.governance is None:
            return None
        governance = self.governance
        request = AdmissionRequest(
            governance.route,
            f"{governance.parent_run_id}:{stem}:1:{uuid.uuid4().hex}",
            governance.parent_run_id, governance.project_digest, self.role,
            governance.role_cap or self.max_turns, identity,
        )
        while True:
            admitted = governance.governor.admit(request, control)
            if isinstance(admitted, Admission) or admitted.reason in {
                "quota", "daily_limit", "cancelled"
            }:
                return admitted
            if not governance.governor.wait(admitted, control):
                return QuotaWaiting(admitted.key, admitted.caller_id, "cancelled")

    def _control(self, control: ProcessControl | None, admission: Admission | None) -> ProcessControl:
        def on_start(pid: int, pgid: int) -> None:
            if admission is not None:
                admission.mark_started(pid=pid, pgid=pgid)
            if control and control.on_start:
                control.on_start(pid, pgid)
        return ProcessControl(
            should_stop=control.should_stop if control else None,
            on_start=on_start,
            on_heartbeat=control.on_heartbeat if control else None,
            observe=control.observe if control else None,
            poll_interval=control.poll_interval if control else 0.05,
            hold_before_exec=admission is not None,
        )

    def _waiting_outcome(self, waiting: QuotaWaiting, control: ProcessControl | None) -> tuple[RunOutcome, str]:
        assert self.governance is not None
        self.governance.governor.cancel_wait(waiting)
        reason = _control_reason(control) or waiting.reason
        if waiting.reason not in {"quota", "daily_limit"}:
            return RunOutcome("cancelled"), reason
        return RunOutcome(
            "quota",
            LimitInfo(
                "daily_limit" if waiting.reason == "daily_limit" else "governor quota",
                waiting.reset_at,
                "usage governor",
                waiting.hold_id,
            ),
        ), reason

    def _usage(self, usage: UsageRecord, identity: SessionIdentity | None, admission: Admission | None, duration: float, outcome: RunOutcome) -> UsageRecord:
        usage.session_identity = identity
        usage.client = "vibe"
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

    def _empty_result(
        self,
        outcome: RunOutcome,
        stopped_reason: str,
        identity: SessionIdentity | None,
    ) -> WorkerResult:
        session_id = identity.session_id if identity is not None else None
        return WorkerResult(
            0,
            False,
            0.0,
            session_id,
            "",
            "",
            0,
            _unknown_usage(),
            0,
            [],
            [],
            stopped_reason,
            [],
            outcome,
            identity,
            self._resume_artifact(session_id),
        )


def _vibe_text(payload: Mapping[str, Any], stdout_text: str) -> tuple[str, str]:
    for key in ("final_text", "result", "response"):
        value = payload.get(key)
        if isinstance(value, str):
            return value, value
    messages: list[str] = []
    for raw in stdout_text.splitlines():
        try:
            event = json.loads(raw)
        except ValueError:
            continue
        if isinstance(event, Mapping):
            value = event.get("text") or event.get("content")
            if isinstance(value, str):
                messages.append(value)
    return (messages[-1] if messages else "", "\n".join(messages))


def _vibe_notices(payload: Mapping[str, Any], stderr_text: str) -> list[str]:
    """Collect real RunExport warnings and process stderr notices."""
    notices: list[str] = []
    warnings = payload.get("warnings")
    if isinstance(warnings, list):
        for warning in warnings:
            message = warning.get("message") if isinstance(warning, Mapping) else None
            if isinstance(message, str) and message:
                notices.append(message)
    notices.extend(line for line in stderr_text.splitlines() if line)
    return notices


def _vibe_outcome(value: str | None, exit_code: int | None) -> RunOutcome:
    if value == "finished" and exit_code in (None, 0):
        return RunOutcome("success")
    if value in {"turn_limit", "price_limit"}:
        return RunOutcome(value)
    return RunOutcome("failure")


def _unknown_usage() -> dict[str, float | None]:
    return {"input_tokens": None, "output_tokens": None, "reasoning_tokens": None, "cache_read_tokens": None, "cache_write_tokens": None, "cost_usd": None}


def _merge_usage(total: dict[str, float | None], usage: UsageRecord) -> None:
    for key, value in {
        "input_tokens": usage.input_tokens,
        "output_tokens": usage.output_tokens,
        "reasoning_tokens": usage.reasoning_tokens,
        "cache_read_tokens": usage.cached_tokens,
        "cache_write_tokens": usage.cache_write_tokens,
        "cost_usd": usage.cost,
    }.items():
        if isinstance(value, (int, float)) and not isinstance(value, bool):
            total[key] = float(total[key] or 0) + float(value)


def _text(value: Any) -> str | None:
    return value if isinstance(value, str) else None


def _integer(value: Any) -> int | None:
    return value if isinstance(value, int) and not isinstance(value, bool) else None


class _NoAdmission:
    def __enter__(self) -> None:
        return None

    def __exit__(self, exc_type: Any, exc: Any, traceback: Any) -> bool:
        return False
