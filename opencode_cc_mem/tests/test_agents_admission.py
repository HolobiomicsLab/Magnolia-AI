"""Sender and path admission tests for helper-agent letters."""

from __future__ import annotations

import json
import os
from pathlib import Path

import pytest

from campaign_runner.helper_admission import (
    AdmissionRefused,
    admit_letter,
    consume_generation_receipt,
    run_setup_probe,
    write_generation_receipt,
)
from campaign_runner.governor import Governor, Policy
from campaign_runner.process import ProcessStartError


def _project(root: Path, name: str, *, lane: bool = False) -> Path:
    project = root / "opencode_cc_mem" / "projects" / name
    project.mkdir(parents=True)
    if lane:
        (project / "agent.json").write_text(
            json.dumps({"enabled": True}), encoding="utf-8"
        )
    return project


def _letter(root: Path, lane: str, sender: str, *, headers: str = "") -> Path:
    inbox = (
        root
        / "opencode_cc_mem"
        / "projects"
        / lane
        / "inbox"
        / f"from-{sender}"
    )
    inbox.mkdir(parents=True, exist_ok=True)
    letter = inbox / "request.task.md"
    letter.write_text(
        f"# Request\n- from: {sender}\n- status: open\n{headers}\n## Request\nDo it.\n",
        encoding="utf-8",
    )
    return letter


def test_setup_probe_filters_credentials_and_records_one_turn(tmp_path):
    binary = tmp_path / "probe"
    binary.write_text(
        "#!/bin/sh\n"
        "test -z \"${ANTHROPIC_API_KEY+x}\" || exit 81\n"
        "test -z \"${vendor_api_key+x}\" || exit 84\n"
        "test -z \"${CLAUDE_CONFIG_DIR+x}\" || exit 82\n"
        "test \"$MAGNOLIA_DOORBELL\" = 0 || exit 83\n"
        "printf 'probe-ok\\n'\n",
        encoding="utf-8",
    )
    binary.chmod(0o755)
    ledger = tmp_path / "usage.jsonl"
    governor = Governor(
        Policy(max_concurrent=1, min_seconds_between_runs=1), ledger_path=ledger
    )

    result = run_setup_probe(
        "claude",
        "haiku",
        "probe",
        tmp_path,
        str(binary),
        governor=governor,
        environment={
            **os.environ,
            "ANTHROPIC_API_KEY": "must-not-reach-child",
            "vendor_api_key": "case-insensitive-suffix-must-not-reach-child",
            "CLAUDE_CONFIG_DIR": "/must/not/reach/child",
        },
    )

    assert result.returncode == 0
    assert result.stdout == "probe-ok\n"
    rows = [json.loads(line) for line in ledger.read_text().splitlines()]
    assert len(rows) == 1
    assert rows[0]["usage"]["turns"] == 1
    assert rows[0]["usage"]["source"] == "setup-probe"


def test_opencode_setup_probe_keeps_provider_key(tmp_path):
    binary = tmp_path / "opencode-probe"
    binary.write_text(
        "#!/bin/sh\n"
        "test \"$OPENROUTER_API_KEY\" = provider-key || exit 85\n"
        "printf 'probe-ok\\n'\n",
        encoding="utf-8",
    )
    binary.chmod(0o755)

    result = run_setup_probe(
        "opencode",
        "openrouter/deepseek/deepseek-v4.1-flash",
        "probe",
        tmp_path,
        str(binary),
        governor=Governor(Policy(max_concurrent=1), ledger_path=tmp_path / "usage.jsonl"),
        environment={**os.environ, "OPENROUTER_API_KEY": "provider-key"},
    )

    assert result.returncode == 0


def test_setup_probe_spawn_failure_releases_admission(tmp_path, monkeypatch):
    governor = Governor(
        Policy(max_concurrent=1), ledger_path=tmp_path / "usage.jsonl"
    )

    def fail_start(*args, **kwargs):
        control = args[-1]
        control.on_start(1234, 1234)
        raise ProcessStartError(5, "spawn failed")

    monkeypatch.setattr("campaign_runner.helper_admission.run_process", fail_start)
    with pytest.raises(ProcessStartError):
        run_setup_probe(
            "claude", "haiku", "probe", tmp_path, "/bin/false",
            governor=governor, environment=dict(os.environ),
        )

    state = json.loads(governor.state_path.read_text())
    assert state["reservations"] == {}


def test_helper_valid_same_checkout_sender(tmp_path):
    _project(tmp_path, "literature", lane=True)
    _project(tmp_path, "xiulian")
    letter = _letter(tmp_path, "literature", "xiulian")

    admitted = admit_letter(tmp_path, "literature", letter)

    assert admitted.sender == "xiulian"
    assert admitted.lane == "literature"
    assert admitted.sender_project == (
        tmp_path / "opencode_cc_mem" / "projects" / "xiulian"
    ).resolve()


