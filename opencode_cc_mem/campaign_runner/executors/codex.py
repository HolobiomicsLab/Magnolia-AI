"""Governed no-tools Codex execution and JSONL parsing."""

from __future__ import annotations

import hashlib
import json
import os
import re
import stat
import tomllib
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Any, Callable, Mapping

from campaign_runner.governor import Admission, AdmissionRequest, QuotaWaiting, _control_reason
from campaign_runner.process import ProcessControl, ProcessStartError, run_process
from campaign_runner.security import redact_paths
from campaign_runner.workers import EMPTY_OUTPUT_NOTE, InvocationGovernance, WorkerError, WorkerResult

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


FORBIDDEN_ENV = frozenset({"CODEX_HOME", "CODEX_API_KEY"})
FORBIDDEN_FLAGS = frozenset({"--dangerously-bypass-hook-trust", "--full-auto"})
BARE_CONFIG_KEY = re.compile(r"[A-Za-z0-9_-]+")
PLUGIN_CONFIG_KEY = re.compile(r"[A-Za-z0-9_-]+(?:@[A-Za-z0-9_-]+)?")
QuotaDetector = Callable[[Mapping[str, Any]], RunOutcome | None]


@dataclass
class ParsedCodexRun:
    session_id: str | None = None
    final_text: str = ""
    all_text: str = ""
    item_completed: int = 0
    turns: int = 0
    result_seen: bool = False
    usage: UsageRecord = field(default_factory=UsageRecord)
    cumulative_usage: UsageRecord = field(default_factory=UsageRecord)
    diagnostics: list[dict[str, Any]] = field(default_factory=list)
    outcome: RunOutcome | None = None

    @property
    def bounded_events(self) -> int:
        return self.turns


def parse_codex_stream(
    text: str,
    *,
    baseline: UsageRecord | None = None,
    quota_detector: QuotaDetector | None = None,
) -> ParsedCodexRun:
    """Parse Codex ``exec --json`` while preserving partial agent text."""
    parsed = ParsedCodexRun()
    messages: list[str] = []
    cumulative: dict[str, float | int | None] = {
        "input_tokens": None,
        "output_tokens": None,
        "reasoning_tokens": None,
        "cached_tokens": None,
        "cache_write_tokens": None,
    }
    for line_number, raw_line in enumerate(text.splitlines(), start=1):
        if not raw_line.strip():
            continue
        try:
            event = json.loads(raw_line)
        except json.JSONDecodeError as exc:
            parsed.diagnostics.append(
                {"kind": "invalid_json", "line": line_number, "column": exc.colno}
            )
            continue
        if not isinstance(event, Mapping):
            parsed.diagnostics.append({"kind": "non_object", "line": line_number})
            continue
        event_type = event.get("type")
        if event_type == "thread.started":
            parsed.session_id = _text(event.get("thread_id")) or parsed.session_id
        elif event_type == "item.completed":
            parsed.item_completed += 1
            item = event.get("item")
            if isinstance(item, Mapping):
                item_type = item.get("type")
                if item_type == "agent_message" and isinstance(item.get("text"), str):
                    messages.append(item["text"])
                elif item_type == "error":
                    parsed.diagnostics.append(
                        {"kind": "client_item_error", "message": _text(item.get("message"))}
                    )
        elif event_type == "turn.completed":
            parsed.turns += 1
            parsed.result_seen = True
            usage = event.get("usage")
            if isinstance(usage, Mapping):
                for target, source in (
                    ("input_tokens", "input_tokens"),
                    ("output_tokens", "output_tokens"),
                    ("reasoning_tokens", "reasoning_output_tokens"),
                    ("cached_tokens", "cached_input_tokens"),
                    ("cache_write_tokens", "cache_write_input_tokens"),
                ):
                    value = _number(usage.get(source))
                    if value is not None:
                        cumulative[target] = value
        elif event_type in {"turn.failed", "error"}:
            parsed.diagnostics.append(
                {"kind": "client_error", "event": event_type, "message": _event_message(event)}
            )
        detected = quota_detector(event) if quota_detector else None
        if detected is not None:
            parsed.outcome = detected
    parsed.final_text = messages[-1] if messages else ""
    parsed.all_text = "\n".join(messages)
    raw = {
        "schema_version": "codex-exec-json",
        "tokens": {
            "input": cumulative["input_tokens"],
            "output": cumulative["output_tokens"],
            "reasoning": cumulative["reasoning_tokens"],
            "cache": {
                "read": cumulative["cached_tokens"],
                "write": cumulative["cache_write_tokens"],
            },
        },
    }
    parsed.cumulative_usage = UsageRecord.from_raw(raw, turns=parsed.turns)
    parsed.usage = UsageRecord.from_raw(raw, baseline=baseline, turns=parsed.turns)
    return parsed


