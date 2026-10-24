import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from campaign_runner.gates import (  # noqa: E402
    check_outputs,
    render_argv,
    run_gate,
)


def test_check_outputs_hashes_files(tmp_path):
    (tmp_path / "work").mkdir()
    (tmp_path / "work" / "out.txt").write_text("hello")
    records, errors = check_outputs(tmp_path, ["work/out.txt"])
    assert errors == []
    assert records[0].bytes == 5
    assert len(records[0].sha256) == 64


def test_check_outputs_reports_missing_empty_and_escape(tmp_path):
    (tmp_path / "work").mkdir()
    (tmp_path / "work" / "empty.txt").write_text("")
    records, errors = check_outputs(
        tmp_path,
        ["work/missing.txt", "work/empty.txt", "../escape.txt"],
    )
    assert records == []
    assert any("missing output" in error for error in errors)
    assert any("empty output" in error for error in errors)
    assert any("escapes run directory" in error for error in errors)


def test_render_argv_replaces_context():
    rendered = render_argv(
        ["{work_dir}/run.sh", "--stage", "{stage}", "--attempt", "{attempt}"],
        {"work_dir": "/tmp/w", "stage": "build", "attempt": "2"},
    )
    assert rendered == ["/tmp/w/run.sh", "--stage", "build", "--attempt", "2"]


def test_run_gate_passes_and_fails(tmp_path):
    attempt = tmp_path / "attempt"
    passed = run_gate(
        tmp_path,
        [sys.executable, "-c", "raise SystemExit(0)"],
        {"project_dir": str(tmp_path), "stage": "test", "attempt": "1"},
        attempt,
    )
    assert passed.passed is True
    assert passed.exit_code == 0
    assert Path(passed.stdout_path).is_file()
    failed = run_gate(
        tmp_path,
        [sys.executable, "-c", "raise SystemExit(3)"],
        {"project_dir": str(tmp_path), "stage": "test", "attempt": "1"},
        attempt,
    )
    assert failed.passed is False
    assert failed.exit_code == 3
    assert any("code 3" in error for error in failed.errors)


def test_run_gate_times_out_and_kills_group(tmp_path):
    result = run_gate(
        tmp_path,
        [sys.executable, "-c", "import time; time.sleep(30)"],
        {"project_dir": str(tmp_path), "stage": "test", "attempt": "1"},
        tmp_path / "attempt",
        timeout_seconds=0.5,
    )
    assert result.passed is False
    assert result.timed_out is True
    assert any("timed out" in error for error in result.errors)
