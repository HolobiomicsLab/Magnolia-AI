import json
import os
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from campaign_runner.cli import main  # noqa: E402
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
