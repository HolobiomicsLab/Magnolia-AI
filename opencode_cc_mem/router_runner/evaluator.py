"""The LLM Eval stage: a proposer of typed Decisions with confidence.

Two-stage admission (assessment §9.1): the deterministic gate runs first; only
then does an evaluator propose. Available evaluators:
- Jev (a System One decision model) when MAGNOLIA_EVAL_JEV_MODEL is set AND
  present in the opencode-go catalog;
- any generative LLM via llm.call_llm_json as the fallback.
If neither is available, the Router falls back to clarifying, never to guessing.

The evaluator NEVER executes: a Decision is a proposal with a confidence score,
recorded for later measurement. Low-confidence proposals become clarification
(human review), not auto-accepted work.
"""

from __future__ import annotations

import importlib.util
import hashlib
import json
import os
import shutil
import subprocess
import sys
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Mapping

import httpx

from campaign_runner.executors.capabilities import load_role_capabilities
from campaign_runner.executors.contracts import (
    ExecContext,
    LimitInfo,
    RunOutcome,
    UsageRecord,
)
from campaign_runner.executors.opencode import OpenCodeEvaluatorExecutor
from campaign_runner.executors.registry import executor_for
from campaign_runner.governor import (
    Admission,
    AdmissionRequest,
    Governor,
    QuotaWaiting,
    _process_start_token,
)
from campaign_runner.launch import resolve_python
from campaign_runner.process import ProcessControl, run_process
from campaign_runner.roles import load_role
from campaign_runner.routing import load_setup, resolve_route

# The memory server's llm.py lives in a different package tree; load it by path.
_LLM_PATH = (
    Path(__file__).resolve().parents[1] / "mcp-servers" / "compchem-memory" / "src" / "compchem_memory" / "llm.py"
)
_spec = importlib.util.spec_from_file_location("compchem_llm", _LLM_PATH)
llm = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(llm)

ACTIONS = {"accept", "attach", "stop", "ignore"}
EVALUATOR_SYSTEM_PROMPT = (
    "You are an admission evaluator. Given a user message and optional "
    "context, return ONLY a JSON object with keys: action "
    "(accept|attach|stop|ignore), project (string or null), work_item_id "
    "(string or null), confidence (0..1), reason (short). "
    "accept means create/route new work to the named project; attach "
    "means follow up an existing Work Item; stop means stop it; ignore "
    "means no action. Never invent project names not present in context."
)
_INSTALLATION_ROOT = Path(__file__).resolve().parents[1]


def resolve_evaluator_route(model: str | None = None, *, setup: dict | None = None):
    """Resolve the evaluator's actual configured route without launching it."""
    inputs = dict(load_setup(_INSTALLATION_ROOT) if setup is None else setup)
    if model:
        inputs["model"] = model
    inputs.setdefault("api_fallback_model", "opencode-go/deepseek-v4.1-flash")
    return resolve_route("evaluator", inputs)


_QUOTA_TEXT = ("rate limit", "quota", "session limit", "weekly limit", "usage limit")


@dataclass
class EvalDecision:
    action: str
    project: str | None
    confidence: float
    reason: str
    evaluator: str
    work_item_id: str | None = None


@dataclass(frozen=True)
class EvalQuotaWait:
    """Opaque evaluator continuation; user text remains in the router store."""

    waiter_id: str
    subscription_key: str
    reason: str
    reset_at: str | None
    hold_id: str | None


@dataclass(frozen=True)
class _ClientCall:
    """Typed evaluator result retained across the parse boundary."""

    text: str
    outcome: RunOutcome


def confidence_threshold() -> float:
    try:
        return float(os.environ.get("MAGNOLIA_EVAL_CONFIDENCE_THRESHOLD", "0.7"))
    except ValueError:
        return 0.7


