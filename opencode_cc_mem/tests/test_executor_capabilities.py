import copy
import json
import subprocess
import sys
from pathlib import Path

import pytest

from campaign_runner.executors.capabilities import (
    CapabilityTableError,
    load_capability_table,
    project_role_capabilities,
    validate_capability_table,
)


ROOT = Path(__file__).parents[1]
REPOSITORY_ROOT = ROOT.parent
TABLE = ROOT / "campaign_runner" / "executors" / "capabilities-p3.json"
FIXTURES = ROOT / "tests" / "fixtures" / "executors"


def test_capability_record_requires_identity_and_evidence(tmp_path):
    table = load_capability_table(TABLE, repository_root=REPOSITORY_ROOT)

    missing_identity = copy.deepcopy(table)
    del missing_identity["rows"][0]["cells"]["identity"]["value"]["executable"]
    with pytest.raises(CapabilityTableError, match="executable"):
        validate_capability_table(missing_identity, repository_root=REPOSITORY_ROOT)

    synthetic_observation = copy.deepcopy(table)
    identity = synthetic_observation["rows"][0]["cells"]["identity"]
    identity["artifact"] = (
        "opencode_cc_mem/tests/fixtures/executors/claude/synthetic/quota.json"
    )
    with pytest.raises(CapabilityTableError, match="synthetic"):
        validate_capability_table(synthetic_observation, repository_root=REPOSITORY_ROOT)

    marked_synthetic = copy.deepcopy(table)
    marker = tmp_path / "marked.json"
    marker.write_text('{"synthetic": true}\n', encoding="utf-8")
    marked_synthetic["rows"][0]["cells"]["identity"]["artifact"] = marker.name
    with pytest.raises(CapabilityTableError, match="synthetic"):
        validate_capability_table(marked_synthetic, repository_root=tmp_path)

    missing_artifact = copy.deepcopy(table)
    missing_artifact["rows"][0]["cells"]["identity"]["artifact"] = (
        "opencode_cc_mem/tests/fixtures/executors/claude/recorded/missing.json"
    )
    with pytest.raises(CapabilityTableError, match="does not exist"):
        validate_capability_table(missing_artifact, repository_root=REPOSITORY_ROOT)


def test_capability_record_rejects_unknown_status():
    table = load_capability_table(TABLE, repository_root=REPOSITORY_ROOT)
    table["rows"][0]["cells"]["argv"]["status"] = "assumed"

    with pytest.raises(CapabilityTableError, match="unknown status"):
        validate_capability_table(table, repository_root=REPOSITORY_ROOT)


def test_recorded_usage_shapes_survive_scrubbing(tmp_path):
    source = tmp_path / "usage.json"
    output = tmp_path / "scrubbed.json"
    source.write_text(
        json.dumps(
            {
                "usage": {
                    "input_tokens": 101,
                    "output_tokens": 17,
                    "cached_input_tokens": 83,
                    "cache_creation_input_tokens": 19,
                },
                "modelUsage": {
                    "cacheReadInputTokens": 83,
                    "cacheCreationInputTokens": 19,
                },
            }
        ),
        encoding="utf-8",
    )

    completed = subprocess.run(
        [
            sys.executable,
            str(ROOT / "scripts" / "probes" / "make_fixtures.py"),
            "--scrub-json",
            str(source),
            str(output),
        ],
        check=False,
        capture_output=True,
        text=True,
    )

    assert completed.returncode == 0, completed.stderr
    scrubbed = json.loads(output.read_text(encoding="utf-8"))
    assert scrubbed["usage"] == {
        "input_tokens": 101,
        "output_tokens": 17,
        "cached_input_tokens": 83,
        "cache_creation_input_tokens": 19,
    }
    assert scrubbed["modelUsage"] == {
        "cacheReadInputTokens": 83,
        "cacheCreationInputTokens": 19,
    }

    recorded = json.loads(
        (FIXTURES / "vibe" / "recorded" / "export.json").read_text(encoding="utf-8")
    )
    assert isinstance(recorded["usage"]["input_tokens"], int)
    assert isinstance(recorded["usage"]["cached_input_tokens"], int)


def test_current_table_refuses_every_new_client_role():
    table = load_capability_table(TABLE, repository_root=REPOSITORY_ROOT)
    projected = project_role_capabilities(table)

    assert set(projected) == {"claude-code", "codex", "vibe"}
    for client in projected.values():
        assert set(client) == {"worker", "reviewer", "helper", "evaluator"}
        assert all(not role["accepted"] for role in client.values())
        assert all(role["blocking_cells"] for role in client.values())

    assert any("D2" in reason for reason in projected["codex"]["worker"]["reasons"])
    assert any("D2" in reason for reason in projected["codex"]["reviewer"]["reasons"])
    assert any("D4" in reason for reason in projected["vibe"]["worker"]["reasons"])
