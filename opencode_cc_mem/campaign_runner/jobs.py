"""Campaign job templates, persistence, and backend adapters."""

from __future__ import annotations

import hashlib
import json
import os
import re
import shlex
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Mapping, Protocol

from .accounting import elapsed_to_core_hours
from .gates import render_token
from .models import Stage
from .prejob import PrejobError, validate_prejob
from .security import redact_structure
from .state import CampaignStore, utc_now


class JobError(Exception):
    """Raised when a job plan or submission is invalid."""


class JobIntegrityError(JobError):
    """Raised when submission identity or idempotency cannot be trusted."""


SUBMIT_KEYS = {
    "scheduler",
    "job_name",
    "ncores",
    "memory",
    "time_limit",
    "partition",
    "cluster",
    "account",
    "qos",
    "tool",
    "restart_of",
    "remote_precommand",
    "acknowledge",
    "system_tags",
}


@dataclass(frozen=True)
class JobRequest:
    key: str
    command: tuple[str, ...]
    working_dir: Path
    submit: dict[str, Any]
    stage: str
    attempt: int


@dataclass
class JobStatus:
    terminal: bool
    success: bool = False
    category: str = "pending"
    state: str = ""
    detail: str = ""
    core_hours: float = 0.0


class JobBackend(Protocol):
    def submit(self, request: JobRequest, project_dir: Path) -> dict[str, Any]:
        ...

    def poll(self, project_dir: Path) -> dict[str, Any]:
        ...

    def status(
        self, record: Mapping[str, Any], project_dir: Path
    ) -> JobStatus:
        ...

    def cancel(self, record: Mapping[str, Any], project_dir: Path) -> dict[str, Any]:
        ...


def _render(value: Any, context: Mapping[str, str]) -> Any:
    if isinstance(value, str):
        return render_token(value, context)
    if isinstance(value, list):
        return [_render(item, context) for item in value]
    if isinstance(value, dict):
        return {key: _render(item, context) for key, item in value.items()}
    return value


def _safe_relative(value: str, where: str) -> str:
    path = Path(value)
    if path.is_absolute() or ".." in path.parts:
        raise JobError(f"{where}: must be a relative path without '..'")
    return value


def load_job_plan(
    run_dir: Path,
    template_id: str,
    context: Mapping[str, str],
    stage: str,
    attempt: int,
) -> list[JobRequest]:
    if not template_id or Path(template_id).name != template_id:
        raise JobError(f"invalid job template id: {template_id}")
    path = run_dir / "control" / "jobs" / f"{template_id}.json"
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError as exc:
        raise JobError(f"job template not found: {path}") from exc
    except json.JSONDecodeError as exc:
        raise JobError(f"invalid job template JSON: {exc}") from exc
    if not isinstance(data, dict) or set(data) != {"jobs"}:
        raise JobError("job template must contain exactly one 'jobs' field")
    jobs_data = data["jobs"]
    if not isinstance(jobs_data, list) or not jobs_data:
        raise JobError("job template 'jobs' must be a non-empty array")
    requests: list[JobRequest] = []
    keys: set[str] = set()
    for index, item in enumerate(jobs_data):
        where = f"jobs[{index}]"
        if not isinstance(item, dict):
            raise JobError(f"{where} must be an object")
        allowed = {"key", "command", "working_dir", "submit"}
        extra = sorted(set(item) - allowed)
        if extra:
            raise JobError(f"{where} has unknown fields: {', '.join(extra)}")
        key = item.get("key")
        if not isinstance(key, str) or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]*", key):
            raise JobError(f"{where}.key is invalid")
        if key in keys:
            raise JobError(f"duplicate job key: {key}")
        keys.add(key)
        command_value = item.get("command")
        if not isinstance(command_value, list) or not command_value:
            raise JobError(f"{where}.command must be a non-empty argv array")
        command = tuple(
            render_token(str(token), context) for token in command_value
        )
        working_dir_value = item.get("working_dir")
        if not isinstance(working_dir_value, str):
            raise JobError(f"{where}.working_dir must be a string")
        rendered_working_dir = render_token(working_dir_value, context)
        _safe_relative(rendered_working_dir, f"{where}.working_dir")
        working_dir = (run_dir / rendered_working_dir).resolve()
        try:
            working_dir.relative_to(run_dir.resolve())
        except ValueError as exc:
            raise JobError(f"{where}.working_dir escapes run directory") from exc
        submit_value = item.get("submit", {})
        if not isinstance(submit_value, dict):
            raise JobError(f"{where}.submit must be an object")
        extra_submit = sorted(set(submit_value) - SUBMIT_KEYS)
        if extra_submit:
            raise JobError(
                f"{where}.submit has unknown fields: {', '.join(extra_submit)}"
            )
        submit = _render(submit_value, context)
        submit["system_tags"] = _campaign_tags(submit.get("system_tags"))
        scheduler = submit.get("scheduler")
        if scheduler not in {"local", "ssh-slurm"}:
            raise JobIntegrityError(
                f"{where}.submit.scheduler must be 'local' or 'ssh-slurm'"
            )
        requests.append(
            JobRequest(
                key=key,
                command=command,
                working_dir=working_dir,
                submit=submit,
                stage=stage,
                attempt=attempt,
            )
        )
    return requests