def _catalog_has_model(model: str) -> bool:
    """Whether the opencode-go catalog exposes the configured Jev model id."""
    if not model:
        return False
    v2 = os.environ.get("MAGNOLIA_OPENCODE_V2_BIN", os.path.expanduser(
        "~/.local/share/magnolia-v2/bin/opencode"))
    db = os.path.expanduser("~/.local/share/opencode/opencode-v2.db")
    try:
        route = resolve_evaluator_route(model)
        executor = executor_for(
            route,
            "evaluator",
            ExecContext(
                route=route,
                role="evaluator",
                timeout_seconds=15,
                opencode_binary=v2,
                opencode_db=db,
            ),
        )
        assert isinstance(executor, OpenCodeEvaluatorExecutor)
        out = executor.catalog(environment=os.environ, timeout_seconds=15)
        if out.returncode != 0:
            return False
        data = json.loads(out.stdout or "{}")

        def scan(o):
            if isinstance(o, dict):
                for k, v in o.items():
                    if isinstance(v, str) and v == model:
                        return True
                    if scan(v):
                        return True
            elif isinstance(o, list):
                return any(scan(v) for v in o)
            return False

        return scan(data)
    except Exception:
        return False


def _executable_available(
    value: str, which: Callable[[str], str | None] = shutil.which
) -> bool:
    """Return whether a client path resolves through an injectable lookup."""
    path = Path(value).expanduser()
    return path.is_file() if path.is_absolute() else which(value) is not None


def _capability_accepted(
    capabilities: Mapping[str, Any], client: str, role: str
) -> bool:
    """Read one projected capability row without preparing a launch."""
    client_roles = capabilities.get(client)
    if not isinstance(client_roles, Mapping):
        return False
    evidence = client_roles.get(role, False)
    if isinstance(evidence, Mapping):
        return evidence.get("accepted") is True
    return evidence is True


