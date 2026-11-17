import copy
import json
import subprocess
import sys
from pathlib import Path

import pytest

from campaign_runner.executors.capabilities import (
    CapabilityTableError,
    PREREQUISITE_CELLS,
    load_capability_table,
    project_role_capabilities,
    validate_capability_table,
)


ROOT = Path(__file__).parents[1]
REPOSITORY_ROOT = ROOT.parent
TABLE = ROOT / "campaign_runner" / "executors" / "capabilities-p3.json"
FIXTURES = ROOT / "tests" / "fixtures" / "executors"
RECORDED_METADATA = {"synthetic", "recorded_from", "dropped_keys"}


def _all_observed_table(tmp_path):
    table = load_capability_table(TABLE, repository_root=REPOSITORY_ROOT)
    evidence = tmp_path / "observed.json"
    evidence.write_text('{"synthetic": false}\n', encoding="utf-8")
    values = {
        "argv": ["client", "run"],
        "model_delivery": {"accepted": True, "model": "model", "identity": "model"},
        "discovery_dir": {"scope": "launch-project"},
        "trust": True,
        "hook_policy_digest": "sha256:policy",
        "native_schemas": {"read": "file_path"},
        "allowed_actions": ["native_read"],
        "denied_actions": ["native_shell"],
        "resume": {"same_session": True},
        "caps": {"max_turns": 8},
        "usage_semantics": {"scope": "invocation"},
    }
    for row in table["rows"]:
        for name in PREREQUISITE_CELLS:
            cell = row["cells"][name]
            cell.update(status="observed", artifact=evidence.name)
            if name != "identity":
                cell["value"] = copy.deepcopy(values[name])
        row["cells"]["quota_evidence"] = {"status": "unknown", "artifact": None}
        if "http_api_key_env" in row["cells"]:
            row["cells"]["http_api_key_env"] = {
                "status": "unknown",
                "artifact": None,
            }
        if "vibe_memory" in row["cells"]:
            row["cells"]["vibe_memory"] = {
                "status": "unknown",
                "artifact": None,
            }
        row["gate"] = {
            "status": "observed",
            "artifact": evidence.name,
            "value": "accepted",
            "reason": "All prerequisites observed.",
        }
        row["stale"] = False
    return table


def _join_path(parent, child):
    return f"{parent}.{child}" if parent else str(child)


def _assert_recorded_subset(recorded, source, path=""):
    if isinstance(recorded, dict):
        assert isinstance(source, dict), path
        omitted = [_join_path(path, key) for key in source.keys() - recorded.keys()]
        for key in recorded.keys() - source.keys():
            pytest.fail(f"{_join_path(path, key)} is absent from recorded_from source")
        for key in recorded.keys() & source.keys():
            omitted.extend(
                _assert_recorded_subset(recorded[key], source[key], _join_path(path, key))
            )
        return omitted
    if isinstance(recorded, list):
        assert isinstance(source, list) and len(recorded) <= len(source), path
        omitted = [f"{path}[{index}]" for index in range(len(recorded), len(source))]
        for index, value in enumerate(recorded):
            omitted.extend(_assert_recorded_subset(value, source[index], f"{path}[{index}]"))
        return omitted
    assert recorded == source, path
    return []


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


def test_current_capability_citations_are_repository_tracked_safe():
    table = json.loads(TABLE.read_text(encoding="utf-8"))
    citations = list(table["generated_from"]["sources"])
    for row in table["rows"]:
        citations.append(row["gate"].get("artifact"))
        citations.extend(cell.get("artifact") for cell in row["cells"].values())

    for citation in (value for value in citations if value is not None):
        relative = Path(citation)
        assert not relative.is_absolute(), citation
        assert relative.parts and relative.parts[0] != ".superpowers", citation
        assert (REPOSITORY_ROOT / relative).exists(), citation


def test_capability_record_rejects_unknown_status():
    table = load_capability_table(TABLE, repository_root=REPOSITORY_ROOT)
    table["rows"][0]["cells"]["argv"]["status"] = "assumed"

    with pytest.raises(CapabilityTableError, match="unknown status"):
        validate_capability_table(table, repository_root=REPOSITORY_ROOT)


def test_capability_record_requires_boolean_stale_and_observed_value(tmp_path):
    table = _all_observed_table(tmp_path)
    del table["rows"][0]["stale"]
    with pytest.raises(CapabilityTableError, match="stale"):
        validate_capability_table(table, repository_root=tmp_path)

    table = _all_observed_table(tmp_path)
    del table["rows"][0]["cells"]["argv"]["value"]
    with pytest.raises(CapabilityTableError, match="non-empty value"):
        validate_capability_table(table, repository_root=tmp_path)


@pytest.mark.parametrize("suffix,contents", [(".json", "{"), (".jsonl", "{}\n{")])
def test_observed_json_artifact_must_parse(tmp_path, suffix, contents):
    table = _all_observed_table(tmp_path)
    artifact = tmp_path / f"broken{suffix}"
    artifact.write_text(contents, encoding="utf-8")
    table["rows"][0]["cells"]["argv"]["artifact"] = artifact.name

    with pytest.raises(CapabilityTableError, match="parse"):
        validate_capability_table(table, repository_root=tmp_path)


