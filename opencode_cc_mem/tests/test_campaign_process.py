import sys
import time
import json
from datetime import datetime, timedelta, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from campaign_runner.executors.contracts import RunOutcome  # noqa: E402
from campaign_runner.governor import Governor, Policy  # noqa: E402
from campaign_runner.process import ProcessControl, run_process  # noqa: E402
from campaign_runner.routing import ResolvedRoute  # noqa: E402
from campaign_runner.workers import InvocationGovernance, OpenCodeExecutor  # noqa: E402


def test_run_process_stops_on_callback(tmp_path):
    started = time.monotonic()
    counter = {"n": 0}

    def should_stop():
        counter["n"] += 1
        return "halt requested" if counter["n"] >= 2 else None

    outcome = run_process(
        [sys.executable, "-c", "import time; time.sleep(30)"],
        cwd=tmp_path,
        env={},
        timeout_seconds=30,
        stdout_path=tmp_path / "out.log",
        stderr_path=tmp_path / "err.log",
        control=ProcessControl(should_stop=should_stop, poll_interval=0.05),
    )
    assert outcome.stopped_reason == "halt requested"
    assert outcome.timed_out is False
    assert time.monotonic() - started < 5


def test_run_process_reports_stop_even_after_fast_exit(tmp_path):
    calls = {"n": 0}

    def should_stop():
        calls["n"] += 1
        return None if calls["n"] == 1 else "halt requested"

    outcome = run_process(
        ["/usr/bin/true"],
        cwd=tmp_path,
        env={},
        timeout_seconds=30,
        stdout_path=tmp_path / "out.log",
        stderr_path=tmp_path / "err.log",
        control=ProcessControl(should_stop=should_stop, poll_interval=0.01),
    )
    assert outcome.stopped_reason == "halt requested"
    assert outcome.returncode == 0


def test_run_process_reports_timeout(tmp_path):
    outcome = run_process(
        [sys.executable, "-c", "import time; time.sleep(30)"],
        cwd=tmp_path,
        env={},
        timeout_seconds=0.3,
        stdout_path=tmp_path / "out.log",
        stderr_path=tmp_path / "err.log",
    )
    assert outcome.timed_out is True
    assert outcome.stopped_reason is None
    assert outcome.outcome.kind == "timeout"


def test_run_process_quota_observation_is_typed_and_stops(tmp_path):
    observed = {"calls": 0}

    def observe(_stdout, _stderr):
        observed["calls"] += 1
        return RunOutcome("quota") if observed["calls"] >= 2 else None

    outcome = run_process(
        [sys.executable, "-c", "import time; print('partial', flush=True); time.sleep(30)"],
        cwd=tmp_path,
        env={},
        timeout_seconds=30,
        stdout_path=tmp_path / "out.log",
        stderr_path=tmp_path / "err.log",
        control=ProcessControl(observe=observe, poll_interval=0.05),
    )
    assert outcome.outcome.kind == "quota"
    assert outcome.stopped_reason == "quota or rate limit"
    assert (tmp_path / "out.log").read_text().strip() == "partial"


def test_run_process_cancel_is_typed(tmp_path):
    outcome = run_process(
        [sys.executable, "-c", "import time; time.sleep(30)"],
        cwd=tmp_path,
        env={},
        timeout_seconds=30,
        stdout_path=tmp_path / "out.log",
        stderr_path=tmp_path / "err.log",
        control=ProcessControl(should_stop=lambda: "paused", poll_interval=0.01),
    )
    assert outcome.outcome.kind == "cancelled"


def test_resume_and_repair_each_reserve_and_record(tmp_path):
    binary = tmp_path / "client"
    binary.write_text(
        """#!/usr/bin/env python3
import json, sys
resumed = '--session' in sys.argv
session = 'same-session'
print(json.dumps({'sessionID': session, 'part': {'type': 'step-start'}}))
if resumed:
    print(json.dumps({'sessionID': session, 'part': {'type': 'step-finish', 'tokens': {'input': 150, 'output': 30}}}))
    print(json.dumps({'sessionID': session, 'part': {'type': 'text', 'messageID': 'm2', 'text': 'done'}}))
else:
    print(json.dumps({'sessionID': session, 'part': {'type': 'step-finish', 'tokens': {'input': 100, 'output': 20}}}))
""",
        encoding="utf-8",
    )
    binary.chmod(0o755)
    now = {"value": datetime(2026, 11, 15, 12, tzinfo=timezone.utc)}

    def clock():
        now["value"] += timedelta(seconds=1)
        return now["value"]

    usage = Governor(
        Policy(min_seconds_between_runs=1),
        ledger_path=tmp_path / "usage-ledger.jsonl",
        clock=clock,
    )
    selected = ResolvedRoute(
        "opencode", "provider/model", "provider", "opencode:provider", "worker", "test"
    )
    executor = OpenCodeExecutor(
        str(binary),
        "provider/model",
        "campaign-worker",
        governance=InvocationGovernance(
            governor=usage,
            route=selected,
            parent_run_id="run-1",
            project_digest="project",
            role_cap=60,
        ),
    )

    result = executor.run("work", tmp_path, tmp_path / "attempt")

    rows = [json.loads(line) for line in (tmp_path / "usage-ledger.jsonl").read_text().splitlines()]
    assert result.attempts == 2
    assert result.final_text == "done"
    assert len(rows) == 2
    assert rows[0]["usage"]["input_tokens"] == 100
    assert rows[1]["usage"]["input_tokens"] == 50
