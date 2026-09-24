"""job_notices: the poller → plugin notification bridge queue.

Push must never raise; drain must consume what it returns (roundtrip),
tolerate malformed lines, and leave the absent-file steady state quiet."""
from pathlib import Path

from compchem_memory.job_notices import drain_job_notices, push_job_notice


def test_push_then_drain_roundtrip(tmp_path):
    pd = str(tmp_path / "proj")
    push_job_notice(pd, run_id="r1", tool="xtb", state="COMPLETED",
                    category="success", job_id="777")
    out = drain_job_notices(pd)
    assert len(out) == 1
    assert out[0]["run_id"] == "r1"
    assert out[0]["tool"] == "xtb"
    assert out[0]["state"] == "COMPLETED"
    assert out[0]["category"] == "success"
    assert out[0]["job_id"] == "777"
    assert "timestamp" in out[0]


def test_drain_absent_file_returns_empty(tmp_path):
    assert drain_job_notices(str(tmp_path / "nothing")) == []


def test_drain_consumes_file(tmp_path):
    pd = str(tmp_path / "proj")
    push_job_notice(pd, run_id="r1", tool="xtb", state="COMPLETED",
                    category="success")
    assert drain_job_notices(pd) != []
    assert drain_job_notices(pd) == []  # second drain: quiet
    assert not (Path(pd) / ".magnolia" / ".job-notices.jsonl").exists()


def test_drain_respects_limit_and_keeps_remainder(tmp_path):
    pd = str(tmp_path / "proj")
    for i in range(3):
        push_job_notice(pd, run_id=f"r{i}", tool="xtb", state="COMPLETED",
                        category="success")
    first = drain_job_notices(pd, limit=1)
    assert [n["run_id"] for n in first] == ["r0"]
    second = drain_job_notices(pd)
    assert [n["run_id"] for n in second] == ["r1", "r2"]


def test_drain_drops_malformed_lines(tmp_path):
    pd = Path(str(tmp_path / "proj"))
    queue = pd / ".magnolia" / ".job-notices.jsonl"
    queue.parent.mkdir(parents=True, exist_ok=True)
    queue.write_text(
        '{"run_id": "good", "tool": "xtb", "state": "COMPLETED", "category": "success"}\n'
        "not json at all\n"
        '["also", "not", "an", "object"]\n',
        encoding="utf-8",
    )
    out = drain_job_notices(str(pd))
    assert [n["run_id"] for n in out] == ["good"]
    assert drain_job_notices(str(pd)) == []


def test_push_never_raises_on_unusable_project_dir(tmp_path):
    blocker = tmp_path / "blocker"
    blocker.write_text("i am a file, not a directory\n")
    # .magnolia cannot be created under a file — push must swallow, not raise.
    push_job_notice(str(blocker), run_id="r1", tool="xtb", state="COMPLETED",
                    category="success")


def test_drain_never_raises_on_unusable_project_dir(tmp_path):
    blocker = tmp_path / "blocker"
    blocker.write_text("i am a file, not a directory\n")
    assert drain_job_notices(str(blocker)) == []