class GenerativeEvaluator:
    """Dispatch subscription routes through the registry, retaining API fallback."""

    kind = "generative"
    supports_availability_checked = True

    def __init__(
        self,
        model: str | None = None,
        *,
        governor: Governor | None = None,
        governor_factory: Callable[[], Governor] | None = None,
        project_dir: str | Path | None = None,
        role_capabilities: Mapping[str, Any] | None = None,
        executable_available: Callable[[str], bool] | None = None,
    ):
        self.model = model or os.environ.get("MAGNOLIA_EVAL_MODEL")
        self.governor = governor
        self._governor_factory = governor_factory
        self.project_dir = Path(
            project_dir or os.environ.get("MAGNOLIA_PROJECT_DIR") or Path.cwd()
        ).resolve()
        self.role_capabilities = role_capabilities
        self._executable_available = executable_available or _executable_available

    def bind_project(self, project_dir: str | Path) -> None:
        """Restore the project recorded for a durable evaluator action."""
        self.project_dir = Path(project_dir).resolve()

    def available(self, route=None) -> bool:
        route = route or resolve_evaluator_route(self.model)
        if not self._uses_client(route):
            return llm.is_llm_available()
        try:
            load_role("evaluator")
            if not self._executable_available(self._binary(route.client)):
                return False
            if route.client == "opencode":
                return True
            capabilities = (
                self.role_capabilities
                if self.role_capabilities is not None
                else load_role_capabilities()
            )
            return _capability_accepted(capabilities, route.client, "evaluator")
        except (OSError, RuntimeError, ValueError):
            return False

    def route_identity(self) -> dict[str, str | None]:
        """Return the durable evaluator route binding without launch side effects."""
        route = resolve_evaluator_route(self.model)
        return {
            "client": route.client if self._uses_client(route) else "api",
            "model": route.model,
            "subscription_key": route.subscription_key,
            "role": route.role,
            "project_dir": str(self.project_dir),
        }

    def _uses_client(self, route=None) -> bool:
        """Return whether the resolved route selected a subscription client."""
        selected = route or resolve_evaluator_route(self.model)
        if selected.source == "api-fallback":
            return False
        if self.model is not None:
            return self.model.startswith(
                ("claude-code/", "codex/", "vibe/", "opencode-go/")
            )
        return selected.source == "routes.evaluator" and selected.client in {
            "claude-code", "codex", "vibe", "opencode",
        }

    def _binary(self, client: str) -> str:
        if client == "opencode":
            return os.environ.get(
                "MAGNOLIA_OPENCODE_V2_BIN",
                os.path.expanduser("~/.local/share/magnolia-v2/bin/opencode"),
            )
        variable = {
            "claude-code": "MAGNOLIA_CLAUDE_BIN",
            "codex": "MAGNOLIA_CODEX_BIN",
            "vibe": "MAGNOLIA_VIBE_BIN",
        }[client]
        return os.environ.get(variable, client.removesuffix("-code"))

    def _execution_context(self, route) -> ExecContext:
        binary = self._binary(route.client)
        root = Path(__file__).resolve().parents[1]
        project_digest = hashlib.sha256(str(self.project_dir).encode()).hexdigest()
        capabilities = (
            self.role_capabilities
            if self.role_capabilities is not None
            else {}
            if route.client == "opencode"
            else load_role_capabilities()
        )
        environment = {**os.environ, "MAGNOLIA_DOORBELL": "0"}
        if route.client != "opencode":
            environment.update({
                "MAGNOLIA_PROJECT_DIR": str(self.project_dir),
                "MAGNOLIA_ROOT": str(self.project_dir.parent),
            })
        invocation_id = uuid.uuid4().hex
        run_dir = self.project_dir / ".magnolia" / "evaluator-runs" / invocation_id
        setup = load_setup(root)
        try:
            tools_port = int(environment.get("MAGNOLIA_TOOLS_PORT", "8766"))
        except ValueError:
            tools_port = 8766
        return ExecContext(
            route=route,
            role="evaluator",
            project_dir=self.project_dir,
            run_dir=run_dir,
            control_dir=run_dir / "control",
            work_dir=run_dir / "work",
            timeout_seconds=90,
            role_cap=1,
            governor_cap=(
                self._governor().policy.max_turns_per_run
                if self._has_governor()
                else None
            ),
            opencode_binary=binary,
            opencode_db=os.path.expanduser("~/.local/share/opencode/opencode-v2.db"),
            role_capabilities=capabilities,
            installation_root=root,
            workspace_root=self.project_dir.parent,
            project_identity=project_digest,
            python_executable=resolve_python(root, environment),
            tools_port=tools_port,
            setup=setup,
            required_capabilities=(),
            invocation_id=invocation_id,
            launch_argv=(binary,),
            environment=environment,
        )

    def _executor(self):
        route = resolve_evaluator_route(self.model)
        context = self._execution_context(route)
        return route, context, executor_for(route, "evaluator", context)

    def _has_governor(self) -> bool:
        return self.governor is not None or self._governor_factory is not None

    def _governor(self) -> Governor:
        if self.governor is None:
            if self._governor_factory is None:
                raise RuntimeError("client evaluator has no usage governor")
            self.governor = self._governor_factory()
        return self.governor

    @staticmethod
    def _extract_object(text: str) -> dict | None:
        """Parse the first complete JSON object from client plain text."""
        start = text.find("{")
        if start < 0:
            return None
        depth = 0
        for i, ch in enumerate(text[start:], start):
            if ch == "{":
                depth += 1
            elif ch == "}":
                depth -= 1
                if depth == 0:
                    try:
                        return json.loads(text[start:i + 1])
                    except json.JSONDecodeError:
                        return None
        return None

    def evaluate(
        self,
        text: str,
        context: dict | None = None,
        *,
        waiter_id: str | None = None,
        availability_checked: bool = False,
    ) -> EvalDecision | EvalQuotaWait | None:
        route = resolve_evaluator_route(self.model)
        if not availability_checked and not self.available(route):
            return None
        if self._uses_client(route):
            outcome = self._call_client(text, context or {}, waiter_id, route=route)
            if isinstance(outcome, EvalQuotaWait):
                return outcome
            if not isinstance(outcome, _ClientCall) or outcome.outcome.kind != "success":
                return None
            out = self._extract_object(outcome.text)
        else:
            user = json.dumps({"message": text, "context": context or {}}, ensure_ascii=False)
            out = llm.call_llm_json(
                EVALUATOR_SYSTEM_PROMPT, user, max_tokens=400, disable_thinking=True
            )
        return self._decision(out)

    @staticmethod
    def _system_prompt() -> str:
        return EVALUATOR_SYSTEM_PROMPT

    def _call_client(
        self, text: str, context: dict, waiter_id: str | None, *, route=None
    ) -> _ClientCall | EvalQuotaWait:
        route = route or resolve_evaluator_route(self.model)
        exec_context = self._execution_context(route)
        executor = executor_for(route, "evaluator", exec_context)
        waiter = waiter_id or f"eval_{uuid.uuid4().hex}"
        system = self._system_prompt()
        prompt = system + "\n\n" + json.dumps(
            {"message": text, "context": context}, ensure_ascii=False
        )
        admission = self._admit(route, exec_context, waiter)
        if isinstance(admission, QuotaWaiting):
            return self._persist_wait(route, exec_context, waiter, admission)
        try:
            result = self._run_executor(executor, exec_context, prompt, waiter, admission)
        except Exception:
            if admission is not None:
                if admission.governor._was_launched(admission.request.invocation_id):
                    admission.release(outcome=RunOutcome("failure"))
                else:
                    admission.cancel_before_launch()
            return _ClientCall("", RunOutcome("failure"))
        outcome = self._client_outcome(result)
        if admission is not None:
            if not admission.governor._was_launched(admission.request.invocation_id):
                admission.cancel_before_launch()
                return _ClientCall(
                    str(getattr(result, "final_text", getattr(result, "stdout", "")) or ""),
                    RunOutcome("failure"),
                )
            admission.release(usage=self._usage(result), outcome=outcome)
        if outcome.kind == "quota":
            waiting = self._pending_admission(route, exec_context, waiter)
            return self._wait_result(waiter, waiting)
        return _ClientCall(
            str(getattr(result, "final_text", getattr(result, "stdout", "")) or ""),
            outcome,
        )

    @staticmethod
    def _client_outcome(result) -> RunOutcome:
        quota = GenerativeEvaluator._terminal_quota_outcome(result)
        if quota is not None:
            return quota
        outcome = getattr(result, "outcome", None)
        if isinstance(outcome, RunOutcome):
            return outcome
        return RunOutcome(
            "success" if getattr(result, "returncode", 1) == 0 else "failure"
        )

    @staticmethod
    def _terminal_quota_outcome(result) -> RunOutcome | None:
        """Recognize quota only in terminal diagnostics, never answer prose."""
        diagnostics = [str(getattr(result, "stderr", "") or "")]
        errors = getattr(result, "errors", ())
        if isinstance(errors, list):
            diagnostics.extend(json.dumps(row, sort_keys=True) for row in errors)
        stdout = str(getattr(result, "stdout", "") or "")
        for line in stdout.splitlines():
            try:
                event = json.loads(line)
            except (TypeError, json.JSONDecodeError):
                continue
            if not isinstance(event, dict):
                continue
            part = event.get("part") if isinstance(event.get("part"), dict) else {}
            if event.get("type") in {"error", "turn.failed"} or part.get("type") == "error":
                diagnostics.append(json.dumps(event, sort_keys=True))
        evidence = "\n".join(diagnostics).casefold()
        if any(marker in evidence for marker in _QUOTA_TEXT):
            return RunOutcome(
                "quota",
                LimitInfo(
                    "quota or rate limit", None, "evaluator client diagnostics",
                ),
            )
        return None

    def _admit(self, route, context: ExecContext, waiter: str) -> Admission | QuotaWaiting | None:
        if not self._has_governor():
            return None
        request = AdmissionRequest(
            route,
            f"{waiter}:run:{uuid.uuid4().hex}",
            waiter,
            context.project_identity or "router",
            "evaluator",
            1,
        )
        return self._governor().admit(request)

    def _pending_admission(self, route, context: ExecContext, waiter: str) -> QuotaWaiting:
        admitted = self._governor().admit(
            AdmissionRequest(
                route,
                waiter,
                waiter,
                context.project_identity or "router",
                "evaluator",
                1,
            )
        )
        if isinstance(admitted, Admission):
            admitted.cancel_before_launch()
            raise RuntimeError("quota hold disappeared before evaluator checkpoint")
        return admitted

    def _persist_wait(
        self, route, context: ExecContext, waiter: str, waiting: QuotaWaiting
    ) -> EvalQuotaWait:
        self._governor().cancel_wait(waiting)
        return self._wait_result(waiter, self._pending_admission(route, context, waiter))

    @staticmethod
    def _wait_result(waiter: str, waiting: QuotaWaiting) -> EvalQuotaWait:
        return EvalQuotaWait(
            waiter,
            waiting.key,
            waiting.reason,
            waiting.reset_at.isoformat() if waiting.reset_at else None,
            waiting.hold_id,
        )

    def _run_executor(
        self, executor, context: ExecContext, prompt: str, waiter: str, admission: Admission | None
    ):
        attempt_dir = (context.run_dir or self.project_dir) / waiter
        control = ProcessControl(
            on_start=(
                lambda pid, pgid: admission.mark_started(pid=pid, pgid=pgid)
                if admission is not None
                else None
            ),
            hold_before_exec=admission is not None,
        )
        if isinstance(executor, OpenCodeEvaluatorExecutor):
            if admission is None:
                return executor.run(
                    prompt,
                    environment=context.environment or os.environ,
                    timeout_seconds=context.timeout_seconds,
                )
            attempt_dir.mkdir(parents=True, exist_ok=True)
            stdout = attempt_dir / "evaluator_1.out"
            stderr = attempt_dir / "evaluator_1.err"
            process = run_process(
                executor.run_argv(prompt),
                self.project_dir,
                executor.environment_with_cap(context.environment or os.environ),
                context.timeout_seconds,
                stdout,
                stderr,
                control,
            )
            return type("EvaluatorProcessResult", (), {
                "returncode": process.returncode,
                "stdout": stdout.read_text(encoding="utf-8", errors="replace"),
                "stderr": stderr.read_text(encoding="utf-8", errors="replace"),
                "outcome": process.outcome,
                "usage": {},
                "attempts": 1 if process.client_started else 0,
                "duration_seconds": process.duration_seconds,
                "session_identity": None,
            })()
        return executor.run(
            prompt,
            cwd=self.project_dir,
            attempt_dir=attempt_dir,
            stem="evaluator",
            retries=0,
            control=control,
        )

    @staticmethod
    def _usage(result) -> UsageRecord:
        raw = getattr(result, "usage", {})
        record = UsageRecord.from_raw(raw if isinstance(raw, Mapping) else {})
        record.turns = getattr(result, "attempts", None)
        record.duration_seconds = getattr(result, "duration_seconds", None)
        record.session_identity = getattr(result, "session_identity", None)
        record.source = "evaluator"
        return record

    def quota_wait(self, key: str, waiter_id: str) -> QuotaWaiting | None:
        """Return the matching live hold without exposing pending router text."""
        return self._governor().quota_wait(key, waiter_id)

    def release_hold(self, key: str, hold_id: str | None) -> bool:
        """Explicitly release exactly the hold observed by the waiter."""
        return self._governor().resume_key(key, expected_hold=hold_id)

    def _decision(self, out: Any) -> EvalDecision | None:
        if not isinstance(out, dict):
            return None
        action = out.get("action")
        if action not in ACTIONS:
            return None
        try:
            conf = float(out.get("confidence", 0.0))
        except (TypeError, ValueError):
            conf = 0.0
        return EvalDecision(
            action=action,
            project=out.get("project"),
            confidence=conf,
            reason=str(out.get("reason", "")),
            evaluator=self.kind,
            work_item_id=out.get("work_item_id"),
        )