def _campaign_tags(value: Any) -> list[str]:
    if value is None:
        tags: list[Any] = []
    elif isinstance(value, str):
        tags = [value]
    elif isinstance(value, list):
        tags = list(value)
    else:
        raise JobError("submit.system_tags must be an array of strings")
    if any(not isinstance(tag, str) for tag in tags):
        raise JobError("submit.system_tags must contain only strings")
    if "campaign" not in tags:
        tags.append("campaign")
    return tags


def _canonical_hash(request: JobRequest) -> str:
    payload = {
        "command": list(request.command),
        "working_dir": str(request.working_dir),
        "submit": request.submit,
    }
    body = json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()
    return hashlib.sha256(body).hexdigest()


def _write_json(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.tmp")
    temporary.write_text(
        json.dumps(redact_structure(value), indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    temporary.replace(path)


def _load_json(path: Path) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return {}
    except json.JSONDecodeError as exc:
        raise JobError(f"invalid jobs file: {exc}") from exc
    if not isinstance(value, dict):
        raise JobError("jobs file must contain an object")
    return value


def _job_keys(stage: str, attempt: int) -> str:
    return f"{stage}/{attempt}/"


def _append_jsonl(path: Path, value: Mapping[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(redact_structure(value), sort_keys=True) + "\n")
        handle.flush()


def _parse_time(value: Any) -> datetime | None:
    if not isinstance(value, str):
        return None
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed


class JobController:
    def __init__(
        self,
        store: CampaignStore,
        project_dir: Path,
        backend: JobBackend,
        max_parallel_jobs: int = 2,
    ):
        self.store = store
        self.project_dir = project_dir
        self.backend = backend
        self.max_parallel_jobs = max_parallel_jobs
        self.last_cancel_errors: list[dict[str, Any]] = []

    @property
    def path(self) -> Path:
        return self.store.control_dir / "jobs.json"

    def _context(self, stage: Stage, attempt: int) -> dict[str, str]:
        return {
            "run_dir": str(self.store.run_dir),
            "project_dir": str(self.project_dir),
            "work_dir": str(self.store.work_dir),
            "stage": stage.id,
            "attempt": str(attempt),
        }

    def submit(
        self,
        stage: Stage,
        attempt: int,
    ) -> tuple[bool, str]:
        if not isinstance(stage.command, str):
            raise JobError(f"stage {stage.id}: job template id is missing")
        requests = load_job_plan(
            self.store.run_dir,
            stage.command,
            self._context(stage, attempt),
            stage.id,
            attempt,
        )
        if len(requests) > self.max_parallel_jobs:
            raise JobError(
                f"job template requests {len(requests)} jobs but limit is "
                f"{self.max_parallel_jobs}"
            )
        jobs = _load_json(self.path)
        prefix = _job_keys(stage.id, attempt)
        prepared: list[tuple[JobRequest, str, str]] = []
        for request in requests:
            key = prefix + request.key
            existing = jobs.get(key)
            command_hash = _canonical_hash(request)
            if isinstance(existing, dict):
                if existing.get("command_sha256") != command_hash:
                    raise JobIntegrityError(
                        f"command hash changed for {key}: "
                        f"{existing.get('command_sha256')} -> {command_hash}"
                    )
                status = existing.get("status")
                if existing.get("run_id"):
                    if status not in {"submitted", "running", "succeeded"}:
                        raise JobIntegrityError(
                            f"job record {key} has status {status!r}; "
                            "use a new attempt instead of reusing it"
                        )
                    continue
                if status in {"submitting", "submit_uncertain"}:
                    raise JobIntegrityError(
                        f"job submission state is uncertain: {key}"
                    )
                raise JobIntegrityError(
                    f"job record {key} cannot be reused from status {status!r}"
                )
            prepared.append((request, key, command_hash))
        active = sum(
            1
            for value in jobs.values()
            if isinstance(value, dict)
            and value.get("status")
            in {"submitted", "running", "submitting", "submit_uncertain"}
        )
        if active + len(prepared) > self.max_parallel_jobs:
            raise JobIntegrityError(
                f"{active} jobs are already active and {len(prepared)} new jobs "
                f"would exceed the limit of {self.max_parallel_jobs}"
            )
        for request, key, command_hash in prepared:
            record = {
                "key": key,
                "stage": stage.id,
                "attempt": attempt,
                "job_key": request.key,
                "command": list(request.command),
                "command_sha256": command_hash,
                "working_dir": str(request.working_dir),
                "submit": request.submit,
                "status": "submitting",
                "submitted_at": utc_now(),
            }
            jobs[key] = record
            _write_json(self.path, jobs)
            request.working_dir.mkdir(parents=True, exist_ok=True)
            try:
                result = self.backend.submit(request, self.project_dir)
            except Exception as exc:
                record["status"] = "submit_uncertain"
                record["error"] = str(exc)
                _write_json(self.path, jobs)
                cleanup = self._cleanup_submission_failure(
                    f"submission exception for {key}"
                )
                detail = f"job {key} submission is uncertain: {exc}"
                if cleanup:
                    detail += f"; sibling cleanup failed: {cleanup}"
                return False, detail
            if not result.get("success"):
                record["status"] = "submit_failed"
                record["error"] = result.get("error", "submission failed")
                record["result"] = result
                _write_json(self.path, jobs)
                cleanup = self._cleanup_submission_failure(
                    f"submission failure for {key}"
                )
                if cleanup:
                    return (
                        False,
                        f"job {key} submission is uncertain; sibling cleanup "
                        f"failed: {cleanup}",
                    )
                return False, f"job {key} submission failed: {record['error']}"
            if not result.get("run_id"):
                record["status"] = "submit_uncertain"
                record["error"] = "submission returned no run_id"
                record["result"] = result
                _write_json(self.path, jobs)
                cleanup = self._cleanup_submission_failure(
                    f"missing run id for {key}"
                )
                detail = f"job {key} submission is uncertain; no run id recorded"
                if cleanup:
                    detail += f"; sibling cleanup failed: {cleanup}"
                return False, detail
            record["status"] = "submitted"
            record["run_id"] = str(result["run_id"])
            record["result"] = result
            record["core_hours"] = 0.0
            _write_json(self.path, jobs)
        self.store.update_jobs(jobs, f"submitted jobs for {stage.id}/{attempt}")
        return True, f"submitted {len(requests)} job(s)"

    def _cleanup_submission_failure(self, reason: str) -> list[dict[str, Any]]:
        self.cancel_all(reason)
        return list(self.last_cancel_errors)

    def poll(
        self,
        stage: Stage,
        attempt: int,
        timeout_seconds: int | None = None,
    ) -> JobStatus:
        jobs = _load_json(self.path)
        try:
            self.backend.poll(self.project_dir)
        except Exception as exc:
            _append_jsonl(
                self.store.job_errors_path,
                {
                    "ts": utc_now(),
                    "stage": stage.id,
                    "attempt": attempt,
                    "error": f"poller failed: {exc}",
                },
            )
            return JobStatus(
                terminal=False,
                category="pending",
                detail=f"poller failed: {exc}",
            )
        prefix = _job_keys(stage.id, attempt)
        records = [
            (key, value)
            for key, value in jobs.items()
            if key.startswith(prefix) and isinstance(value, dict)
        ]
        if not records:
            return JobStatus(
                terminal=True,
                success=False,
                category="failure",
                detail="no job records for this stage attempt",
            )
        successes = 0
        pending: JobStatus | None = None
        failures: list[JobStatus] = []
        for key, record in records:
            if not record.get("run_id") or record.get("status") in {
                "submit_failed",
                "cancelled",
            }:
                record["status"] = "failed"
                failures.append(
                    JobStatus(
                        terminal=True,
                        success=False,
                        category="failure",
                        detail=f"job record is not active: {key}",
                    )
                )
                continue
            try:
                status = self.backend.status(record, self.project_dir)
            except Exception as exc:
                pending = JobStatus(
                    terminal=False,
                    category="pending",
                    detail=f"status check failed: {exc}",
                )
                continue
            record["last_state"] = status.state
            record["last_detail"] = status.detail
            if status.core_hours > float(record.get("core_hours", 0) or 0):
                record["core_hours"] = status.core_hours
            if not status.terminal:
                pending = JobStatus(
                    terminal=False,
                    category="pending",
                    state=status.state,
                    detail=status.detail,
                    core_hours=sum(float(item.get("core_hours", 0)) for _, item in records),
                )
                continue
            if not status.success:
                record["status"] = (
                    "cancelled" if status.category == "cancelled" else "failed"
                )
                failures.append(
                    JobStatus(
                        terminal=True,
                        success=False,
                        category=status.category,
                        state=status.state,
                        detail=status.detail,
                        core_hours=status.core_hours,
                    )
                )
                continue
            record["status"] = "succeeded"
            successes += 1
        core_hours = sum(float(item.get("core_hours", 0)) for _, item in records)
        _write_json(self.path, jobs)
        if (
            not failures
            and pending is not None
            and not pending.detail.startswith("status check failed")
            and timeout_seconds is not None
        ):
            active_records = [
                record
                for _, record in records
                if record.get("status") in {"submitted", "running", "submitting"}
            ]
            submitted_times = [
                _parse_time(record.get("submitted_at")) for record in active_records
            ]
            valid_times = [value for value in submitted_times if value is not None]
            if valid_times:
                oldest = min(valid_times)
                elapsed = (datetime.now(timezone.utc) - oldest).total_seconds()
                if elapsed > timeout_seconds:
                    self.cancel_all(f"job stage timeout after {timeout_seconds}s")
                    jobs = _load_json(self.path)
                    category = "infra"
                    detail = f"job stage timed out after {timeout_seconds}s"
                    if self.last_cancel_errors:
                        category = "cleanup_failure"
                        detail += "; cancellation failed"
                    self.store.update_jobs(jobs, detail)
                    return JobStatus(
                        terminal=True,
                        success=False,
                        category=category,
                        detail=detail,
                        core_hours=core_hours,
                    )
        if failures:
            first = failures[0]
            self.cancel_all(f"job failure: {first.detail}")
            jobs = _load_json(self.path)
            detail = first.detail
            category = first.category
            if self.last_cancel_errors:
                category = "cleanup_failure"
                detail += (
                    "; sibling cleanup failed: "
                    f"{self.last_cancel_errors}"
                )
            self.store.update_jobs(jobs, f"job failed: {detail}")
            return JobStatus(
                terminal=True,
                success=False,
                category=category,
                state=first.state,
                detail=detail,
                core_hours=core_hours,
            )
        if pending is not None:
            return pending
        self.store.update_jobs(jobs, f"jobs succeeded for {stage.id}/{attempt}")
        return JobStatus(
            terminal=True,
            success=True,
            category="success",
            detail=f"{successes} job(s) succeeded",
            core_hours=core_hours,
        )

    def cancel_all(self, reason: str) -> list[str]:
        jobs = _load_json(self.path)
        cancelled: list[str] = []
        self.last_cancel_errors = []
        for key, record in jobs.items():
            if not isinstance(record, dict):
                continue
            if record.get("status") in {"submitting", "submit_uncertain"}:
                error = "job submission state is uncertain; cancel manually"
                self.last_cancel_errors.append({"job": key, "error": error})
                _append_jsonl(
                    self.store.cancel_errors_path,
                    {"ts": utc_now(), "job": key, "error": error},
                )
                cancelled.append(key)
                continue
            if record.get("status") not in {"submitted", "running"}:
                continue
            try:
                result = self.backend.cancel(record, self.project_dir)
                if result.get("success", True):
                    record["status"] = "cancelled"
                    record["cancel_result"] = result
                else:
                    error = result.get("error", "cancellation failed")
                    record["cancel_error"] = error
                    self.last_cancel_errors.append({"job": key, "error": error})
                    _append_jsonl(
                        self.store.cancel_errors_path,
                        {"ts": utc_now(), "job": key, "error": error},
                    )
            except Exception as exc:
                record["cancel_error"] = str(exc)
                self.last_cancel_errors.append({"job": key, "error": str(exc)})
                _append_jsonl(
                    self.store.cancel_errors_path,
                    {"ts": utc_now(), "job": key, "error": str(exc)},
                )
            cancelled.append(key)
        _write_json(self.path, jobs)
        if cancelled:
            self.store.update_jobs(jobs, f"cancelled jobs: {reason}")
        return cancelled


class FakeJobBackend:
    def __init__(self, statuses: dict[str, list[JobStatus]] | None = None):
        self.statuses = statuses or {}
        self.submitted: list[JobRequest] = []
        self.cancelled: list[str] = []
        self.counter = 0
        self.poll_calls = 0

    def submit(self, request: JobRequest, project_dir: Path) -> dict[str, Any]:
        self.counter += 1
        run_id = f"fake-{self.counter}"
        self.submitted.append(request)
        return {
            "success": True,
            "run_id": run_id,
            "job_id": str(1000 + self.counter),
            "scheduler": request.submit.get("scheduler", "local"),
            "working_dir": str(request.working_dir),
            "test_double": True,
        }

    def poll(self, project_dir: Path) -> dict[str, Any]:
        self.poll_calls += 1
        return {"polled": 0}

    def status(self, record: Mapping[str, Any], project_dir: Path) -> JobStatus:
        run_id = str(record["run_id"])
        queue = self.statuses.get(run_id)
        if queue:
            return queue.pop(0)
        return JobStatus(terminal=True, success=True, category="success", state="COMPLETED")

    def cancel(self, record: Mapping[str, Any], project_dir: Path) -> dict[str, Any]:
        self.cancelled.append(str(record["run_id"]))
        return {"success": True}


class CompchemJobBackend:
    def _module(self):
        try:
            from compchem_tools.tools import jobs
        except ImportError as exc:
            raise JobError(
                "compchem-tools is not importable; run magnolia-campaign with the "
                "project virtual environment"
            ) from exc
        return jobs

    def submit(self, request: JobRequest, project_dir: Path) -> dict[str, Any]:
        jobs_module = self._module()
        try:
            validate_prejob(
                request.working_dir,
                str(request.submit.get("tool") or ""),
            )
        except PrejobError as exc:
            return {"success": False, "error": f"pre-job check failed: {exc}"}
        command = shlex.join(request.command)
        result = jobs_module.submit_job(
            command=command,
            working_dir=str(request.working_dir),
            project_dir=str(project_dir),
            **request.submit,
        )
        if not isinstance(result, dict):
            return {"success": False, "error": "submit_job returned a non-object"}
        return result

    def poll(self, project_dir: Path) -> dict[str, Any]:
        try:
            from compchem_tools.tools import poller
        except ImportError as exc:
            raise JobError(
                "compchem-tools poller is not importable; run magnolia-campaign with "
                "the project virtual environment"
            ) from exc
        previous_auto_retry = os.environ.get("MAGNOLIA_AUTO_RETRY")
        os.environ["MAGNOLIA_AUTO_RETRY"] = "0"
        try:
            result = poller.poll_jobs(str(project_dir))
        finally:
            if previous_auto_retry is None:
                os.environ.pop("MAGNOLIA_AUTO_RETRY", None)
            else:
                os.environ["MAGNOLIA_AUTO_RETRY"] = previous_auto_retry
        if not isinstance(result, dict):
            raise JobError("poll_jobs returned a non-object")
        errors = result.get("errors", 0)
        if errors:
            raise JobError(f"poll_jobs reported {errors} error(s): {result}")
        return result

    def status(self, record: Mapping[str, Any], project_dir: Path) -> JobStatus:
        run_id = str(record.get("run_id", ""))
        path = _find_run_record(project_dir, run_id)
        if path is None:
            return JobStatus(terminal=False, category="pending", detail="run record not found")
        body = path.read_text(encoding="utf-8", errors="replace")
        values = _scalar_values(body)
        lifecycle = values.get("lifecycle", "")
        status = values.get("status", "")
        retry_recommended = (
            values.get("retry_recommended", "") == "true"
            or _nested_value(body, "remote", "retry_recommended") == "true"
        )
        failure_block = _has_nested_key(body, "remote", "failure")
        terminal = lifecycle in {"fetched", "completed", "failed", "cancelled"} or status in {
            "pass",
            "fail",
            "completed",
            "failed",
            "cancelled",
        }
        submit = record.get("submit") if isinstance(record.get("submit"), dict) else {}
        try:
            fallback_ncores = int(submit.get("ncores") or 2)
        except (TypeError, ValueError):
            fallback_ncores = 2
        ncores = _integer_value(body, "ncores", fallback_ncores)
        elapsed = _nested_value(body, "slurm", "elapsed")
        core_hours = elapsed_to_core_hours(elapsed, ncores) if elapsed else 0.0
        if submit.get("scheduler") == "local":
            started = _parse_time(record.get("submitted_at"))
            if started is not None:
                local_hours = max(
                    0.0,
                    (datetime.now(timezone.utc) - started).total_seconds() / 3600,
                )
                core_hours = max(core_hours, local_hours * ncores)
        if not terminal:
            return JobStatus(
                terminal=False,
                state=lifecycle or status,
                detail=str(path),
                core_hours=core_hours,
            )
        if retry_recommended:
            return JobStatus(
                terminal=True,
                success=False,
                category="infra",
                state=lifecycle or status,
                detail=str(path),
                core_hours=core_hours,
            )
        if lifecycle == "cancelled" or status == "cancelled":
            return JobStatus(
                terminal=True,
                success=False,
                category="cancelled",
                state=lifecycle or status,
                detail=str(path),
                core_hours=core_hours,
            )
        failed_status = {"fail", "failed"}
        if lifecycle == "completed":
            success = status not in failed_status and not failure_block
        elif lifecycle in {"fetched", ""}:
            success = status in {"pass", "completed", "success"} and not failure_block
        else:
            success = False
        return JobStatus(
            terminal=True,
            success=success,
            category="success" if success else "science_failure",
            state=lifecycle or status,
            detail=str(path),
            core_hours=core_hours,
        )

    def cancel(self, record: Mapping[str, Any], project_dir: Path) -> dict[str, Any]:
        jobs_module = self._module()
        result = record.get("result") or {}
        submit = record.get("submit") or {}
        scheduler = submit.get("scheduler", result.get("scheduler", "slurm"))
        cluster = submit.get("cluster", result.get("cluster"))
        job_id = result.get("job_id")
        if not job_id:
            return {"success": False, "error": "no job_id recorded"}
        return jobs_module.cancel_job(
            job_id=str(job_id),
            scheduler=scheduler,
            cluster=cluster,
            project_dir=str(project_dir),
        )


def _find_run_record(project_dir: Path, run_id: str) -> Path | None:
    runs = project_dir / ".magnolia" / "runs"
    if not runs.is_dir():
        return None
    matches = sorted(runs.glob(f"*{run_id}.yaml"))
    return matches[-1] if matches else None


def _scalar_values(body: str) -> dict[str, str]:
    values: dict[str, str] = {}
    for line in body.splitlines():
        match = re.match(r"^([A-Za-z0-9_]+):\s*(.*?)\s*$", line)
        if match:
            key, value = match.groups()
            values.setdefault(key, value.strip("'\""))
    return values


def _has_nested_key(body: str, parent: str, child: str) -> bool:
    in_parent = False
    parent_indent = 0
    for line in body.splitlines():
        if not line.strip():
            continue
        indent = len(line) - len(line.lstrip())
        stripped = line.strip()
        if stripped == f"{parent}:":
            in_parent = True
            parent_indent = indent
            continue
        if in_parent:
            if indent <= parent_indent:
                break
            if stripped.startswith(f"{child}:"):
                return True
    return False


def _nested_value(body: str, parent: str, child: str) -> str:
    in_parent = False
    parent_indent = 0
    for line in body.splitlines():
        if not line.strip():
            continue
        indent = len(line) - len(line.lstrip())
        stripped = line.strip()
        if stripped == f"{parent}:":
            in_parent = True
            parent_indent = indent
            continue
        if in_parent:
            if indent <= parent_indent:
                break
            match = re.match(rf"^{re.escape(child)}:\s*(.*?)\s*$", stripped)
            if match:
                return match.group(1).strip("'\"")
    return ""


def _integer_value(body: str, key: str, default: int) -> int:
    match = re.search(rf"^\s*{re.escape(key)}:\s*(\d+)\s*$", body, re.MULTILINE)
    return int(match.group(1)) if match else default
