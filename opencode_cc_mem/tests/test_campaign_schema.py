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


def test_omitted_models_resolve_or_fail_before_launch(tmp_path):
    from campaign_runner.cli import resolve_campaign_routes

    payload = valid_protocol()
    payload["worker"].pop("model")
    payload["reviewer"].pop("model")
    protocol = load_protocol(write_protocol(tmp_path, payload))
    routes = resolve_campaign_routes(protocol)
    assert routes.worker["analyze"].model == "kimi-code-plan-cn/k3.1-pro#high"
    with pytest.raises(ProtocolError, match="reviewer"):
        resolve_campaign_routes(protocol, require_reviewers=True)


def test_hard_stage_override_preserved(tmp_path):
    from campaign_runner.cli import resolve_campaign_routes

    payload = valid_protocol()
    payload["stages"][2].pop("model")
    payload["stages"][2]["hard"] = True
    protocol = load_protocol(write_protocol(tmp_path, payload))
    routes = resolve_campaign_routes(protocol)
    assert routes.worker["analyze"].model == "kimi-code-plan-cn/k3.1-pro#high"


def test_agent_alias_normalizes_without_changing_legacy_hash(tmp_path):
    legacy = valid_protocol()
    protocol = load_protocol(write_protocol(tmp_path / "legacy", legacy))
    assert protocol_sha256(protocol) == protocol_sha256(
        load_protocol(write_protocol(tmp_path / "again", legacy))
    )
    alias = valid_protocol()
    alias["stages"][2]["type"] = "agent"
    normalized = load_protocol(write_protocol(tmp_path / "alias", alias))
    assert normalized.stage("analyze").type == "opencode"


def test_opencode_service_for_reviewer_or_alternate_only(tmp_path):
    from campaign_runner.cli import opencode_service_roles, resolve_campaign_routes

    payload = valid_protocol()
    payload["worker"]["model"] = "claude-code/sonnet"
    payload["stages"][2]["model"] = "claude-code/opus"
    payload["reviewer"]["model"] = "claude-code/opus"
    payload["reviewer"]["alternate_model"] = "openai/gpt-5"
    routes = resolve_campaign_routes(load_protocol(write_protocol(tmp_path, payload)))
    assert opencode_service_roles(routes) == {"reviewer"}


def test_no_opencode_service_for_claude_only_protocol(tmp_path):
    from campaign_runner.cli import opencode_service_roles, resolve_campaign_routes

    payload = valid_protocol()
    payload["worker"]["model"] = "claude-code/sonnet"
    payload["worker"]["hard_stage_model"] = "claude-code/opus"
    payload["stages"][2]["model"] = "claude-code/opus"
    payload["reviewer"]["model"] = "claude-code/sonnet"
    payload["reviewer"]["alternate_model"] = "claude-code/opus"
    routes = resolve_campaign_routes(load_protocol(write_protocol(tmp_path, payload)))
    assert opencode_service_roles(routes) == set()