def test_observed_jsonl_artifact_detects_synthetic_lines(tmp_path):
    table = _all_observed_table(tmp_path)
    artifact = tmp_path / "stream.jsonl"
    artifact.write_text('{}\n{"synthetic": true}\n', encoding="utf-8")
    table["rows"][0]["cells"]["argv"]["artifact"] = artifact.name

    with pytest.raises(CapabilityTableError, match="synthetic"):
        validate_capability_table(table, repository_root=tmp_path)


def test_d2_d4_refusal_reason_requires_refused_gate(tmp_path):
    table = _all_observed_table(tmp_path)
    table["rows"][0]["gate"]["reason"] = "D2: campaign roles remain refused."

    with pytest.raises(CapabilityTableError, match="D2/D4 refusal"):
        validate_capability_table(table, repository_root=tmp_path)


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
                "session_id": "private-session-id",
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
    assert scrubbed["session_id"] == "ses-fixture-1"

    recorded = json.loads(
        (FIXTURES / "vibe" / "recorded" / "export.json").read_text(encoding="utf-8")
    )
    assert isinstance(recorded["usage"]["input_tokens"], int)
    assert isinstance(recorded["usage"]["cached_input_tokens"], int)


def test_recorded_fixtures_match_their_sources():
    for fixture in sorted(FIXTURES.glob("*/recorded/*.json")):
        recorded = json.loads(fixture.read_text(encoding="utf-8"))
        source = json.loads(
            (REPOSITORY_ROOT / recorded["recorded_from"]).read_text(encoding="utf-8")
        )
        payload = {key: value for key, value in recorded.items() if key not in RECORDED_METADATA}
        omitted = _assert_recorded_subset(payload, source)
        assert sorted(recorded["dropped_keys"]) == sorted(omitted), fixture


def test_all_observed_prerequisites_project_to_accepted_launch_fields(tmp_path):
    table = _all_observed_table(tmp_path)
    validate_capability_table(table, repository_root=tmp_path)

    role = project_role_capabilities(table)["claude-code"]["worker"]

    assert role == {
        "accepted": True,
        "gate_accepted": True,
        "stale": False,
        "trusted": True,
        "identity": "sha256:policy",
        "discovery_dir": {"scope": "launch-project"},
        "capabilities": ["native_read"],
        "model_isolation": {"accepted": True, "model": "model", "identity": "model"},
        "blocking_cells": [],
        "reasons": ["All prerequisites observed."],
    }


@pytest.mark.parametrize("cell_name", PREREQUISITE_CELLS)
def test_each_unknown_prerequisite_blocks_and_names_it(tmp_path, cell_name):
    table = _all_observed_table(tmp_path)
    table["rows"][0]["cells"][cell_name] = {"status": "unknown", "artifact": None}

    role = project_role_capabilities(table)["claude-code"]["worker"]

    assert role["accepted"] is False
    assert role["blocking_cells"] == [cell_name]


@pytest.mark.parametrize("quota_status", ["unknown", "synthetic-only"])
def test_quota_evidence_never_blocks_projection(tmp_path, quota_status):
    table = _all_observed_table(tmp_path)
    quota = table["rows"][0]["cells"]["quota_evidence"]
    quota["status"] = quota_status
    quota["artifact"] = None if quota_status == "unknown" else "observed.json"

    role = project_role_capabilities(table)["claude-code"]["worker"]

    assert role["accepted"] is True
    assert role["blocking_cells"] == []


@pytest.mark.parametrize(
    "client,role,decision",
    [("codex", "worker", "D2"), ("codex", "reviewer", "D2"), ("vibe", "worker", "D4")],
)
def test_d2_d4_rows_remain_refused_with_observed_cells(tmp_path, client, role, decision):
    table = _all_observed_table(tmp_path)
    row = next(item for item in table["rows"] if (item["client"], item["role"]) == (client, role))
    row["gate"].update(status="refused", reason=f"{decision}: role remains refused.")

    projected = project_role_capabilities(table)[client][role]

    assert projected["accepted"] is False
    assert projected["blocking_cells"] == []
    assert decision in projected["reasons"][0]


def test_projection_treats_missing_or_non_false_stale_as_stale(tmp_path):
    table = _all_observed_table(tmp_path)
    del table["rows"][0]["stale"]
    table["rows"][1]["stale"] = "false"

    projected = project_role_capabilities(table)["claude-code"]

    assert projected["worker"]["stale"] is True
    assert projected["reviewer"]["stale"] is True


