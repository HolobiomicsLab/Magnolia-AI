"""Deterministic campaign stage dispatcher."""

from __future__ import annotations

import json
import os
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Mapping, Protocol

from .accounting import read_session_usage
from .gates import check_outputs, render_argv, run_gate, write_receipt
from .jobs import JobController, JobStatus
from .models import Stage
from .process import run_process
from .reviewers import ReviewError, ReviewResult, build_review_prompt, choose_reviewer_model
from .state import CampaignState, CampaignStore
from .workers import WorkerResult


class RunnerError(Exception):
    """Raised when a campaign cannot continue safely."""


class WorkerExecutor(Protocol):
    def run(
        self,
        prompt: str,
        cwd: str | Path,
        attempt_dir: Path,
        stem: str = "worker",
        resume_session: str | None = None,
        retries: int | None = None,
    ) -> WorkerResult:
        ...


class ReviewExecutor(Protocol):
    def run(
        self,
        model: str,
        prompt: str,
        cwd: Path,
        attempt_dir: Path,
        run_dir: Path,
    ) -> ReviewResult:
        ...


WorkerFactory = Callable[[str, int], WorkerExecutor]


SYSTEM_PROMPT = (
    "You are a Magnolia campaign worker. Execute only the assigned stage.\n"
    "Read the brief and the provided inputs. Write only inside the work directory.\n"
    "Never edit control files, gates, budgets, or approvals.\n"
    "Do not claim scientific success beyond the measured evidence.\n"
    "Your final message must contain the complete result for this stage.\n"
)


@dataclass
class RunnerConfig:
    worker_factory: WorkerFactory
    jobs: JobController
    reviewer: ReviewExecutor | None = None
    usage_db: str | None = None
    sleep_seconds: float = 10.0


