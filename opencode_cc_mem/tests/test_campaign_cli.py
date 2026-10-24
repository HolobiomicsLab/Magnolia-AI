import json
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
        "project": "hsc70_new",
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
    protocol = tmp_path / "protocol.json"
    protocol.write_text(json.dumps(protocol_payload()), encoding="utf-8")
    brief = tmp_path / "brief.md"
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
    run_dir = tmp_path / "run"
    assert main(["init", str(protocol), "--run-dir", str(run_dir), "--brief", str(brief)]) == 0
    assert main(["status", str(run_dir)]) == 0
    assert main(["pause", str(run_dir), "--reason", "test"]) == 0
    assert main(["resume", str(run_dir), "--reason", "test"]) == 0
    assert main(["halt", str(run_dir), "--reason", "test"]) == 0
    output = capsys.readouterr().out
    assert '"state": "halted"' in output


def test_approve_command(tmp_path, capsys):
    protocol, brief = make_inputs(tmp_path)
    run_dir = tmp_path / "run"
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
    )
    assert result.returncode == 0
    assert json.loads(result.stdout)["id"] == "cli-pilot"
