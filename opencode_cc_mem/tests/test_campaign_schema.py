import copy
import json
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from campaign_runner.models import (  # noqa: E402
    ProtocolError,
    load_protocol,
    protocol_sha256,
)


def valid_protocol():
    return {
        "schema": 1,
        "id": "pilot-v1",
        "project": "hsc70_new",
        "worker": {
            "model": "opencode-go/deepseek-v4.1-flash",
            "hard_stage_model": "kimi-code-plan-cn/k3.1-pro#high",
            "agent": "campaign-worker",
            "max_steps": 30,
            "timeout_seconds": 1800,
        },
        "reviewer": {
            "enabled": True,
            "model": "opencode-go/deepseek-v4.1-flash",
            "alternate_model": "zai-coding-plan/glm-5.3",
            "max_steps": 8,
            "timeout_seconds": 600,
            "repair_attempts": 1,
        },
        "budget": {
            "max_tokens": 2000000,
            "max_wall_hours": 24,
            "max_core_hours": 40,
            "max_attempts_per_stage": 2,
            "max_parallel_jobs": 2,
            "max_followups": 0,
        },
        "stages": [
            {"id": "confirm", "type": "human"},
            {
                "id": "prepare",
                "type": "script",
                "command": ["/bin/true"],
                "outputs": ["work/prepared.txt"],
                "gate": ["/bin/true"],
            },
            {
                "id": "analyze",
                "type": "opencode",
                "model": "kimi-code-plan-cn/k3.1-pro#high",
                "prompt": "prompts/analyze.md",
                "outputs": ["work/analysis.md"],
                "gate": ["/bin/true"],
                "review": True,
            },
            {
                "id": "dock",
                "type": "job",
                "command": "paired-docking",
                "gate": ["/bin/true"],
            },
        ],
    }


def write_protocol(tmp_path, payload):
    path = tmp_path / "protocol.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload), encoding="utf-8")
    return path


def test_valid_protocol_loads(tmp_path):
    path = write_protocol(tmp_path, valid_protocol())
    protocol = load_protocol(path)
    assert protocol.id == "pilot-v1"
    assert protocol.worker.model == "opencode-go/deepseek-v4.1-flash"
    assert protocol.reviewer.alternate_model == "zai-coding-plan/glm-5.3"
    assert protocol.stage("analyze").model == "kimi-code-plan-cn/k3.1-pro#high"
    assert protocol.next_stage_id("prepare") == "analyze"
    assert protocol.next_stage_id("dock") is None


def test_protocol_hash_is_stable_and_content_sensitive(tmp_path):
    path = write_protocol(tmp_path, valid_protocol())
    first = load_protocol(path)
    second = load_protocol(path)
    assert protocol_sha256(first) == protocol_sha256(second)
    changed = copy.deepcopy(valid_protocol())
    changed["worker"]["model"] = "zai-coding-plan/glm-5.3"
    other = load_protocol(write_protocol(tmp_path / "other", changed))
    assert protocol_sha256(first) != protocol_sha256(other)


@pytest.mark.parametrize(
    "mutator, message",
    [
        (lambda p: p.update(extra=True), "unknown field"),
        (lambda p: p["stages"][1].update(extra=1), "unknown field"),
        (lambda p: p["worker"].update(model=""), "must not be empty"),
        (lambda p: p["worker"].update(model="no-provider"), "provider/model"),
        (lambda p: p["reviewer"].update(model=None), "model is required"),
        (lambda p: p["stages"][1].pop("gate"), "missing required field 'gate'"),
        (lambda p: p["stages"][2].pop("prompt"), "missing required field 'prompt'"),
        (lambda p: p["stages"][3].pop("command"), "missing required field 'command'"),
        (lambda p: p["stages"][1].update(retry="always"), "must be one of"),
        (lambda p: p["stages"][1].update(on_gate_fail="ignore"), "must be one of"),
        (lambda p: p["stages"][1].update(type="magic"), "must be one of"),
        (lambda p: p["stages"][1].update(model="x/y"), "only opencode stages"),
        (lambda p: p["stages"][1].update(review=True), "only opencode stages"),
        (lambda p: p["stages"][1].update(gate=["../escape"]), "must not contain"),
        (lambda p: p["stages"][2].update(prompt="../escape.md"), "relative path"),
        (lambda p: p["budget"].update(max_tokens=0), "must be >= 1"),
        (lambda p: p["budget"].pop("max_core_hours"), "missing required field"),
    ],
)
def test_protocol_rejects_invalid_input(tmp_path, mutator, message):
    payload = valid_protocol()
    mutator(payload)
    path = write_protocol(tmp_path, payload)
    with pytest.raises(ProtocolError, match=message):
        load_protocol(path)


def test_protocol_rejects_duplicate_stage_ids(tmp_path):
    payload = valid_protocol()
    payload["stages"][2]["id"] = "prepare"
    path = write_protocol(tmp_path, payload)
    with pytest.raises(ProtocolError, match="duplicate stage id"):
        load_protocol(path)


def test_protocol_rejects_empty_stages(tmp_path):
    payload = valid_protocol()
    payload["stages"] = []
    path = write_protocol(tmp_path, payload)
    with pytest.raises(ProtocolError, match="non-empty array"):
        load_protocol(path)


def test_protocol_rejects_bad_json(tmp_path):
    path = tmp_path / "bad.json"
    path.write_text("{not json", encoding="utf-8")
    with pytest.raises(ProtocolError, match="invalid JSON"):
        load_protocol(path)


def test_disabled_reviewer_does_not_require_model(tmp_path):
    payload = valid_protocol()
    payload["reviewer"] = {"enabled": False}
    protocol = load_protocol(write_protocol(tmp_path, payload))
    assert protocol.reviewer.enabled is False
    assert protocol.reviewer.model is None