class CampaignRunner:
    def __init__(
        self,
        store: CampaignStore,
        project_dir: str | Path,
        config: RunnerConfig,
    ) -> None:
        self.store = store
        self.project_dir = Path(project_dir).resolve()
        self.config = config
        self.protocol = store.verify_protocol()

    def run_once(self) -> dict[str, Any]:
        state = self.store.load()
        actions: list[str] = []
        for _ in range(200):
            if state.state in {"completed", "failed", "halted", "paused", "waiting_human"}:
                break
            budget_reason = self._budget_exceeded(state)
            if budget_reason:
                self._cancel_active_jobs(budget_reason)
                state = self.store.halt(budget_reason)
                actions.append(f"halted: {budget_reason}")
                break
            if self._stop_file().exists():
                self._cancel_active_jobs("operator stop file present")
                state = self.store.halt("operator stop file present")
                actions.append("halted: operator stop file")
                break
            if self._pause_file().exists():
                if state.state in {"queued", "waiting_job", "validating"}:
                    state = self.store.pause("operator pause file present")
                    actions.append("paused")
                break
            if state.state == "queued":
                state = self._begin_stage(state)
                actions.append(f"stage:{state.stage}:{state.state}")
                continue
            if state.state == "running":
                state = self.store.transition(
                    "waiting_human",
                    "worker was interrupted while running; inspect before resuming",
                )
                actions.append("interrupted run requires human review")
                break
            if state.state == "waiting_job":
                try:
                    outcome = self.config.jobs.poll(
                        self.protocol.stage(state.stage), state.attempt
                    )
                except Exception as exc:
                    outcome = JobStatus(
                        terminal=True,
                        success=False,
                        category="failure",
                        detail=f"job polling error: {exc}",
                    )
                self._record_job_usage(state, outcome)
                if not outcome.terminal:
                    break
                if outcome.success:
                    state = self.store.transition("validating", "jobs completed")
                    actions.append("jobs completed")
                    continue
                state = self.store.transition("validating", "jobs finished with failure")
                state = self._handle_failure(
                    state,
                    self.protocol.stage(state.stage),
                    state.attempt,
                    outcome.category,
                    outcome.detail,
                )
                actions.append(f"job failure: {outcome.category}")
                continue
            if state.state == "validating":
                state = self._validate_stage(
                    self.protocol.stage(state.stage), state.attempt
                )
                actions.append(f"validated:{state.stage}:{state.state}")
                continue
            raise RunnerError(f"unhandled campaign state: {state.state}")
        return {"state": state.to_dict(), "actions": actions}

    def run_forever(
        self,
        interval_seconds: float | None = None,
        max_iterations: int | None = None,
    ) -> None:
        interval = self.config.sleep_seconds if interval_seconds is None else interval_seconds
        iterations = 0
        while True:
            result = self.run_once()
            iterations += 1
            if max_iterations is not None and iterations >= max_iterations:
                return
            if result["state"]["state"] in {"completed", "failed", "halted"}:
                return
            time.sleep(interval)

    def cancel_jobs(self, reason: str = "operator halt") -> list[str]:
        return self.config.jobs.cancel_all(reason)

    def _cancel_active_jobs(self, reason: str) -> list[str]:
        try:
            return self.cancel_jobs(reason)
        except Exception:
            return []

    def _begin_stage(self, state: CampaignState) -> CampaignState:
        stage = self.protocol.stage(state.stage)
        attempt = state.attempt + 1
        state = self.store.transition("running", f"start stage {stage.id}", attempt=attempt)
        if stage.type == "human":
            self._write_human_request(stage, attempt)
            return self.store.transition(
                "waiting_human",
                f"human decision required for stage {stage.id}",
            )
        return self._execute_stage(stage, attempt)

    def _execute_stage(self, stage: Stage, attempt: int) -> CampaignState:
        attempt_dir = self._attempt_dir(stage, attempt)
        attempt_dir.mkdir(parents=True, exist_ok=True)
        if stage.type == "script":
            return self._execute_script(stage, attempt, attempt_dir)
        if stage.type == "opencode":
            return self._execute_worker(stage, attempt, attempt_dir)
        if stage.type == "job":
            try:
                success, detail = self.config.jobs.submit(stage, attempt)
            except Exception as exc:
                success, detail = False, f"job submission error: {exc}"
            if not success:
                state = self.store.transition(
                    "validating", f"job submission failed: {detail}"
                )
                return self._handle_failure(state, stage, attempt, "failure", detail)
            return self.store.transition("waiting_job", f"submitted jobs: {detail}")
        raise RunnerError(f"unknown stage type: {stage.type}")

    def _execute_script(
        self,
        stage: Stage,
        attempt: int,
        attempt_dir: Path,
    ) -> CampaignState:
        context = self._context(stage, attempt)
        if not stage.command or isinstance(stage.command, str):
            return self._fail_before_validation(
                stage, attempt, "script stage has no argv command"
            )
        argv = render_argv(stage.command, context)
        environment = dict(os.environ)
        environment.update(
            {
                "MAGNOLIA_RUN_DIR": str(self.store.run_dir),
                "MAGNOLIA_PROJECT_DIR": str(self.project_dir),
                "MAGNOLIA_STAGE": stage.id,
                "MAGNOLIA_ATTEMPT": str(attempt),
            }
        )
        timeout = stage.timeout_seconds or 3600
        try:
            outcome = run_process(
                argv,
                cwd=self.store.run_dir,
                env=environment,
                timeout_seconds=timeout,
                stdout_path=attempt_dir / "script.stdout.log",
                stderr_path=attempt_dir / "script.stderr.log",
            )
        except OSError as exc:
            return self._fail_before_validation(stage, attempt, f"cannot start script: {exc}")
        if outcome.timed_out:
            return self._fail_before_validation(
                stage, attempt, f"script timed out after {timeout}s"
            )
        if outcome.returncode != 0:
            tail = (attempt_dir / "script.stderr.log").read_text(
                encoding="utf-8", errors="replace"
            )[-2000:]
            return self._fail_before_validation(
                stage,
                attempt,
                f"script exited with code {outcome.returncode}: {tail}".strip(),
            )
        self.store.transition("validating", f"script {stage.id} completed")
        return self._validate_stage(stage, attempt)

    def _execute_worker(
        self,
        stage: Stage,
        attempt: int,
        attempt_dir: Path,
    ) -> CampaignState:
        try:
            prompt = self._worker_prompt(stage, attempt)
        except Exception as exc:
            return self._fail_before_validation(stage, attempt, f"prompt error: {exc}")
        model = stage.model or self.protocol.worker.model
        timeout = stage.timeout_seconds or self.protocol.worker.timeout_seconds
        try:
            executor = self.config.worker_factory(model, timeout)
            result = executor.run(
                prompt,
                cwd=self.store.run_dir,
                attempt_dir=attempt_dir,
                stem="worker",
            )
        except Exception as exc:
            return self._fail_before_validation(stage, attempt, f"worker failed: {exc}")
        write_receipt(
            attempt_dir / "worker_result.json",
            {
                "session_id": result.session_id,
                "returncode": result.returncode,
                "timed_out": result.timed_out,
                "attempts": result.attempts,
                "tool_calls": result.tool_calls,
                "usage": result.usage,
            },
        )
        self._record_worker_usage(result)
        if result.timed_out:
            return self._fail_before_validation(
                stage, attempt, f"worker timed out after {timeout}s"
            )
        if result.returncode != 0:
            return self._fail_before_validation(
                stage,
                attempt,
                f"worker exited with code {result.returncode}",
            )
        if not result.final_text.strip():
            return self._fail_before_validation(
                stage, attempt, "worker returned no final text"
            )
        self.store.transition("validating", f"worker {stage.id} completed")
        return self._validate_stage(stage, attempt)

    def _validate_stage(self, stage: Stage, attempt: int) -> CampaignState:
        attempt_dir = self._attempt_dir(stage, attempt)
        attempt_dir.mkdir(parents=True, exist_ok=True)
        review = None
        if stage.review and self.protocol.reviewer.enabled:
            try:
                review = self._run_review(stage, attempt, attempt_dir)
            except Exception as exc:
                return self._finish_gate_failure(
                    stage, attempt, attempt_dir, [f"review failed: {exc}"], review=None
                )
            blocker = any(item["severity"] == "blocker" for item in review.issues)
            if blocker:
                detail = "; ".join(item["claim"] for item in review.issues if item["severity"] == "blocker")
                return self._finish_gate_failure(
                    stage,
                    attempt,
                    attempt_dir,
                    [f"reviewer blocker: {detail}"],
                    review=review,
                )
        outputs, errors = check_outputs(self.store.run_dir, stage.outputs)
        if errors:
            return self._finish_gate_failure(
                stage, attempt, attempt_dir, errors, review=review, outputs=outputs
            )
        gate = run_gate(
            self.store.run_dir,
            stage.gate,
            self._context(stage, attempt),
            attempt_dir,
            timeout_seconds=stage.timeout_seconds or 300,
        )
        if not gate.passed:
            return self._finish_gate_failure(
                stage,
                attempt,
                attempt_dir,
                gate.errors,
                review=review,
                outputs=outputs,
                gate=gate,
            )
        payload = self._receipt_payload(
            stage,
            attempt,
            "passed",
            outputs,
            [],
            review,
            gate,
        )
        write_receipt(attempt_dir / "receipt.json", payload)
        next_stage = self.protocol.next_stage_id(stage.id)
        if next_stage is None:
            return self.store.transition("completed", "campaign completed")
        return self.store.transition(
            "queued",
            f"stage {stage.id} passed; next {next_stage}",
            stage=next_stage,
            attempt=0,
        )

    def _run_review(
        self,
        stage: Stage,
        attempt: int,
        attempt_dir: Path,
    ) -> ReviewResult:
        if self.config.reviewer is None:
            raise ReviewError("reviewer is not configured")
        if not self.protocol.reviewer.model:
            raise ReviewError("reviewer model is not configured")
        model = choose_reviewer_model(
            stage.model or self.protocol.worker.model,
            self.protocol.reviewer.model,
            self.protocol.reviewer.alternate_model,
        )
        artifact_paths = [
            (self.store.run_dir / item).resolve() for item in stage.outputs
        ]
        worker_report = attempt_dir / "worker_final.md"
        if not worker_report.is_file():
            worker_report = artifact_paths[-1] if artifact_paths else self.store.run_dir
        prompt = build_review_prompt(artifact_paths, worker_report, stage.id)
        result = self.config.reviewer.run(
            model,
            prompt,
            self.store.run_dir,
            attempt_dir,
            self.store.run_dir,
        )
        self._record_usage(result.usage, f"reviewer usage for {stage.id}/{attempt}")
        write_receipt(
            attempt_dir / "reviewer.json",
            {
                "model": result.model,
                "issues": result.issues,
                "followups": result.followups,
                "session_id": result.session_id,
            },
        )
        return result

    def _finish_gate_failure(
        self,
        stage: Stage,
        attempt: int,
        attempt_dir: Path,
        errors: list[str],
        review: ReviewResult | None,
        outputs: list[Any] | None = None,
        gate: Any | None = None,
    ) -> CampaignState:
        payload = self._receipt_payload(
            stage,
            attempt,
            "failed",
            outputs or [],
            errors,
            review,
            gate,
        )
        write_receipt(attempt_dir / "receipt.json", payload)
        return self._handle_failure(
            self.store.load(),
            stage,
            attempt,
            "gate",
            "; ".join(errors),
        )

    def _fail_before_validation(
        self,
        stage: Stage,
        attempt: int,
        reason: str,
    ) -> CampaignState:
        state = self.store.load()
        if state.state == "running":
            state = self.store.transition("validating", reason)
        attempt_dir = self._attempt_dir(stage, attempt)
        write_receipt(
            attempt_dir / "receipt.json",
            self._receipt_payload(stage, attempt, "failed", [], [reason], None, None),
        )
        return self._handle_failure(state, stage, attempt, "failure", reason)

    def _handle_failure(
        self,
        state: CampaignState,
        stage: Stage,
        attempt: int,
        category: str,
        reason: str,
    ) -> CampaignState:
        if state.state != "validating":
            raise RunnerError(
                f"failure handling requires validating state, got {state.state}"
            )
        max_attempts = self.protocol.budget.max_attempts_per_stage
        if category == "infra":
            if stage.retry == "infra_once" and attempt < max_attempts:
                return self.store.transition(
                    "queued",
                    f"infra retry for {stage.id}: {reason}",
                    stage=stage.id,
                    attempt=attempt,
                )
            return self.store.transition(
                "waiting_human", f"infrastructure failure needs review: {reason}"
            )
        if (
            stage.retry == "repair_once" or stage.on_gate_fail == "repair"
        ) and attempt < max_attempts:
            return self.store.transition(
                "queued",
                f"repair attempt for {stage.id}: {reason}",
                stage=stage.id,
                attempt=attempt,
            )
        if stage.on_gate_fail == "failed":
            return self.store.transition("failed", reason)
        if stage.on_gate_fail == "halt":
            return self.store.transition("halted", reason)
        return self.store.transition(
            "waiting_human", f"stage {stage.id} needs review: {reason}"
        )

    def _worker_prompt(self, stage: Stage, attempt: int) -> str:
        if not stage.prompt:
            raise RunnerError(f"stage {stage.id}: prompt is missing")
        prompt_path = (self.store.control_dir / stage.prompt).resolve()
        try:
            prompt_path.relative_to(self.store.control_dir.resolve())
        except ValueError as exc:
            raise RunnerError(f"prompt escapes control directory: {stage.prompt}") from exc
        try:
            template = prompt_path.read_text(encoding="utf-8")
        except OSError as exc:
            raise RunnerError(f"cannot read prompt {prompt_path}: {exc}") from exc
        context = self._context(stage, attempt)
        rendered = template
        for key, value in context.items():
            rendered = rendered.replace("{" + key + "}", value)
        brief_path = self.store.control_dir / "brief.md"
        return (
            SYSTEM_PROMPT
            + f"\nCampaign: {self.protocol.id}\n"
            + f"Stage: {stage.id}\n"
            + f"Attempt: {attempt}\n"
            + f"Run directory: {self.store.run_dir}\n"
            + f"Work directory: {self.store.work_dir}\n"
            + f"Project directory: {self.project_dir}\n"
            + f"Brief: {brief_path}\n"
            + f"Required outputs: {', '.join(stage.outputs) or '(none)'}\n\n"
            + "Stage instructions:\n"
            + rendered
        )

    def _context(self, stage: Stage, attempt: int) -> dict[str, str]:
        return {
            "run_dir": str(self.store.run_dir),
            "project_dir": str(self.project_dir),
            "work_dir": str(self.store.work_dir),
            "control_dir": str(self.store.control_dir),
            "stage": stage.id,
            "attempt": str(attempt),
            "inputs": "\n".join(str(self.store.run_dir / item) for item in stage.inputs),
            "outputs": "\n".join(str(self.store.run_dir / item) for item in stage.outputs),
        }

    def _attempt_dir(self, stage: Stage, attempt: int) -> Path:
        return self.store.attempts_dir / stage.id / f"{attempt:03d}"

    def _write_human_request(self, stage: Stage, attempt: int) -> None:
        request_dir = self.store.control_dir / "requests"
        request_dir.mkdir(parents=True, exist_ok=True)
        path = request_dir / f"{stage.id}.md"
        path.write_text(
            f"# Human decision required\n\n"
            f"- Campaign: {self.protocol.id}\n"
            f"- Stage: {stage.id}\n"
            f"- Attempt: {attempt}\n"
            f"- Run directory: {self.store.run_dir}\n\n"
            f"Approve with:\n\n"
            f"`magnolia-campaign approve {self.store.run_dir} --approver <name> --reason <reason>`\n",
            encoding="utf-8",
        )

    def _receipt_payload(
        self,
        stage: Stage,
        attempt: int,
        status: str,
        outputs: list[Any],
        errors: list[str],
        review: ReviewResult | None,
        gate: Any | None,
    ) -> dict[str, Any]:
        worker_result_path = self._attempt_dir(stage, attempt) / "worker_result.json"
        worker_meta: dict[str, Any] = {}
        if worker_result_path.is_file():
            try:
                worker_meta = json.loads(worker_result_path.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError):
                worker_meta = {}
        return {
            "stage": stage.id,
            "attempt": attempt,
            "status": status,
            "session_id": worker_meta.get("session_id"),
            "usage": worker_meta.get("usage"),
            "outputs": [
                {"path": item.path, "sha256": item.sha256, "bytes": item.bytes}
                for item in outputs
            ],
            "errors": errors,
            "review": (
                {
                    "model": review.model,
                    "issues": review.issues,
                    "followups": review.followups,
                    "session_id": review.session_id,
                }
                if review is not None
                else None
            ),
            "gate": (
                {
                    "passed": gate.passed,
                    "exit_code": gate.exit_code,
                    "timed_out": gate.timed_out,
                    "stdout": gate.stdout_path,
                    "stderr": gate.stderr_path,
                }
                if gate is not None
                else None
            ),
        }

    def _record_worker_usage(self, result: WorkerResult) -> None:
        usage = result.usage
        if self.config.usage_db and result.session_id:
            database_usage = read_session_usage(self.config.usage_db, result.session_id)
            if sum(
                database_usage[key] for key in ("input_tokens", "output_tokens", "reasoning_tokens")
            ) > 0:
                usage = database_usage
        self._record_usage(usage, "worker usage")

    def _record_usage(self, usage: Mapping[str, float], reason: str) -> None:
        delta = {key: float(value) for key, value in usage.items() if float(value) != 0}
        if delta:
            self.store.update_usage(delta, reason)

    def _record_job_usage(self, state: CampaignState, outcome: JobStatus) -> None:
        previous = float(state.usage.get("core_hours", 0))
        if outcome.core_hours > previous:
            self.store.update_usage(
                {"core_hours": outcome.core_hours - previous},
                "job core-hour usage",
            )

    def _budget_exceeded(self, state: CampaignState) -> str:
        usage = state.usage
        tokens = (
            float(usage.get("input_tokens", 0))
            + float(usage.get("output_tokens", 0))
            + float(usage.get("reasoning_tokens", 0))
        )
        if tokens >= self.protocol.budget.max_tokens:
            return f"token budget exceeded: {tokens:.0f} >= {self.protocol.budget.max_tokens}"
        if float(usage.get("core_hours", 0)) >= self.protocol.budget.max_core_hours:
            return (
                f"core-hour budget exceeded: {usage['core_hours']} >= "
                f"{self.protocol.budget.max_core_hours}"
            )
        try:
            created = datetime.fromisoformat(state.created_at)
        except ValueError:
            return ""
        elapsed_hours = (datetime.now(timezone.utc) - created).total_seconds() / 3600
        if elapsed_hours >= self.protocol.budget.max_wall_hours:
            return (
                f"wall-clock budget exceeded: {elapsed_hours:.2f} >= "
                f"{self.protocol.budget.max_wall_hours}"
            )
        return ""

    def _stop_file(self) -> Path:
        return self.store.run_dir / "HALT"

    def _pause_file(self) -> Path:
        return self.store.run_dir / "PAUSE"
