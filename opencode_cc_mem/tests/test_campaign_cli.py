import json
import os
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from campaign_runner.cli import _memory_route, main  # noqa: E402
from campaign_runner.routing import ResolvedRoute  # noqa: E402
from campaign_runner.state import CampaignStore  # noqa: E402


def protocol_payload():
    return {
        "schema": 1,
        "id": "cli-pilot",
        "project": "project",
        "worker": {
            "model": "opencode-go/deepseek-v4.1-flash",
            "agent": "campaign-worker",
        },
        "reviewer": {"enabled": False},
        "budget": {
            "max_tokens": 1000,
            "max_wall_hours": 1,
            "max_core_hours": 1,
            "max_attempts_per_stage": 2,
            "max_parallel_jobs": 1,
            "max_followups": 0,
        },
        "stages": [
            {"id": "confirm", "type": "human"},
            {
                "id": "prepare",
                "type": "script",
                "command": ["/bin/true"],
                "gate": ["/bin/true"],
            },
        ],
    }


def test_vibe_campaign_route_is_managed_and_never_static(monkeypatch):
    monkeypatch.setenv("MAGNOLIA_MEMORY_URL", "http://127.0.0.1:9999/mcp")
    route = ResolvedRoute("vibe", "model", "family", "vibe", "reviewer", "test")

    assert _memory_route(route) == {"transport": "managed"}


def test_claude_campaign_route_keeps_provider_keys_in_managed_server():
    route = ResolvedRoute("claude-code", "haiku", "family", "claude-code", "worker", "test")

    memory = _memory_route(route, "/repo/.venv/bin/python3", managed=True)

    assert memory == {"transport": "managed"}


@pytest.mark.parametrize("override", (None, "/selected/.venv/bin/python3"))
def test_campaign_uses_repository_interpreter_for_context_and_memory(tmp_path, monkeypatch, override):
    from campaign_runner import cli

    installation = tmp_path / "installation" / "opencode_cc_mem"
    monkeypatch.setattr(cli, "_ROOT", installation)
    monkeypatch.setattr(cli.sys, "executable", "/bare/python3")
    monkeypatch.delenv("MAGNOLIA_PYTHON", raising=False)
    if override:
        monkeypatch.setenv("MAGNOLIA_PYTHON", override)
    expected = override or str(installation.parent / ".venv/bin/python3")
    protocol, brief = make_inputs(tmp_path)
    payload = protocol_payload()
    payload["worker"]["model"] = "claude-code/haiku"
    protocol.write_text(json.dumps(payload))
    run = tmp_path / "project/runs/test"
    CampaignStore(run).initialize(protocol, brief)
    monkeypatch.setattr(cli, "Governor", lambda: SimpleNamespace(policy=SimpleNamespace(max_turns_per_run=60)))
    monkeypatch.setattr(cli, "executor_for", lambda route, role, context: context)
    runner = cli._build_runner(SimpleNamespace(run_dir=str(run), command="run"))
    context = runner.config.worker_factory("claude-code/haiku", 30)
    assert context.python_executable == expected
    assert context.memory_service == {"transport": "managed"}
    assert _memory_route(context.route, managed=True) == {"transport": "managed"}


def make_inputs(tmp_path):
    campaign_dir = tmp_path / "project" / "campaigns"
    campaign_dir.mkdir(parents=True, exist_ok=True)
    protocol = campaign_dir / "protocol.json"
    protocol.write_text(json.dumps(protocol_payload()), encoding="utf-8")
    brief = campaign_dir / "brief.md"
    brief.write_text("# Brief\n", encoding="utf-8")
    return protocol, brief


def test_validate_command(tmp_path, capsys):
    protocol, _ = make_inputs(tmp_path)
    code = main(["validate", str(protocol)])
    assert code == 0
    output = json.loads(capsys.readouterr().out)
    assert output["id"] == "cli-pilot"
    assert output["stages"] == ["confirm", "prepare"]
    assert len(output["sha256"]) == 64


def test_init_status_pause_resume_halt(tmp_path, capsys):
    protocol, brief = make_inputs(tmp_path)
    run_dir = tmp_path / "project" / "runs" / "run"
    assert main(["init", str(protocol), "--run-dir", str(run_dir), "--brief", str(brief)]) == 0
    assert main(["status", str(run_dir)]) == 0
    assert main(["pause", str(run_dir), "--reason", "test"]) == 0
    assert main(["resume", str(run_dir), "--reason", "test"]) == 0
    assert main(["halt", str(run_dir), "--reason", "test"]) == 0
    output = capsys.readouterr().out
    assert '"state": "halted"' in output


def test_approve_command(tmp_path, capsys):
    protocol, brief = make_inputs(tmp_path)
    run_dir = tmp_path / "project" / "runs" / "run"
    store = CampaignStore(run_dir)
    store.initialize(protocol, brief)
    store.transition("running", "start")
    store.transition("waiting_human", "need approval")
    assert main(["approve", str(run_dir), "--approver", "tao", "--reason", "ok"]) == 0
    assert store.load().state == "queued"
    output = capsys.readouterr().out
    assert '"state": "queued"' in output


def test_decline_followups_command_uses_runner_state_api(tmp_path, capsys, monkeypatch):
    protocol, brief = make_inputs(tmp_path)
    run_dir = tmp_path / "project" / "runs" / "run"
    store = CampaignStore(run_dir)
    store.initialize(protocol, brief)
    store.transition("running", "start")
    store.transition("waiting_human", "review followups need a decision")
    calls = []

    class Runner:
        def decline_followups(self, approver, reason, minutes=0):
            calls.append((approver, reason, minutes))
            return store.transition("completed", "declined followups")

    monkeypatch.setattr("campaign_runner.cli._build_runner", lambda _args: Runner())

    assert main([
        "decline-followups", str(run_dir), "--approver", "tao",
        "--reason", "stage is sufficient", "--minutes", "2",
    ]) == 0
    assert calls == [("tao", "stage is sufficient", 2.0)]
    assert '"state": "completed"' in capsys.readouterr().out