def require_codex_trust(project_dir: Path, setup: Mapping[str, Any]) -> None:
    """Read normal persisted Codex trust; never start a client or modify it."""
    config_path = Path(
        str(setup.get("codex_user_config_path", Path.home() / ".codex" / "config.toml"))
    )
    rows_value = setup.get("codex_hooks_list")
    if isinstance(rows_value, (str, Path)):
        try:
            rows_value = json.loads(Path(rows_value).read_text(encoding="utf-8"))
        except (OSError, ValueError, TypeError) as exc:
            raise ExecutorRefused("cannot read persisted Codex hook trust evidence") from exc
    rows = rows_value.get("hooks") if isinstance(rows_value, Mapping) else rows_value
    try:
        config = tomllib.loads(config_path.read_text(encoding="utf-8"))
    except (OSError, tomllib.TOMLDecodeError) as exc:
        raise ExecutorRefused(
            "Codex project trust is absent; trust the project and review its hooks with /hooks"
        ) from exc
    project = str(project_dir.resolve())
    projects = config.get("projects")
    entry = projects.get(project) if isinstance(projects, Mapping) else None
    if not isinstance(entry, Mapping) or entry.get("trust_level") != "trusted":
        raise ExecutorRefused(
            "Codex project is not normally trusted; trust it and review its hooks with /hooks"
        )
    state = config.get("hooks", {}).get("state", {}) if isinstance(config.get("hooks"), Mapping) else {}
    if not isinstance(rows, list) or not rows:
        raise ExecutorRefused(
            "Codex hook trust cannot be verified; review the project hooks with /hooks and supply hooks/list evidence"
        )
    expected_source = str((project_dir.resolve() / ".codex" / "hooks.json"))
    try:
        project_hooks = json.loads(Path(expected_source).read_text(encoding="utf-8"))
    except (OSError, ValueError, TypeError) as exc:
        raise ExecutorRefused("Codex project hooks are missing or invalid") from exc
    registered = project_hooks.get("hooks") if isinstance(project_hooks, Mapping) else None
    if not isinstance(registered, Mapping):
        raise ExecutorRefused("Codex project hooks are missing or invalid")
    project_rows = [
        row for row in rows
        if isinstance(row, Mapping) and row.get("sourcePath") == expected_source
    ]
    expected_rows: dict[str, str] = {}
    for event_name, groups in registered.items():
        if not isinstance(groups, list):
            raise ExecutorRefused("Codex project hooks are missing or invalid")
        for group_index, group in enumerate(groups):
            handlers = group.get("hooks") if isinstance(group, Mapping) else None
            if not isinstance(handlers, list):
                raise ExecutorRefused("Codex project hooks are missing or invalid")
            for hook_index, _handler in enumerate(handlers):
                key = _hook_key(expected_source, event_name, group_index, hook_index)
                expected_rows[key] = event_name
    rows_by_key: dict[str, Mapping[str, Any]] = {}
    for row in project_rows:
        key = row.get("key")
        if not isinstance(key, str) or key in rows_by_key:
            raise ExecutorRefused(
                "Codex hook trust rows have missing or duplicate keys; review them with /hooks"
            )
        rows_by_key[key] = row
    if set(rows_by_key) != set(expected_rows):
        raise ExecutorRefused(
            "Codex hook trust keys do not match hooks.json; review the project hooks with /hooks"
        )
    for key, event_name in expected_rows.items():
        row = rows_by_key[key]
        current_hash = row.get("currentHash")
        stored = state.get(key) if isinstance(state, Mapping) else None
        if (
            row.get("enabled") is not True
            or row.get("trustStatus") != "trusted"
            or not isinstance(current_hash, str)
            or not current_hash
            or not isinstance(stored, Mapping)
            or stored.get("trusted_hash") != current_hash
        ):
            raise ExecutorRefused(
                "Codex hooks are stale or untrusted; review the project hooks with /hooks"
            )
        if _normalized_event(row.get("eventName")) != _normalized_event(event_name):
            raise ExecutorRefused("Codex trusted hook evidence does not match hooks.json")
    if not project_rows:
        raise ExecutorRefused("Codex hook trust evidence targets a different project")


