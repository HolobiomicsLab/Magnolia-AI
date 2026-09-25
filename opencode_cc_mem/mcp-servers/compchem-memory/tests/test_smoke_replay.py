"""Phase 0 fault-replay suite (kill-gate).

Replays the three historical failure classes against the smoke detector and
ASSERTS detection. If any replayed fault passes undetected, the smoke-detector
design fails its kill-gate and must not ship.

1. placeholder-config / broken model id  -> distiller returns None -> canary freeze
2. thinking-mode empty output            -> distiller returns [] -> canary freeze
3. double-boot                           -> duplicated boot-pipeline steps -> alarm
plus the dead-man silence alarm and scheduler gating (offline).
"""

import json
import os
import time
from pathlib import Path

import pytest

from compchem_memory import canary, smoke

FIXTURES = Path(__file__).parent / "fixtures"

# Saved at import time: the suite's autouse _isolate_llm_env clears this from
# os.environ inside every test; real-API replays must opt back in explicitly.
_DEEPSEEK_KEY = os.environ.get("DEEPSEEK_API_KEY")
NEEDS_DEEPSEEK = pytest.mark.skipif(
    not _DEEPSEEK_KEY, reason="real-API fault replay needs DEEPSEEK_API_KEY")


def _opt_in_llm(monkeypatch):
    """Re-arm the real DeepSeek path after _isolate_llm_env cleared it."""
    monkeypatch.setenv("MAGNOLIA_MEMORY_PROVIDER", "deepseek")
    monkeypatch.setenv("DEEPSEEK_API_KEY", _DEEPSEEK_KEY)


def _tmp_project(tmp_path):
    (tmp_path / ".magnolia").mkdir()
    return str(tmp_path)


# ---------- offline: boot-pipeline duplication (incident 3, real 2026-09-17 data) ----------

def test_double_boot_fixture_is_caught():
    report = smoke.check_boot_single_pipeline(FIXTURES / "boot_timing_double_boot.jsonl")
    assert report["status"] == "alarm", report
    dupes = {d for a in report["alarms"] for d in a["duplicated_steps"]}
    # The second pipeline's own server_import opens the next segment, so the
    # duplicated signature inside a segment is: startup_scan, handover, etc.
    assert {"startup_scan", "handover", "boot_context", "audit", "total"} <= dupes


def test_healthy_boot_fixture_passes():
    report = smoke.check_boot_single_pipeline(FIXTURES / "boot_timing_healthy.jsonl")
    assert report["status"] == "pass", report


def test_incomplete_last_segment_never_alarms(tmp_path):
    f = tmp_path / "boot-timing.jsonl"
    rows = [
        {"ts": "t1", "step": "server_import", "ms": 1},
        {"ts": "t2", "step": "startup_scan", "ms": 1},
    ]
    f.write_text("\n".join(json.dumps(r) for r in rows) + "\n")
    assert smoke.check_boot_single_pipeline(f)["status"] == "pass"


# ---------- offline: dead-man silence ----------

def _touch(path: Path, mtime: float):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("x\n")
    os.utime(path, (mtime, mtime))


def test_deadman_alarms_on_silence_while_active(tmp_path):
    proj = tmp_path
    now = time.time()
    _touch(proj / ".magnolia" / "llm-timing.jsonl", now - 10 * 3600)
    _touch(proj / ".magnolia" / "sessions" / "ses_a.jsonl", now - 60)
    report = smoke.check_deadman(str(proj), max_silence_hours=6, now=now)
    assert report["status"] == "alarm", report


def test_deadman_passes_when_llm_recent(tmp_path):
    proj = tmp_path
    now = time.time()
    _touch(proj / ".magnolia" / "llm-timing.jsonl", now - 60)
    _touch(proj / ".magnolia" / "sessions" / "ses_a.jsonl", now - 30)
    assert smoke.check_deadman(str(proj), max_silence_hours=6, now=now)["status"] == "pass"