def test_invalid_protocol_returns_two(tmp_path, capsys):
    protocol, _ = make_inputs(tmp_path)
    protocol.write_text("{}", encoding="utf-8")
    assert main(["validate", str(protocol)]) == 2
    assert "error:" in capsys.readouterr().err


def test_executable_entrypoint(tmp_path):
    protocol, _ = make_inputs(tmp_path)
    executable = ROOT / "softwares" / "bin" / "magnolia-campaign"
    result = subprocess.run(
        [sys.executable, str(executable), "validate", str(protocol)],
        capture_output=True,
        text=True,
        check=False,
        env={**os.environ, "MAGNOLIA_CAMPAIGN_NO_REEXEC": "1"},
    )
    assert result.returncode == 0
    assert json.loads(result.stdout)["id"] == "cli-pilot"


def test_fake_backend_rejected_in_production(tmp_path, capsys, monkeypatch):
    protocol, brief = make_inputs(tmp_path)
    project = tmp_path / "project"
    run_dir = project / "runs" / "run"
    assert main(["init", str(protocol), "--run-dir", str(run_dir), "--brief", str(brief)]) == 0
    monkeypatch.delenv("MAGNOLIA_TESTING", raising=False)
    assert (
        main(
            [
                "run",
                str(run_dir),
                "--project-dir",
                str(project),
                "--job-backend",
                "fake",
            ]
        )
        == 2
    )
    assert "MAGNOLIA_TESTING" in capsys.readouterr().err


def test_protocol_project_mismatch_rejected(tmp_path, capsys):
    payload = protocol_payload()
    payload["project"] = "other-project"
    campaign_dir = tmp_path / "project" / "campaigns"
    campaign_dir.mkdir(parents=True)
    protocol = campaign_dir / "protocol.json"
    protocol.write_text(json.dumps(payload))
    brief = campaign_dir / "brief.md"
    brief.write_text("brief")
    run_dir = tmp_path / "project" / "runs" / "run"
    assert main(["init", str(protocol), "--run-dir", str(run_dir), "--brief", str(brief)]) == 2
    assert "does not match" in capsys.readouterr().err


def test_protocol_outside_allowed_root_rejected(tmp_path, capsys):
    payload = protocol_payload()
    outside = tmp_path / "outside"
    outside.mkdir()
    protocol = outside / "protocol.json"
    protocol.write_text(json.dumps(payload))
    brief = outside / "brief.md"
    brief.write_text("brief")
    run_dir = tmp_path / "project" / "runs" / "run"
    assert main(["init", str(protocol), "--run-dir", str(run_dir), "--brief", str(brief)]) == 2
    assert "campaigns/" in capsys.readouterr().err


def test_run_command_executes_script_stage(tmp_path, capsys):
    payload = protocol_payload()
    payload["stages"] = [
        {
            "id": "prepare",
            "type": "script",
            "command": [
                sys.executable,
                "-c",
                "from pathlib import Path; Path('work/out.txt').write_text('ok')",
            ],
            "outputs": ["work/out.txt"],
            "gate": [sys.executable, "-c", "raise SystemExit(0)"],
        },
        {"id": "review", "type": "human"},
    ]
    project = tmp_path / "project"
    campaign_dir = project / "campaigns"
    campaign_dir.mkdir(parents=True)
    protocol = campaign_dir / "protocol.json"
    protocol.write_text(json.dumps(payload))
    brief = campaign_dir / "brief.md"
    brief.write_text("brief")
    run_dir = project / "runs" / "run"
    assert main(["init", str(protocol), "--run-dir", str(run_dir), "--brief", str(brief)]) == 0
    assert (
        main(
            [
                "approve-start",
                str(run_dir),
                "--approver",
                "tao",
                "--reason",
                "start",
            ]
        )
        == 0
    )
    capsys.readouterr()
    assert (
        main(
            [
                "run",
                str(run_dir),
                "--project-dir",
                str(project),
            ]
        )
        == 0
    )
    output = json.loads(capsys.readouterr().out)
    assert output["state"]["state"] == "waiting_human"
    assert output["state"]["stage"] == "review"
    assert (run_dir / "work" / "out.txt").is_file()


def test_dedicated_service_gets_step_limits_and_agents(monkeypatch):
    # Observed on OpenCode 2.3.0: `agents.<name>.steps` reaches a dedicated
    # service only from the service's own environment at start.
    from campaign_runner.cli import _service_settings
    from campaign_runner.models import Protocol

    payload = protocol_payload()
    payload["worker"]["max_steps"] = 12
    payload["reviewer"] = {"enabled": True, "model": "m/x", "max_steps": 4}
    monkeypatch.setenv("OPENCODE_CONFIG_CONTENT", json.dumps({"logLevel": "INFO"}))
    config, agents = _service_settings(Protocol.from_dict(payload))
    assert json.loads(config) == {
        "logLevel": "INFO",
        "agents": {"campaign-worker": {"steps": 12},
                   "campaign-reviewer": {"steps": 4}},
    }
    assert agents == ["campaign-worker", "campaign-reviewer"]

    payload["reviewer"] = {"enabled": False}
    monkeypatch.delenv("OPENCODE_CONFIG_CONTENT")
    config, agents = _service_settings(Protocol.from_dict(payload))
    assert json.loads(config) == {"agents": {"campaign-worker": {"steps": 12}}}
    assert agents == ["campaign-worker"]