class JevEvaluator(GenerativeEvaluator):
    """Jev path: the OpenCode Console System One endpoint (free tier).

    Console (not Go plan): POST https://opencode.ai/inference/systemone/v1/systemone
    with model jev-1.13-free (neriousy on issue #53507). A Go-plan balance is
    NOT required. Key resolution: OPENCODE_API_KEY, else auth.json's opencode/
    opencode-go entry, else the v2 DB OAuth access token. If no key is found,
    available() is False and the caller uses the generative evaluator.
    """

    kind = "jev"
    ENDPOINT = "https://opencode.ai/inference/systemone/v1/systemone"

    def __init__(self, model: str | None = None):
        super().__init__(model or os.environ.get("MAGNOLIA_EVAL_JEV_MODEL", "jev-1.13-free"))
        self.jev_model = model

    def _api_key(self) -> str | None:
        if os.environ.get("OPENCODE_API_KEY"):
            return os.environ["OPENCODE_API_KEY"]
        try:
            auth = json.loads((Path.home() / ".local/share/opencode/auth.json").read_text())
            for name in ("opencode", "opencode-go"):
                entry = auth.get(name)
                if isinstance(entry, str) and entry:
                    return entry
                if isinstance(entry, dict):
                    return entry.get("key") or entry.get("access") or entry.get("token")
        except Exception:
            pass
        try:
            import sqlite3
            conn = sqlite3.connect(os.path.expanduser("~/.local/share/opencode/opencode-v2.db"))
            row = conn.execute(
                "select value from credential where integration_id='opencode' and active=1 limit 1"
            ).fetchone()
            if row:
                data = json.loads(row[0])
                return data.get("access")
        except Exception:
            pass
        return None

    def available(self) -> bool:
        return bool(self.jev_model) and bool(self._api_key())

    def evaluate(
        self,
        text: str,
        context: dict | None = None,
        *,
        availability_checked: bool = False,
    ) -> EvalDecision | None:
        if not availability_checked and not self.available():
            return None
        projects = (context or {}).get("projects", [])
        project_criteria = {p: p for p in projects} or None
        questions = {
            "action": {
                "type": "choice",
                "instructions": "Which action fits the user request?",
                "criteria": {
                    "accept": "Create or route new work to a project",
                    "attach": "Follow up an existing Work Item",
                    "stop": "Stop an existing Work Item",
                    "ignore": "No action"
                },
            }
        }
        if project_criteria:
            questions["project"] = {
                "type": "choice",
                "instructions": "Which project should handle this work?",
                "criteria": project_criteria,
            }
        body = {
            "model": self.jev_model,
            "state": json.dumps({"message": text, "context": context or {}}, ensure_ascii=False),
            "questions": questions,
        }
        try:
            resp = httpx.post(
                self.ENDPOINT,
                headers={"Authorization": f"Bearer {self._api_key()}",
                         "Content-Type": "application/json",
                         "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36"},
                json=body,
                timeout=30,
            )
            resp.raise_for_status()
            data = resp.json()
        except Exception:
            return None
        answers = data.get("answers") or {}
        if not isinstance(answers, dict):
            return None
        action_answer = answers.get("action") or {}
        action = action_answer.get("choice")
        if action not in ACTIONS:
            return None
        try:
            conf = float(action_answer.get("confidence", 0.0))
        except (TypeError, ValueError):
            conf = 0.0
        project_answer = answers.get("project") or {}
        project = project_answer.get("choice") if isinstance(project_answer, dict) else None
        return EvalDecision(
            action=action,
            project=project,
            confidence=conf,
            reason=action_answer.get("reason", action_answer.get("rationale", "")),
            evaluator=self.kind,
            work_item_id=action_answer.get("work_item_id"),
        )


def make_evaluator() -> GenerativeEvaluator | JevEvaluator | None:
    jev_model = os.environ.get("MAGNOLIA_EVAL_JEV_MODEL")
    if jev_model:
        return JevEvaluator(jev_model)
    return GenerativeEvaluator(governor_factory=Governor)