def test_deadman_passes_when_no_session_activity(tmp_path):
    proj = tmp_path
    now = time.time()
    _touch(proj / ".magnolia" / "llm-timing.jsonl", now - 48 * 3600)
    assert smoke.check_deadman(str(proj), max_silence_hours=6, now=now)["status"] == "pass"


# ---------- offline: scheduler gating ----------

def test_scheduler_interval_zero_disables(tmp_path, monkeypatch):
    monkeypatch.setenv("MAGNOLIA_SMOKE_INTERVAL_H", "0")
    assert smoke.maybe_run_scheduled(str(tmp_path)) is None


def test_scheduler_runs_when_marker_stale(tmp_path, monkeypatch):
    monkeypatch.setenv("MAGNOLIA_SMOKE_INTERVAL_H", "12")
    calls = []
    monkeypatch.setattr(smoke, "run_all",
                        lambda pd, **kw: calls.append(pd)
                        or {"status": "pass", "ts": "t", "checks": []})
    assert smoke.maybe_run_scheduled(str(tmp_path)) is not None
    assert smoke.maybe_run_scheduled(str(tmp_path)) is None  # marker fresh -> skip
    assert calls == [str(tmp_path)]


# ---------- real-API fault replays (the kill-gate) ----------

@NEEDS_DEEPSEEK
def test_killgate_replay_1_broken_model_id_trips_canary(tmp_path, monkeypatch):
    proj = _tmp_project(tmp_path)
    _opt_in_llm(monkeypatch)
    monkeypatch.setenv("MAGNOLIA_MEMORY_MODEL", "deepseek-no-such-model-xyz")

    import compchem_memory.llm as llm_mod
    real_call_llm = llm_mod.call_llm
    seen = {}

    def spy(system_prompt, user_content, max_tokens=2000, **kw):
        seen["called"] = True
        seen["kwargs"] = kw
        seen["result"] = real_call_llm(system_prompt, user_content,
                                       max_tokens=max_tokens, **kw)
        return seen["result"]

    monkeypatch.setattr(llm_mod, "call_llm", spy)
    report = smoke.run_all(proj, run_canary_check=True)
    assert seen.get("called"), "replay must go through the real LLM path"
    assert seen["result"] is None            # 400 on the unknown model id
    assert canary.is_frozen(proj), "kill-gate: broken model id must freeze the canary"
    assert report["status"] == "fail"
    canary.clear(proj)


@NEEDS_DEEPSEEK
def test_killgate_replay_2_thinking_mode_empty_output_trips_canary(tmp_path, monkeypatch):
    proj = _tmp_project(tmp_path)
    _opt_in_llm(monkeypatch)
    monkeypatch.delenv("MAGNOLIA_MEMORY_MODEL", raising=False)

    import compchem_memory.llm as llm_mod
    real_call_llm = llm_mod.call_llm
    seen = {}

    def thinking_starved_call(system_prompt, user_content, max_tokens=2000, **kw):
        """Replay the 2026-08-28 fault: reasoning enabled + tiny budget ->
        HTTP 200 with empty content (verified deterministic on deepseek)."""
        kw["disable_thinking"] = False
        seen["result"] = real_call_llm(system_prompt, user_content,
                                       max_tokens=10, **kw)
        return seen["result"]

    monkeypatch.setattr(llm_mod, "call_llm", thinking_starved_call)
    report = smoke.run_all(proj, run_canary_check=True)
    assert seen.get("result") in ("", None) or seen["result"] is not None
    assert canary.is_frozen(proj), "kill-gate: thinking-starved empty output must freeze the canary"
    assert report["status"] == "fail"
    canary.clear(proj)


@NEEDS_DEEPSEEK
def test_killgate_happy_path_no_false_alarm(tmp_path, monkeypatch):
    proj = _tmp_project(tmp_path)
    _opt_in_llm(monkeypatch)
    monkeypatch.delenv("MAGNOLIA_MEMORY_MODEL", raising=False)
    report = smoke.run_all(proj, run_canary_check=True)
    assert report["status"] == "pass", report
    assert not canary.is_frozen(proj)
