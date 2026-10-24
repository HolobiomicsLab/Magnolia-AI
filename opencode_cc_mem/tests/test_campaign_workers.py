import json
import os
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from campaign_runner.workers import OpenCodeExecutor, parse_run_events  # noqa: E402


FAKE = r'''#!/usr/bin/env python3
import json
import os
import subprocess
import sys
import time

args = sys.argv[1:]
log_path = os.environ.get("FAKE_LOG")
if log_path:
    with open(log_path, "w", encoding="utf-8") as handle:
        json.dump(args, handle)
env_log_path = os.environ.get("FAKE_ENV_LOG")
if env_log_path:
    with open(env_log_path, "w", encoding="utf-8") as handle:
        handle.write(os.environ.get("OPENCODE_CONFIG_CONTENT", ""))
mode = os.environ.get("FAKE_MODE", "ok")
session = "ses_fake"
if "child-pid" in os.environ:
    child = subprocess.Popen(["sleep", "30"])
    with open(os.environ["child-pid"], "w", encoding="utf-8") as handle:
        handle.write(str(child.pid))
if mode == "exit":
    raise SystemExit(2)
if mode == "empty_first" and "--session" not in args:
    print(json.dumps({"type": "step_start", "sessionID": session, "part": {"type": "step-start"}}))
    raise SystemExit(0)
if mode == "timeout":
    print(json.dumps({"type": "step_start", "sessionID": session, "part": {"type": "step-start"}}), flush=True)
    time.sleep(30)
    raise SystemExit(0)
if mode == "review":
    text = '{"issues":[],"followups":[]}'
else:
    text = "DONE"
print(json.dumps({"type": "step_start", "sessionID": session, "part": {"type": "step-start"}}))
print(json.dumps({"type": "tool_use", "sessionID": session, "part": {"type": "tool", "tool": "read"}}))
print(json.dumps({"type": "step_finish", "sessionID": session, "part": {"type": "step-finish", "cost": 0.1, "tokens": {"input": 10, "output": 3, "reasoning": 1, "cache": {"read": 2, "write": 0}}}}))
print(json.dumps({"type": "text", "sessionID": session, "part": {"type": "text", "messageID": "m1", "text": text}}))
'''


def fake_binary(tmp_path):
    path = tmp_path / "fake-opencode"
    path.write_text(FAKE, encoding="utf-8")
    path.chmod(0o755)
    return path


def test_parse_v2_events():
    body = "\n".join(
        [
            json.dumps({"type": "step_start", "sessionID": "s1", "part": {"type": "step-start"}}),
            json.dumps({"type": "tool_use", "sessionID": "s1", "part": {"type": "tool", "tool": "read"}}),
            json.dumps({"type": "text", "sessionID": "s1", "part": {"type": "text", "messageID": "m1", "text": "one"}}),
            json.dumps({"type": "step_finish", "sessionID": "s1", "part": {"type": "step-finish", "cost": 0.2, "tokens": {"input": 5, "output": 2, "reasoning": 1, "cache": {"read": 1, "write": 0}}}}),
            json.dumps({"type": "text", "sessionID": "s1", "part": {"type": "text", "messageID": "m2", "text": "two"}}),
        ]
    )
    parsed = parse_run_events(body)
    assert parsed.session_id == "s1"
    assert parsed.final_text == "two"
    assert parsed.all_text == "one\ntwo"
    assert parsed.tool_calls == 1
    assert parsed.usage["input_tokens"] == 5
    assert parsed.usage["cost_usd"] == pytest.approx(0.2)


def test_executor_builds_expected_argv_and_parses(tmp_path):
    binary = fake_binary(tmp_path)
    log = tmp_path / "args.json"
    executor = OpenCodeExecutor(
        binary=str(binary),
        model="opencode-go/deepseek-v4.1-flash",
        agent="campaign-worker",
        server_url="http://127.0.0.1:4111",
        db_path=str(tmp_path / "db.sqlite"),
        environment={"FAKE_LOG": str(log)},
        timeout_seconds=10,
    )
    result = executor.run("do work", tmp_path, tmp_path / "attempt")
    argv = json.loads(log.read_text())
    assert "--auto" in argv
    assert "--format" in argv and argv[argv.index("--format") + 1] == "json"
    assert argv[argv.index("--model") + 1] == "opencode-go/deepseek-v4.1-flash"
    assert argv[argv.index("--agent") + 1] == "campaign-worker"
    assert argv[argv.index("--server") + 1] == "http://127.0.0.1:4111"
    assert "--session" not in argv
    assert argv[-1] == "do work"
    assert result.returncode == 0
    assert result.session_id == "ses_fake"
    assert result.final_text == "DONE"
    assert result.tool_calls == 1
    assert result.attempts == 1
    assert result.usage["output_tokens"] == 3


def test_executor_injects_step_limit(tmp_path):
    binary = fake_binary(tmp_path)
    env_log = tmp_path / "env.txt"
    executor = OpenCodeExecutor(
        binary=str(binary),
        model="opencode-go/deepseek-v4.1-flash",
        agent="campaign-worker",
        environment={"FAKE_ENV_LOG": str(env_log)},
        timeout_seconds=10,
        max_steps=17,
    )
    executor.run("do work", tmp_path, tmp_path / "attempt")
    payload = json.loads(env_log.read_text())
    assert payload == {"agents": {"campaign-worker": {"steps": 17}}}


def test_executor_retries_empty_output_in_same_session(tmp_path):
    binary = fake_binary(tmp_path)
    log = tmp_path / "args.json"
    executor = OpenCodeExecutor(
        binary=str(binary),
        model="opencode-go/deepseek-v4.1-flash",
        agent="campaign-worker",
        environment={"FAKE_LOG": str(log), "FAKE_MODE": "empty_first"},
        timeout_seconds=10,
    )
    result = executor.run("do work", tmp_path, tmp_path / "attempt")
    assert result.attempts == 2
    assert result.final_text == "DONE"
    second_args = json.loads(log.read_text())
    assert "--session" in second_args
    assert second_args[second_args.index("--session") + 1] == "ses_fake"


def test_executor_nonzero_exit_keeps_result(tmp_path):
    binary = fake_binary(tmp_path)
    executor = OpenCodeExecutor(
        binary=str(binary),
        model="opencode-go/deepseek-v4.1-flash",
        agent="campaign-worker",
        environment={"FAKE_MODE": "exit"},
        timeout_seconds=10,
    )
    result = executor.run("do work", tmp_path, tmp_path / "attempt")
    assert result.returncode == 2
    assert result.final_text == ""


def test_executor_kills_process_group_on_timeout(tmp_path):
    binary = fake_binary(tmp_path)
    child_pid_path = tmp_path / "child.pid"
    executor = OpenCodeExecutor(
        binary=str(binary),
        model="opencode-go/deepseek-v4.1-flash",
        agent="campaign-worker",
        environment={"FAKE_MODE": "timeout", "child-pid": str(child_pid_path)},
        timeout_seconds=0.5,
    )
    result = executor.run("do work", tmp_path, tmp_path / "attempt")
    assert result.timed_out is True
    child_pid = int(child_pid_path.read_text())
    with pytest.raises(ProcessLookupError):
        os.kill(child_pid, 0)
