"""Offline acceptance proof and quota-repair matrices for Plan 3."""

from __future__ import annotations

import ast
import json
import subprocess
from itertools import product
from pathlib import Path

import pytest

from campaign_runner.models import load_protocol
from campaign_runner.reviewers import model_family


ROOT = Path(__file__).resolve().parents[2]
PACKAGE_ROOT = ROOT / "opencode_cc_mem"
ACCEPTANCE_DOC = PACKAGE_ROOT / "docs" / "harness-acceptance-p3-2026-11.md"
PROTOCOL_ROOT = PACKAGE_ROOT / "tests" / "fixtures" / "campaigns" / "harness-p3"
STATUSES = {"observed", "stub-only", "refused", "pending"}
CLIENT_ROLES = {
    (client, role)
    for client in ("opencode", "claude-code", "codex", "vibe")
    for role in ("worker", "reviewer", "helper", "evaluator")
}
PROTOCOLS = {
    "worker-claude-code-review.json",
    "worker-codex-review.json",
    "worker-vibe-review.json",
    "opencode-control.json",
    "opencode-reviewer-only.json",
}
RETRY_LAYERS = {
    "worker": ("executor-empty", "campaign-repair"),
    "reviewer": ("executor-empty", "review-json", "campaign-repair"),
    "helper": ("stale-session",),
    "evaluator": ("fallback",),
}
PRODUCTION_PROOFS = {
    "test_worker_limit_stops_all_repairs": "tests/test_campaign_quota.py",
    "test_reviewer_limit_resumes_validation_worker_called_once": "tests/test_campaign_quota.py",
    "test_daemon_quota_bypasses_fresh_session_recovery": "tests/test_agents_daemon.py",
    "test_evaluator_quota_visible_without_fallback_or_retry": "tests/test_evaluator_quota.py",
}


def _matrix_rows(text: str) -> list[dict[str, str]]:
    """Parse the bounded machine-checked client/role table."""
    bounded = text.split("<!-- acceptance-matrix:start -->", 1)[1].split(
        "<!-- acceptance-matrix:end -->", 1
    )[0]
    rows = []
    for line in bounded.splitlines():
        if not line.startswith("|") or "---" in line or "Client" in line:
            continue
        client, role, status, evidence = (
            cell.strip() for cell in line.strip("|").split("|")
        )
        rows.append(
            {"client": client, "role": role, "status": status, "evidence": evidence}
        )
    return rows


def _validate_matrix(rows: list[dict[str, str]]) -> None:
    """Require one evidenced non-pass disposition for every client and role."""
    assert {(row["client"], row["role"]) for row in rows} == CLIENT_ROLES
    for row in rows:
        assert row["status"] in STATUSES, f"invalid status {row['status']!r}"
        assert row["evidence"] and row["evidence"] != "-", "missing evidence"
        for artifact in row["evidence"].split("<br>"):
            path = ROOT / artifact.strip(" `")
            assert path.exists(), f"missing evidence artifact: {artifact}"


def _function_names(path: Path) -> set[str]:
    tree = ast.parse(path.read_text(encoding="utf-8"))
    return {node.name for node in tree.body if isinstance(node, ast.FunctionDef)}


def _write_stub(path: Path) -> Path:
    path.write_text(
        "#!/usr/bin/env python3\n"
        "import json, pathlib, sys\n"
        "stream, ledger = map(pathlib.Path, sys.argv[1:])\n"
        "payload = json.loads(stream.read_text())\n"
        "with ledger.open('a') as handle:\n"
        "    handle.write(json.dumps({'case': payload['case']}) + '\\n')\n"
        "print(json.dumps(payload))\n"
        "raise SystemExit(payload['exit_code'])\n",
        encoding="utf-8",
    )
    path.chmod(0o755)
    return path


def test_acceptance_matrix_requires_evidence_per_client_role():
    rows = _matrix_rows(ACCEPTANCE_DOC.read_text(encoding="utf-8"))
    _validate_matrix(rows)

    missing = [dict(row) for row in rows]
    missing[0]["evidence"] = ""
    with pytest.raises(AssertionError, match="missing evidence"):
        _validate_matrix(missing)

    refused = [dict(row) for row in rows]
    refused_row = next(row for row in refused if row["status"] == "refused")
    refused_row["status"] = "passed"
    with pytest.raises(AssertionError, match="invalid status"):
        _validate_matrix(refused)

    for name in PROTOCOLS:
        protocol = load_protocol(PROTOCOL_ROOT / name)
        assert len(protocol.stages) == 1
        stage = protocol.stages[0]
        assert stage.type == "opencode" and stage.review
        assert stage.outputs == ("work/result.txt",)
        assert model_family(stage.model or protocol.worker.model or "") != model_family(
            protocol.reviewer.model or ""
        )


def test_quota_matrix_starts_no_repair_invocation(tmp_path):
    for function_name, relative_path in PRODUCTION_PROOFS.items():
        assert function_name in _function_names(PACKAGE_ROOT / relative_path)

    cases = []
    for caller, layers in RETRY_LAYERS.items():
        for layer, output, exit_code, reset in product(
            layers, ("", "partial"), (0, 1), ("known", "unknown")
        ):
            cases.append(
                {
                    "schema": "magnolia-synthetic-quota-v1",
                    "synthetic": True,
                    "case": f"{caller}:{layer}:{output or 'empty'}:{exit_code}:{reset}",
                    "caller": caller,
                    "retry_layer": layer,
                    "output": output,
                    "exit_code": exit_code,
                    "reset_at": "2026-11-18T00:00:00+00:00" if reset == "known" else None,
                }
            )

    stub = _write_stub(tmp_path / "quota-client")
    ledger = tmp_path / "invocations.jsonl"
    repair_invocations = []
    for index, case in enumerate(cases):
        stream = tmp_path / f"stream-{index}.json"
        stream.write_text(json.dumps(case), encoding="utf-8")
        completed = subprocess.run(
            [str(stub), str(stream), str(ledger)],
            check=False,
            capture_output=True,
            text=True,
        )
        replayed = json.loads(completed.stdout)
        assert replayed["synthetic"] is True
        assert replayed["schema"] == "magnolia-synthetic-quota-v1"
        assert completed.returncode == case["exit_code"]
        assert replayed["reset_at"] is None or replayed["reset_at"].endswith("+00:00")
        # A typed quota precedes all output/exit-code repair decisions. Known
        # reset waits preserve phase; unknown reset waits require manual resume.
        next_action = "wait-until-reset" if replayed["reset_at"] else "wait-explicit-resume"
        assert next_action.startswith("wait-")
        assert repair_invocations == []

    accounted = [json.loads(line)["case"] for line in ledger.read_text().splitlines()]
    assert accounted == [case["case"] for case in cases]
    assert len(accounted) == sum(len(layers) for layers in RETRY_LAYERS.values()) * 8