def test_helper_unknown_outside_conflicting_sender_refused(tmp_path):
    _project(tmp_path, "literature", lane=True)
    unknown = _letter(tmp_path, "literature", "missing")
    with pytest.raises(AdmissionRefused, match="sender project"):
        admit_letter(tmp_path, "literature", unknown)

    _project(tmp_path, "xiulian")
    conflicting = _letter(
        tmp_path, "literature", "xiulian", headers="- from: someone-else"
    )
    with pytest.raises(AdmissionRefused, match="exactly one"):
        admit_letter(tmp_path, "literature", conflicting)

    outside = tmp_path / "outside.task.md"
    outside.write_text("from: xiulian\nstatus: open\n", encoding="utf-8")
    with pytest.raises(AdmissionRefused, match="inbox"):
        admit_letter(tmp_path, "literature", outside)


def test_helper_lane_inbox_letter_traversal_symlink_refused(tmp_path):
    _project(tmp_path, "literature", lane=True)
    _project(tmp_path, "xiulian")
    with pytest.raises(AdmissionRefused, match="lane identifier"):
        admit_letter(tmp_path, "../literature", tmp_path / "nothing.task.md")

    lane = tmp_path / "opencode_cc_mem" / "projects" / "literature"
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "escape.task.md").write_text(
        "from: xiulian\nstatus: open\n", encoding="utf-8"
    )
    (lane / "inbox").symlink_to(outside, target_is_directory=True)
    with pytest.raises(AdmissionRefused, match="symlink"):
        admit_letter(tmp_path, "literature", lane / "inbox" / "escape.task.md")

    (lane / "inbox").unlink()
    inbox = lane / "inbox" / "from-xiulian"
    inbox.mkdir(parents=True)
    linked = inbox / "linked.task.md"
    linked.symlink_to(outside / "escape.task.md")
    with pytest.raises(AdmissionRefused, match="symlink"):
        admit_letter(tmp_path, "literature", linked)


def test_scheduled_letter_requires_generation_receipt(tmp_path):
    lane = _project(tmp_path, "literature", lane=True)
    inbox = lane / "inbox"
    inbox.mkdir()
    letter = inbox / "2026-11-16_literature-scheduled-sweep.task.md"
    letter.write_text(
        "# Scheduled sweep\n- from: agents-daemon\n- status: open\n\nRun it.\n",
        encoding="utf-8",
    )

    with pytest.raises(AdmissionRefused, match="generation receipt"):
        admit_letter(tmp_path, "literature", letter)

    write_generation_receipt(tmp_path, "literature", letter)
    assert admit_letter(tmp_path, "literature", letter).is_scheduled is True

    letter.write_text(letter.read_text() + "changed\n", encoding="utf-8")
    with pytest.raises(AdmissionRefused, match="digest"):
        admit_letter(tmp_path, "literature", letter)


def test_scheduled_receipt_ignores_daemon_status_and_records_consumption(tmp_path):
    lane = _project(tmp_path, "literature", lane=True)
    inbox = lane / "inbox"
    inbox.mkdir()
    letter = inbox / "2026-11-16_literature-scheduled-sweep.task.md"
    letter.write_text(
        "# Scheduled sweep\n- from: agents-daemon\n- status: open\n\nRun it.\n",
        encoding="utf-8",
    )
    write_generation_receipt(tmp_path, "literature", letter)

    admitted = admit_letter(tmp_path, "literature", letter)
    admitted.write_text(admitted.read_text().replace("status: open", "status: done"))
    consume_generation_receipt(admitted)
    admitted.close()

    replay = admit_letter(tmp_path, "literature", letter)
    assert replay.generation_consumed is True
    replay.close()


def test_scheduled_ticket_digest_ignores_lifecycle_fields(tmp_path):
    lane = _project(tmp_path, "literature", lane=True)
    inbox = lane / "inbox"
    inbox.mkdir()
    letter = inbox / "2026-11-16_literature-scheduled-sweep.task.md"
    request = (
        "# Scheduled sweep\n- from: agents-daemon\n- status: open\n"
        "ticket: idea\n\n## Request\nRun the durable evaluation.\n"
    )
    letter.write_text(request, encoding="utf-8")
    write_generation_receipt(tmp_path, "literature", letter)

    letter.write_text(
        request.replace("status: open", "status: prototyped").replace(
            "ticket: idea", "ticket: idea\nwait_for: job:run-123"
        )
        + "\n## Job result (run-123)\n\n- status: succeeded\n",
        encoding="utf-8",
    )

    admitted = admit_letter(tmp_path, "literature", letter)
    assert admitted.is_scheduled is True
    admitted.close()
