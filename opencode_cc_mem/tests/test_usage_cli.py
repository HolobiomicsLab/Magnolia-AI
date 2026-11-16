import json
import os
from datetime import datetime, timedelta, timezone

import pytest

from campaign_runner.governor import Policy, PolicyError
from test_launcher_harness import make_workspace, run_raw, write_setup


PROJECT = "demo"


def _row(*, when, key, project, usage):
    return {
        "schema_version": "magnolia-invocation-v1",
        "invocation_id": f"{key}-{project}-{when.timestamp()}",
        "started_at": when.isoformat(),
        "ended_at": (when + timedelta(seconds=2)).isoformat(),
        "client": key.split(":", 1)[0],
        "resolved_model": "sentinel-model",
        "subscription_key": key,
        "billing": "subscription",
        "run_kind": "worker",
        "project_digest": project,
        "usage": usage,
        "outcome": {"kind": "success", "limit": None},
    }


def _usage_env(tmp_path, rows, state=None):
    state_root = tmp_path / "xdg-state"
    config_root = tmp_path / "xdg-config"
    magnolia = state_root / "magnolia"
    magnolia.mkdir(parents=True)
    (magnolia / "usage-ledger.jsonl").write_text(
        "".join(json.dumps(row) + "\n" for row in rows), encoding="utf-8"
    )
    if state is not None:
        (magnolia / "usage-state.json").write_text(json.dumps(state), encoding="utf-8")
    return {"XDG_STATE_HOME": str(state_root), "XDG_CONFIG_HOME": str(config_root)}


def test_usage_day_week_routes_cache_and_unknowns(tmp_path):
    ws = make_workspace(tmp_path)
    write_setup(ws, {
        "harness": "claude",
        "models": {"claude": "sonnet"},
        "routes": {"worker": "codex/gpt-5.6-sol", "reviewer": "vibe/zai-glm-5.2"},
    })
    now = datetime.now(timezone.utc)
    env = _usage_env(tmp_path, [
        _row(when=now, key="codex", project="p-a1b2", usage={
            "turns": 2, "input_tokens": 100, "output_tokens": 20,
            "reasoning_tokens": None, "cached_tokens": 40,
            "cache_write_tokens": None, "cost": None, "duration_seconds": 2,
            "source": "codex", "completeness": "partial",
        }),
        _row(when=now - timedelta(days=2), key="vibe", project="p-c3d4", usage=None),
    ])

    proc = run_raw(ws, ["usage"], env=env)

    assert proc.returncode == 0, proc.stderr
    assert "interactive" in proc.stdout and "models.claude" in proc.stdout
    assert "worker" in proc.stdout and "routes.worker" in proc.stdout
    assert "day" in proc.stdout and "week" in proc.stdout
    assert "cache-read=40" in proc.stdout
    assert "unknown" in proc.stdout


def test_usage_groups_by_project_digest(tmp_path):
    ws = make_workspace(tmp_path)
    now = datetime.now(timezone.utc)
    raw_path = "/private/secret/project"
    env = _usage_env(tmp_path, [
        _row(when=now, key="codex", project="a1b2c3d4", usage=None),
        _row(when=now, key="vibe", project="e5f6a7b8", usage=None),
    ])
    env["RAW_PROJECT_SENTINEL"] = raw_path

    proc = run_raw(ws, ["usage"], env=env)

    assert proc.returncode == 0, proc.stderr
    assert "a1b2c3d4" in proc.stdout and "e5f6a7b8" in proc.stdout
    assert raw_path not in proc.stdout + proc.stderr


