import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from campaign_runner.process import ProcessControl, run_process  # noqa: E402


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
        ["/bin/true"],
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