def test_current_table_accepts_claude_and_codex_campaign_roles():
    table = load_capability_table(TABLE, repository_root=REPOSITORY_ROOT)
    projected = project_role_capabilities(table)

    assert set(projected) == {"claude-code", "codex", "vibe"}
    for client in projected.values():
        assert set(client) == {"worker", "reviewer", "helper", "evaluator"}

    assert all(role["gate_accepted"] for role in projected["claude-code"].values())
    assert all(role["accepted"] for role in projected["claude-code"].values())
    assert all(not role["stale"] for role in projected["claude-code"].values())
    assert all("launch_identity" in role for role in projected["claude-code"].values())
    assert all("launch_identity" in role for role in projected["codex"].values())
    assert all(
        role["launch_identity"]["components"]["model"] == "gpt-5.6-luna"
        for role in projected["codex"].values()
    )
    for role in ("worker", "reviewer"):
        assert projected["codex"][role]["gate_accepted"] is True
        assert projected["codex"][role]["accepted"] is True
        assert projected["codex"][role]["blocking_cells"] == []
    for role in ("helper", "evaluator"):
        assert projected["codex"][role]["accepted"] is False
        assert projected["codex"][role]["blocking_cells"]
    assert all(not role["accepted"] for role in projected["vibe"].values())
    assert all(role["blocking_cells"] for role in projected["vibe"].values())
    assert any("D4" in reason for reason in projected["vibe"]["worker"]["reasons"])


def test_claude_rows_accepted_from_live_gate():
    table = load_capability_table(TABLE, repository_root=REPOSITORY_ROOT)
    projected = project_role_capabilities(table)["claude-code"]
    rows = [row for row in table["rows"] if row["client"] == "claude-code"]

    assert {row["role"] for row in rows} == {
        "worker",
        "reviewer",
        "helper",
        "evaluator",
    }
    assert all(row["gate"]["status"] == "observed" for row in rows)
    assert all(not item["blocking_cells"] for item in projected.values())
    assert all(item["gate_accepted"] is True for item in projected.values())
    worker = next(row for row in rows if row["role"] == "worker")
    assert "MCP allowlist" in worker["gate"]["reason"]
    assert {"hook_interpreter", "hook_runner", "hook_source"} <= set(
        worker["gate"]["value"]["configuration_binding"]["components"]
    )


def test_claude_row_stale_on_identity_change():
    table = load_capability_table(TABLE, repository_root=REPOSITORY_ROOT)
    worker = next(
        row
        for row in table["rows"]
        if (row["client"], row["role"]) == ("claude-code", "worker")
    )
    worker["stale"] = False

    fresh = project_role_capabilities(table)["claude-code"]["worker"]
    assert fresh["stale"] is False
    assert fresh["accepted"] is True

    worker["cells"]["identity"]["value"]["sha256"] = "changed-client-binary"
    changed = project_role_capabilities(table)["claude-code"]["worker"]
    assert changed["stale"] is True
    assert changed["accepted"] is False


def test_codex_campaign_rows_accepted_from_live_gate():
    table = load_capability_table(TABLE, repository_root=REPOSITORY_ROOT)
    projected = project_role_capabilities(table)
    rows = {
        row["role"]: row
        for row in table["rows"]
        if row["client"] == "codex" and row["role"] in {"worker", "reviewer"}
    }

    assert set(rows) == {"worker", "reviewer"}
    assert all(row["gate"]["status"] == "observed" for row in rows.values())
    assert all(projected["codex"][role]["accepted"] for role in rows)
    assert rows["worker"]["gate"]["value"]["configuration_binding"][
        "configuration_identity_sha256"
    ] == "a5f58afaa3aa6fa7efd4949f048fa8cbbba155a0a68d035b8424d2e57d50fadb"
    assert rows["reviewer"]["gate"]["value"]["configuration_binding"][
        "configuration_identity_sha256"
    ] == "21acaebecfb171ce22b54550abcdceffa4b9ae359cbf7d306c5587847762485f"


def test_codex_helper_evaluator_and_vibe_rows_still_refused():
    table = load_capability_table(TABLE, repository_root=REPOSITORY_ROOT)
    projected = project_role_capabilities(table)

    assert projected["codex"]["helper"]["accepted"] is False
    assert projected["codex"]["evaluator"]["accepted"] is False
    assert all(
        row["cells"]["vibe_memory"]["status"] != "observed"
        for row in table["rows"]
        if row["client"] == "vibe"
    )
    assert projected["vibe"]["worker"]["vibe_memory"] is False
    assert all(not row["accepted"] for row in projected["vibe"].values())


def test_codex_rows_share_their_reaudited_client_version_and_hash():
    table = load_capability_table(TABLE, repository_root=REPOSITORY_ROOT)
    rows = [row for row in table["rows"] if row["client"] == "codex"]
    identities = [row["gate"]["value"]["client_identity"] for row in rows]

    assert len(rows) == 4
    assert all(
        identity["version"] == identities[0]["version"] for identity in identities
    )
    assert all(
        identity["sha256"] == identities[0]["sha256"] for identity in identities
    )
    assert all(
        row["cells"]["identity"]["value"] == identity
        for row, identity in zip(rows, identities, strict=True)
    )
    assert all(identity["version"] in identity["executable"] for identity in identities)
    assert len(identities[0]["sha256"]) == 64