@dataclass
class CodexExecutor:
    prepared: "PreparedLaunch"
    model: str
    role: str
    max_turns: int
    timeout_seconds: int = 1800
    governance: InvocationGovernance | None = None
    resume_permitted: bool = False
    resume_artifact: ResumeArtifact | None = None
    quota_detector: QuotaDetector | None = None
    reasoning_effort: str = "low"
    user_config_path: Path | None = None
    _disable_overrides: tuple[str, ...] = field(default=(), init=False, repr=False)

    def __post_init__(self) -> None:
        if self.role not in {"worker", "reviewer", "helper", "evaluator"}:
            raise ExecutorRefused(f"Codex does not provide the {self.role} role")
        if not self.prepared.argv or len(self.prepared.argv) != 1:
            raise WorkerError("Codex prepared argv must contain exactly one executable")
        if not self.model or self.max_turns < 1:
            raise WorkerError("Codex requires an explicit model and positive supervising cap")
        if FORBIDDEN_ENV & set(self.prepared.env):
            raise WorkerError("forbidden Codex child environment")
        if any(value in FORBIDDEN_FLAGS for value in self.prepared.argv):
            raise WorkerError("forbidden Codex launch flag")
        self._native_artifact(".codex/config.toml")
        self._native_artifact(".codex/hooks.json")
        self._verify_project_artifacts()
        self._disable_overrides = tuple(self._discovered_disable_overrides())
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
        if retries not in (None, 0, 1):
            raise WorkerError("Codex permits at most one empty-output retry")
        if self.resume_artifact is not None:
            if resume_session not in (None, self.resume_artifact.session_id):
                raise ExecutorRefused("Codex resume session differs from its artifact")
            resume_session = self.resume_artifact.session_id
        if resume_session is not None and not self.resume_permitted:
            if self.resume_artifact is None:
                raise ExecutorRefused("Codex resume requires the creating launch identity")
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
        identity = self._identity(session_id)
        for attempt_index in range(retry_limit + 1):
            if session_id:
                self._require_bound_session(session_id)
            admission = self._admit(attempt_index, stem, identity, control)
            if isinstance(admission, QuotaWaiting):
                outcome, stopped_reason = self._waiting_outcome(admission, control)
                break
            effective_cap = min(
                self.max_turns,
                admission.effective_cap
                if admission is not None and admission.effective_cap is not None
                else self.max_turns,
            )
            stdout_path = attempt_dir / f"{stem}_{attempt_index + 1}.jsonl"
            stderr_path = attempt_dir / f"{stem}_{attempt_index + 1}.err.log"
            baseline = self._usage_baseline(session_id)
            try:
                with admission or _NoAdmission():
                    argv = self._argv(current_prompt, session_id)
                    process = run_process(
                        argv,
                        cwd,
                        self.prepared.env,
                        self.timeout_seconds,
                        stdout_path,
                        stderr_path,
                        self._control(control, admission, effective_cap),
                    )
                    if admission is not None and not process.client_started:
                        admission._cancel_before_client_exec()
                    if process.client_started:
                        attempts += 1
                    parsed = parse_codex_stream(
                        stdout_path.read_text(encoding="utf-8", errors="replace"),
                        baseline=baseline,
                        quota_detector=self.quota_detector,
                    )
                    outcome = self._outcome(process, parsed)
                    if session_id and parsed.session_id and parsed.session_id != session_id:
                        errors.append({"kind": "resume_identity_mismatch"})
                        outcome = RunOutcome("refused")
                    if parsed.session_id and outcome.kind != "refused":
                        self._bind_session(parsed.session_id, parsed.cumulative_usage)
                    identity = self._identity(parsed.session_id or session_id)
                    usage = self._usage(parsed.usage, identity, admission, process.duration_seconds, outcome)
                    if admission is not None and process.client_started:
                        admission.release(usage=usage, outcome=outcome)
            except ProcessStartError as exc:
                if admission is not None:
                    admission._cancel_before_client_exec()
                raise WorkerError(f"cannot start Codex: {exc}") from exc
            stdout_paths.append(stdout_path)
            stderr_paths.append(stderr_path)
            duration += process.duration_seconds
            returncode = process.returncode
            timed_out = process.timed_out
            stopped_reason = process.stopped_reason
            session_id = parsed.session_id or session_id
            final_text = parsed.final_text
            all_text.extend([parsed.all_text] if parsed.all_text else [])
            tool_calls += parsed.item_completed
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
            returncode, timed_out, duration, session_id, final_text, "\n".join(all_text),
            tool_calls, total_usage, attempts, stdout_paths, stderr_paths,
            stopped_reason, errors, outcome, identity,
            self._resume_artifact(session_id),
        )

    def _argv(self, prompt: str, session_id: str | None) -> list[str]:
        overrides = [
            "approval_policy=\"never\"",
            "notify=[]",
            f"model_reasoning_effort={json.dumps(self.reasoning_effort)}",
        ]
        argv = [
            self.prepared.argv[0],
            "-C", str(self.prepared.discovery_dir),
            "-s", "workspace-write" if self.role == "worker" else "read-only",
            "-m", self.model,
            "exec",
        ]
        if session_id:
            argv.append("resume")
        argv.append("--json")
        for override in overrides:
            argv.extend(["-c", override])
        for override in self._disable_overrides:
            argv.extend(["-c", override])
        if self.role in {"helper", "evaluator"}:
            argv.extend(["--disable", "code_mode_host"])
        argv.extend(["--disable", "apps"])
        argv.extend(([session_id] if session_id else []) + [prompt])
        return argv

    def _discovered_disable_overrides(self) -> list[str]:
        overrides: list[str] = []
        native = tomllib.loads(
            (self.prepared.invocation_dir / ".codex" / "config.toml").read_text(
                encoding="utf-8"
            )
        )
        governed = set(native.get("mcp_servers", {}))
        paths = [
            (self.prepared.discovery_dir / ".codex" / "config.toml", True),
            (self.user_config_path or Path.home() / ".codex" / "config.toml", False),
        ]
        for path, project_config in paths:
            try:
                payload = tomllib.loads(path.read_text(encoding="utf-8"))
            except (OSError, tomllib.TOMLDecodeError) as exc:
                raise ExecutorRefused(
                    f"cannot verify Codex config used by the client: {path}"
                ) from exc
            for section in ("mcp_servers", "plugins"):
                entries = payload.get(section)
                if not isinstance(entries, Mapping):
                    continue
                for name in sorted(str(value) for value in entries):
                    if section == "mcp_servers" and name in governed:
                        if project_config:
                            continue
                        raise ExecutorRefused(
                            f"Codex user MCP server {name!r} conflicts with a governed server; "
                            "rename the user-level entry"
                        )
                    key_pattern = (
                        PLUGIN_CONFIG_KEY if section == "plugins" else BARE_CONFIG_KEY
                    )
                    if key_pattern.fullmatch(name) is None:
                        raise ExecutorRefused(
                            f"Codex {section} entry {name!r} is not a supported bare TOML key or "
                            "<bare-key>@<bare-key> plugin id; rename it to use only letters, "
                            "digits, underscores, or hyphens"
                        )
                    overrides.append(f"{section}.{name}.enabled=false")
        return overrides

    def _native_artifact(self, relative: str) -> Path:
        path = self.prepared.invocation_dir / relative
        expected = self.prepared.files.get(path)
        try:
            mode = path.lstat().st_mode
            body = path.read_bytes()
        except OSError as exc:
            raise ExecutorRefused(f"missing Codex-native launch artifact: {relative}") from exc
        if expected is None or body != expected or not stat.S_ISREG(mode) or mode & 0o222:
            raise ExecutorRefused(f"invalid Codex-native launch artifact: {relative}")
        if relative.endswith("config.toml"):
            payload = tomllib.loads(body.decode())
            servers = payload.get("mcp_servers", {})
            expected = (
                {"magnolia-read", "compchem-memory"}
                if self.role == "worker"
                else {"magnolia-read"}
                if self.role == "reviewer"
                else set()
            )
            if not isinstance(servers, Mapping) or set(servers) != expected:
                raise ExecutorRefused("Codex config has unexpected governed MCP servers")
            if "magnolia-read" in expected:
                read_server = servers["magnolia-read"]
                if (
                    not isinstance(read_server, Mapping)
                    or read_server.get("command") != self.prepared.env.get("MAGNOLIA_PYTHON")
                    or read_server.get("args")
                    != ["-m", "compchem_memory.harness.read_server"]
                    or read_server.get("env", {}).get("MAGNOLIA_HOOK_POLICY")
                    != self.prepared.env.get("MAGNOLIA_HOOK_POLICY")
                ):
                    raise ExecutorRefused("Codex magnolia-read server is not run-owned")
        else:
            payload = json.loads(body)
            hooks = payload.get("hooks") if isinstance(payload, Mapping) else None
            if not isinstance(hooks, Mapping) or "PreToolUse" not in hooks:
                raise ExecutorRefused("Codex required hook is absent")
        return path

    def _verify_project_artifacts(self) -> None:
        """Refuse when Codex would read bytes other than the bound identity."""
        for relative in (".codex/config.toml", ".codex/hooks.json"):
            expected_path = self.prepared.invocation_dir / relative
            project_path = self.prepared.discovery_dir / relative
            expected = self.prepared.files.get(expected_path)
            try:
                actual = project_path.read_bytes()
            except OSError as exc:
                raise ExecutorRefused(
                    f"Codex project file {relative} is missing; render the expected role config"
                ) from exc
            if expected is None or actual != expected:
                raise ExecutorRefused(
                    f"Codex project file {relative} differs from the run-owned role identity; "
                    "synchronize the project config before launch"
                )

    def _binding_path(self) -> Path:
        return self.prepared.invocation_dir / "codex-session-bindings.json"

    def _resume_artifact(self, session_id: str | None) -> ResumeArtifact | None:
        if session_id is None:
            return None
        binding = self._binding_identity()
        return ResumeArtifact(
            "codex",
            session_id,
            binding["configuration_identity_digest"],
            binding["role"],
            binding["project_identity"],
        )

    def _accept_resume_artifact(self, artifact: ResumeArtifact) -> None:
        if artifact != self._resume_artifact(artifact.session_id):
            raise ExecutorRefused(
                "Codex resume artifact does not match client, role, policy, or project"
            )
        current = self._load_bindings()["sessions"].get(artifact.session_id)
        if current is None:
            self._bind_session(artifact.session_id, UsageRecord())
        elif not isinstance(current, Mapping) or current.get(
            "identity"
        ) != self._binding_identity():
            raise ExecutorRefused("Codex session has a different bound identity")

    def _binding_identity(self) -> dict[str, str]:
        project = self.prepared.env.get("MAGNOLIA_PROJECT_IDENTITY")
        digest = hashlib.sha256(
            f"{self.prepared.policy_digest}\0{self.prepared.config_digest}\0{self.role}\0{self.model}".encode()
        ).hexdigest()
        return {"configuration_identity_digest": digest, "role": self.role, "project_identity": project or ""}

    def _load_bindings(self) -> dict[str, Any]:
        path = self._binding_path()
        if not path.exists():
            return {"schema_version": "magnolia-codex-session-bindings-v1", "sessions": {}}
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError) as exc:
            raise ExecutorRefused("invalid Codex session binding state") from exc
        if not isinstance(payload, dict) or not isinstance(payload.get("sessions"), dict):
            raise ExecutorRefused("invalid Codex session binding state")
        return payload

    def _bind_session(self, session_id: str, usage: UsageRecord) -> None:
        payload = self._load_bindings()
        expected = self._binding_identity()
        current = payload["sessions"].get(session_id)
        if current is not None and current.get("identity") != expected:
            raise ExecutorRefused("Codex session has a different bound identity")
        payload["sessions"][session_id] = {
            "identity": expected,
            "usage": _usage_payload(usage),
        }
        _atomic_json(self._binding_path(), payload)

    def _require_bound_session(self, session_id: str) -> None:
        current = self._load_bindings()["sessions"].get(session_id)
        if not isinstance(current, Mapping) or current.get("identity") != self._binding_identity():
            raise ExecutorRefused("unknown Codex session or different bound identity")

    def _usage_baseline(self, session_id: str | None) -> UsageRecord | None:
        if not session_id:
            return None
        current = self._load_bindings()["sessions"].get(session_id)
        raw = current.get("usage") if isinstance(current, Mapping) else None
        return UsageRecord(**raw) if isinstance(raw, Mapping) else None

    def _identity(self, session_id: str | None) -> SessionIdentity | None:
        if not session_id:
            return None
        project = self.governance.project_digest if self.governance else "ungoverned"
        return SessionIdentity("codex", self.model, session_id, project, self.role, self._binding_identity()["configuration_identity_digest"])

    def _control(self, control: ProcessControl | None, admission: Admission | None, cap: int) -> ProcessControl:
        client_pid: int | None = None
        over_cap_observations = 0

        def on_start(pid: int, pgid: int) -> None:
            nonlocal client_pid
            client_pid = pid
            if admission is not None:
                admission.mark_started(pid=pid, pgid=pgid)
            if control and control.on_start:
                control.on_start(pid, pgid)

        def observe(stdout: Path, stderr: Path) -> RunOutcome | None:
            nonlocal over_cap_observations
            parsed = parse_codex_stream(stdout.read_text(encoding="utf-8", errors="replace"), quota_detector=self.quota_detector)
            if parsed.outcome is not None:
                return parsed.outcome
            if parsed.bounded_events > cap and _pid_is_running(client_pid):
                over_cap_observations += 1
                if over_cap_observations > 1:
                    return RunOutcome("cap_reached")
            else:
                over_cap_observations = 0
            return control.observe(stdout, stderr) if control and control.observe else None

        return ProcessControl(
            should_stop=control.should_stop if control else None,
            on_start=on_start,
            on_heartbeat=control.on_heartbeat if control else None,
            observe=observe,
            poll_interval=control.poll_interval if control else 0.05,
            hold_before_exec=admission is not None,
        )

    def _outcome(self, process: Any, parsed: ParsedCodexRun) -> RunOutcome:
        if parsed.outcome is not None:
            return parsed.outcome
        if process.outcome is not None and process.outcome.kind in {
            "quota", "timeout", "cancelled"
        }:
            return process.outcome
        if (
            process.outcome is not None
            and process.outcome.kind == "cap_reached"
            and process.stopped_reason == "cap_reached"
            and process.returncode != 0
        ):
            return process.outcome
        if process.returncode != 0 or not parsed.result_seen:
            return RunOutcome("failure")
        return RunOutcome("success")

    def _admit(self, attempt_index: int, stem: str, identity: SessionIdentity | None, control: ProcessControl | None) -> Admission | QuotaWaiting | None:
        if self.governance is None:
            return None
        governance = self.governance
        request = AdmissionRequest(
            governance.route,
            f"{governance.parent_run_id}:{stem}:{attempt_index + 1}:{uuid.uuid4().hex}",
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
        usage.client = "codex"
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


def _atomic_json(path: Path, payload: Mapping[str, Any]) -> None:
    temporary = path.with_name(f".{path.name}.{uuid.uuid4().hex}.tmp")
    temporary.write_text(json.dumps(payload, sort_keys=True, separators=(",", ":")) + "\n", encoding="utf-8")
    temporary.chmod(0o600)
    temporary.replace(path)


def _usage_payload(usage: UsageRecord) -> dict[str, Any]:
    return {name: getattr(usage, name) for name in ("input_tokens", "output_tokens", "reasoning_tokens", "cached_tokens", "cache_write_tokens", "cost")}


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
        if _number(value) is not None:
            total[key] = float(total[key] or 0) + float(value)


def _event_message(event: Mapping[str, Any]) -> str | None:
    return _text(event.get("message")) or _text(event.get("error"))


def _normalized_event(value: Any) -> str:
    """Normalize native and app-server hook event spellings."""
    if not isinstance(value, str):
        return ""
    return "".join(char.lower() for char in value if char.isalnum())


def _hook_key(source: str, event_name: str, group_index: int, hook_index: int) -> str:
    """Return the persisted Codex key for one hooks.json handler."""
    snake_event = re.sub(r"(?<!^)(?=[A-Z])", "_", event_name).lower()
    return f"{source}:{snake_event}:{group_index}:{hook_index}"


def _pid_is_running(pid: int | None) -> bool:
    """Return whether the observed client can still be stopped."""
    if pid is None:
        return False
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


def _text(value: Any) -> str | None:
    return value if isinstance(value, str) else None


def _number(value: Any) -> float | int | None:
    return value if isinstance(value, (int, float)) and not isinstance(value, bool) else None


class _NoAdmission:
    def __enter__(self) -> None:
        return None

    def __exit__(self, exc_type: Any, exc: Any, traceback: Any) -> bool:
        return False
