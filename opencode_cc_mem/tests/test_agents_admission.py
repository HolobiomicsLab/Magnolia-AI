"""Sender and path admission tests for helper-agent letters."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from campaign_runner.helper_admission import (
    AdmissionRefused,
    admit_letter,
    consume_generation_receipt,
    write_generation_receipt,
)


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
