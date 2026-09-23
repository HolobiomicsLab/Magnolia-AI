"""Derived receipts: deterministic extraction, delta predicate, v1 degradation."""
import json

from compchem_memory.receipts import (
    extract_receipts,
    extract_receipts_from_events,
    main as receipts_main,
)


def _v2_call(**over):
    ev = {
        "event_type": "tool_call", "session_id": "s1", "project_id": "proj",
        "timestamp": "2026-09-23T00:00:00", "source": "compchem-tools",
        "tool": "submit_job", "capture_version": 2, "args_truncated": False,
        "args": {
            "command": "haddock3 config.cfg", "working_dir": "/w",
            "scheduler": "ssh-slurm", "job_name": "KILDQ_s2000",
            "ncores": 32, "memory": "64GB", "time_limit": "48:00:00",
            "tool": "haddock3", "project_dir": "/p",
        },
        "args_summary": "command=haddock3 config.cfg, ...",
    }
    ev.update(over)
    return ev


def _v2_ok(run_id="haddock3_20260601_202842", **over):
    ev = {
        "event_type": "tool_success", "tool": "submit_job",
        "capture_version": 2, "result_truncated": False,
        "result": {"success": True, "job_id": "11335803", "run_id": run_id},
        "result_summary": json.dumps(
            {"success": True, "job_id": "11335803", "run_id": run_id}
        ),
    }
    ev.update(over)
    return ev


def test_v2_full_fidelity_receipt_with_delta():
    receipts = extract_receipts_from_events(
        [_v2_call(), _v2_ok()], session_id="s1"
    )
    assert len(receipts) == 1
    r = receipts[0]
    assert r["receipt_id"] == "s1:1"
    assert r["fidelity"] == "full"
    assert r["args_complete"] is True
    assert r["outcome"]["run_id"] == "haddock3_20260601_202842"
    assert r["pinning"] == "unassessed"
    assert r["rationale"] is None and r["rationale_source"] == "none"
    # command/working_dir/project_dir are not decisions
    params = [d["parameter"] for d in r["decisions"]]
    assert "command" not in params and "working_dir" not in params
    assert "project_dir" not in json.dumps(r["args"])
    # resource args collapse into ONE decision
    by_param = {d["parameter"]: d for d in r["decisions"]}
    assert by_param["resource_selection"]["chosen"] == {
        "scheduler": "ssh-slurm", "ncores": 32,
        "memory": "64GB", "time_limit": "48:00:00",
    }
    assert by_param["resource_selection"]["default"] == {
        "scheduler": "slurm", "ncores": 4,
        "memory": "4GB", "time_limit": "24:00:00",
    }
    # non-resource deviations are individual decisions
    assert by_param["job_name"]["chosen"] == "KILDQ_s2000"
    assert by_param["job_name"]["default"] == "compchem"
    assert by_param["tool"]["chosen"] == "haddock3"


def test_all_default_args_yield_no_decisions():
    call = _v2_call(args={
        "command": "xtb mol.xyz", "working_dir": "/w",
        "scheduler": "slurm", "job_name": "compchem", "ncores": 4,
        "memory": "4GB", "time_limit": "24:00:00",
    })
    r = extract_receipts_from_events([call, _v2_ok()], session_id="s1")[0]
    assert r["decisions"] == []


def test_v1_record_degrades_gracefully():
    """Real shape from the hsc70_new corpus: unstamped v1 call, result cut
    mid-path at 200 chars — run_id must still be recovered, honestly flagged."""
    v1_call = {
        "event_type": "tool_call", "session_id": "2026-06-01_134053",
        "project_id": "hsc70_new", "timestamp": "t",
        "source": "compchem-tools", "tool": "submit_job",
        "args_summary": ("command=haddock3 config.cfg, working_dir=/w, "
                         "scheduler=ssh-slurm, job_name=KILDQ_s2000, ncores=32"),
    }
    v1_ok = {
        "event_type": "tool_success", "tool": "submit_job",
        "result_summary": ('{\n  "success": true,\n  "scheduler": "ssh-slurm",\n'
                           '  "job_id": "11335803",\n  "run_id": '
                           '"haddock3_20260601_202842",\n  "remote_run_dir": '
                           '"/workspace/t'),
    }
    r = extract_receipts_from_events(
        [v1_call, v1_ok], session_id="2026-06-01_134053"
    )[0]
    assert r["fidelity"] == "summary"
    assert r["args_complete"] is False
    assert r["args"]["ncores"] == "32"
    assert r["outcome"]["run_id"] == "haddock3_20260601_202842"
    assert r["outcome"]["job_id"] == "11335803"
    assert any("v1" in n for n in r["fidelity_notes"])
    assert any("pattern match" in n for n in r["fidelity_notes"])


def test_pairing_is_fifo_when_calls_batch_before_results():
    events = [_v2_call(), _v2_call(job_name="run_b"),
              _v2_ok(run_id="r_a"), _v2_ok(run_id="r_b")]
    receipts = extract_receipts_from_events(events, session_id="s1")
    assert [r["outcome"]["run_id"] for r in receipts] == ["r_a", "r_b"]
    assert [r["receipt_id"] for r in receipts] == ["s1:1", "s1:2"]


def test_unpaired_call_still_produces_a_receipt():
    r = extract_receipts_from_events([_v2_call()], session_id="s1")[0]
    assert r["outcome"] is None
    assert any("no tool_success" in n for n in r["fidelity_notes"])


def test_tool_error_outcome():
    err = {"event_type": "tool_error", "tool": "submit_job",
           "error": "ValueError: boom", "error_truncated": False}
    r = extract_receipts_from_events([_v2_call(), err], session_id="s1")[0]
    assert r["outcome"]["success"] is False
    assert "boom" in r["outcome"]["error"]


def test_non_allowlisted_tools_are_skipped():
    events = [
        {"event_type": "tool_call", "tool": "memory_get_context",
         "args_summary": "q"},
        {"event_type": "tool_success", "tool": "memory_get_context",
         "result_summary": "ok"},
        _v2_call(), _v2_ok(),
    ]
    receipts = extract_receipts_from_events(events, session_id="s1")
    assert len(receipts) == 1
    assert receipts[0]["tool"] == "submit_job"


def test_extract_from_file_and_cli(tmp_path, capsys):
    path = tmp_path / "s.jsonl"
    path.write_text("\n".join(json.dumps(e) for e in [
        {"event_type": "session_start", "session_id": "s9"},
        _v2_call(session_id="s9"), _v2_ok(),
    ]) + "\n")
    receipts = extract_receipts(path)
    assert len(receipts) == 1
    assert receipts[0]["receipt_id"] == "s9:1"

    assert receipts_main([str(path), "--indent", "0"]) == 0
    out = json.loads(capsys.readouterr().out)
    assert out[0]["outcome"]["run_id"] == "haddock3_20260601_202842"
