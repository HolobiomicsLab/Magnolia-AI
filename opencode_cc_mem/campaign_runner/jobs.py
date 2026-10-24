"""Campaign job templates, persistence, and backend adapters."""

from __future__ import annotations

import hashlib
import json
import re
import shlex
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Mapping, Protocol

from .accounting import elapsed_to_core_hours
from .gates import render_token
from .models import Stage
from .state import CampaignStore


class JobError(Exception):
    """Raised when a job plan or submission is invalid."""


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
    temporary.write_text(json.dumps(value, indent=2, sort_keys=True) + "\n", encoding="utf-8")
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
        for request in requests:
            key = prefix + request.key
            existing = jobs.get(key)
            if isinstance(existing, dict) and existing.get("run_id"):
                continue
            if isinstance(existing, dict) and existing.get("status") == "submitting":
                raise JobError(f"job submission state is uncertain: {key}")
            command_hash = _canonical_hash(request)
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
            }
            jobs[key] = record
            _write_json(self.path, jobs)
            request.working_dir.mkdir(parents=True, exist_ok=True)
            try:
                result = self.backend.submit(request, self.project_dir)
            except Exception as exc:
                record["status"] = "submit_failed"
                record["error"] = str(exc)
                _write_json(self.path, jobs)
                return False, f"job {key} submission failed: {exc}"
            if not result.get("success") or not result.get("run_id"):
                record["status"] = "submit_failed"
                record["error"] = result.get("error", "submission returned no run_id")
                record["result"] = result
                _write_json(self.path, jobs)
                return False, f"job {key} submission failed: {record['error']}"
            record["status"] = "submitted"
            record["run_id"] = str(result["run_id"])
            record["result"] = result
            record["core_hours"] = 0.0
            _write_json(self.path, jobs)
        self.store.update_jobs(jobs, f"submitted jobs for {stage.id}/{attempt}")
        return True, f"submitted {len(requests)} job(s)"

    def poll(self, stage: Stage, attempt: int) -> JobStatus:
        jobs = _load_json(self.path)
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
        for key, record in records:
            if not record.get("run_id") or record.get("status") in {
                "submit_failed",
                "cancelled",
            }:
                return JobStatus(
                    terminal=True,
                    success=False,
                    category="failure",
                    detail=f"job record is not active: {key}",
                )
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
            _write_json(self.path, jobs)
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
                self.store.update_jobs(jobs, f"job failed: {key}")
                return JobStatus(
                    terminal=True,
                    success=False,
                    category=status.category,
                    state=status.state,
                    detail=status.detail,
                    core_hours=sum(float(item.get("core_hours", 0)) for _, item in records),
                )
            record["status"] = "succeeded"
            successes += 1
        if pending is not None:
            return pending
        self.store.update_jobs(jobs, f"jobs succeeded for {stage.id}/{attempt}")
        return JobStatus(
            terminal=True,
            success=True,
            category="success",
            detail=f"{successes} job(s) succeeded",
            core_hours=sum(float(item.get("core_hours", 0)) for _, item in records),
        )

    def cancel_all(self, reason: str) -> list[str]:
        jobs = _load_json(self.path)
        cancelled: list[str] = []
        for key, record in jobs.items():
            if not isinstance(record, dict):
                continue
            if record.get("status") not in {"submitted", "running"}:
                continue
            try:
                result = self.backend.cancel(record, self.project_dir)
                if result.get("success", True):
                    record["status"] = "cancelled"
                    record["cancel_result"] = result
                else:
                    record["cancel_error"] = result.get("error", "cancellation failed")
            except Exception as exc:
                record["cancel_error"] = str(exc)
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
        }

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

    def status(self, record: Mapping[str, Any], project_dir: Path) -> JobStatus:
        run_id = str(record.get("run_id", ""))
        path = _find_run_record(project_dir, run_id)
        if path is None:
            return JobStatus(terminal=False, category="pending", detail="run record not found")
        body = path.read_text(encoding="utf-8", errors="replace")
        values = _scalar_values(body)
        lifecycle = values.get("lifecycle", "")
        status = values.get("status", "")
        retry_recommended = values.get("retry_recommended", "") == "true"
        terminal = lifecycle in {"fetched", "failed", "cancelled"} or status in {
            "pass",
            "fail",
            "completed",
            "failed",
            "cancelled",
        }
        ncores = _integer_value(body, "ncores", 2)
        elapsed = _nested_value(body, "slurm", "elapsed")
        core_hours = elapsed_to_core_hours(elapsed, ncores) if elapsed else 0.0
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
        success = status in {"pass", "completed", "success"} and lifecycle in {
            "fetched",
            "",
        }
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