def test_usage_lists_campaign_helper_evaluator_waits(tmp_path):
    ws = make_workspace(tmp_path)
    now = datetime.now(timezone.utc)
    reset = (now + timedelta(hours=1)).isoformat()
    waits = {
        f"codex\x1fcampaign:run-1": {"key": "codex", "caller_id": "campaign:run-1", "reason": "quota", "reset_at": reset, "hold_id": "h1", "observed_at": now.isoformat(), "owner_pid": os.getpid()},
        f"vibe\x1fhelper:literature": {"key": "vibe", "caller_id": "helper:literature", "reason": "daily_limit", "reset_at": reset, "hold_id": "h2", "observed_at": now.isoformat(), "owner_pid": os.getpid()},
        f"claude-code\x1fevaluator:eval-3": {"key": "claude-code", "caller_id": "evaluator:eval-3", "reason": "quota", "reset_at": None, "hold_id": "h3", "observed_at": now.isoformat(), "owner_pid": os.getpid()},
    }
    state = {"version": 1, "reservations": {}, "cooldowns": {}, "waits": waits, "pending_completions": {}, "completed": {}}
    env = _usage_env(tmp_path, [], state)

    proc = run_raw(ws, ["usage"], env=env)

    assert proc.returncode == 0, proc.stderr
    assert "campaign:run-1" in proc.stdout and "helper:literature" in proc.stdout
    assert "evaluator:eval-3" in proc.stdout
    assert "python -m router_runner.agentd resume-eval <id> --db <path>" in proc.stdout
    assert reset in proc.stdout


def test_usage_manual_resume_does_not_override_daily_budget(tmp_path):
    ws = make_workspace(tmp_path)
    now = datetime.now(timezone.utc)
    state = {
        "version": 1,
        "reservations": {},
        "cooldowns": {"codex": {"reason": "daily_limit", "reset_at": (now + timedelta(hours=1)).isoformat(), "observed_at": now.isoformat(), "generation": "daily-hold"}},
        "waits": {}, "pending_completions": {}, "completed": {},
    }
    env = _usage_env(tmp_path, [
        _row(when=now, key="codex", project="a1b2", usage=None) for _ in range(50)
    ], state)

    resumed = run_raw(ws, ["usage", "resume", "codex"], env=env)
    report = run_raw(ws, ["usage"], env=env)

    assert resumed.returncode == 1
    assert "releasable observed quota hold" in resumed.stderr
    assert "codex" in json.loads((tmp_path / "xdg-state/magnolia/usage-state.json").read_text())["cooldowns"]
    assert "day" in report.stdout and "50" in report.stdout


def test_usage_and_errors_never_print_keys(tmp_path):
    ws = make_workspace(tmp_path)
    sentinel = "sk-secret-never-print"
    write_setup(ws, {"memory_api_key": sentinel, "routes": {"worker": "codex/gpt"}})
    now = datetime.now(timezone.utc)
    env = _usage_env(tmp_path, [
        _row(when=now, key=sentinel, project="raw-project-name", usage=None),
    ], {
        "version": 1,
        "reservations": {},
        "cooldowns": {sentinel: {
            "reason": "quota", "reset_at": None,
            "observed_at": now.isoformat(), "generation": "hold",
        }},
        "waits": {},
        "pending_completions": {},
        "completed": {},
    })
    env["OPENAI_API_KEY"] = sentinel

    report = run_raw(ws, ["usage"], env=env)
    error = run_raw(ws, ["usage", "resume", sentinel], env=env)

    assert report.returncode == 0, report.stderr
    assert error.returncode == 1
    assert sentinel not in report.stdout + report.stderr + error.stdout + error.stderr


def test_policy_uses_optional_client_native_budgets(tmp_path):
    removed = tmp_path / "removed.yaml"
    removed.write_text("max_price_per_run: 1.0\n", encoding="utf-8")
    with pytest.raises(PolicyError, match="unknown usage policy fields"):
        Policy.load(removed)

    native = tmp_path / "native.yaml"
    native.write_text(
        "client_native_budgets:\n"
        "  claude-code: 2.5\n"
        "  vibe: 1.25\n",
        encoding="utf-8",
    )
    policy = Policy.load(native)
    assert policy.client_budget("claude-code") == 2.5
    assert policy.client_budget("vibe") == 1.25
    assert policy.client_budget("codex") is None
