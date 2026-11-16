"""Fail-closed admission for helper-agent inbox letters."""

from __future__ import annotations

import hashlib
import json
import os
import re
import stat
import tempfile
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


SAFE_IDENTIFIER = re.compile(r"[A-Za-z0-9][A-Za-z0-9_-]{0,127}")
FROM_HEADER = re.compile(r"^[\s>*-]*from:\s*(\S.*?)\s*$", re.IGNORECASE)
SCHEDULED_NAME = re.compile(
    r"\d{4}-\d{2}-\d{2}_([A-Za-z0-9][A-Za-z0-9_-]{0,127})-scheduled-sweep\.task\.md"
)
HEADER_LINES = 15
HEADER_BYTES = 16 * 1024
RECEIPT_SCHEMA = "magnolia-helper-generation-v1"


class AdmissionRefused(RuntimeError):
    """A letter failed sender, registry, or filesystem admission."""

    def __init__(
        self,
        reason: str,
        *,
        letter: Path | None = None,
        can_mark: bool = False,
    ) -> None:
        self.reason = reason
        self.letter = letter
        self.can_mark = can_mark
        super().__init__(reason)


@dataclass(frozen=True)
class AdmittedLetter:
    """Canonical identities proved before actionable letter content is read."""

    checkout: Path
    projects_root: Path
    lane: str
    lane_project: Path
    inbox: Path
    letter: Path
    sender: str
    sender_project: Path | None
    is_scheduled: bool = False


def admit_letter(
    checkout: str | Path, lane: str, letter: str | Path
) -> AdmittedLetter:
    """Validate one registered-lane letter and its same-checkout sender."""
    lane_name = _safe_identifier(lane, "lane identifier")
    checkout_path = _canonical_directory(Path(checkout), "checkout")
    projects = _projects_root(checkout_path)
    lane_project = projects / lane_name
    _require_plain_directory(lane_project, projects, "lane")
    _require_registered_lane(lane_project)
    inbox = lane_project / "inbox"
    _require_plain_directory(inbox, projects, "inbox")
    letter_path = _letter_path(letter, inbox, projects)
    header = _read_header(letter_path)
    sender = _sender_from_header(header, letter_path)
    relative = letter_path.relative_to(inbox)
    if sender == "agents-daemon":
        _admit_scheduled(checkout_path, lane_name, relative, letter_path)
        return AdmittedLetter(
            checkout_path,
            projects,
            lane_name,
            lane_project.resolve(),
            inbox.resolve(),
            letter_path,
            sender,
            None,
            True,
        )
    sender_name = _safe_identifier(sender, "sender identifier", letter_path)
    expected_parent = Path(f"from-{sender_name}")
    if relative.parent != expected_parent:
        raise AdmissionRefused(
            f"letter must be directly inside inbox/{expected_parent}",
            letter=letter_path,
            can_mark=True,
        )
    sender_project = projects / sender_name
    try:
        _require_plain_directory(sender_project, projects, "sender project")
    except AdmissionRefused as exc:
        raise AdmissionRefused(
            f"sender project is unknown or unsafe: {sender_name}",
            letter=letter_path,
            can_mark=True,
        ) from exc
    return AdmittedLetter(
        checkout_path,
        projects,
        lane_name,
        lane_project.resolve(),
        inbox.resolve(),
        letter_path,
        sender_name,
        sender_project.resolve(),
    )


def generation_receipt_path(
    checkout: str | Path, lane: str, letter: str | Path
) -> Path:
    """Return the checkout-local receipt path for one scheduled letter name."""
    lane_name = _safe_identifier(lane, "lane identifier")
    letter_name = Path(letter).name
    key = hashlib.sha256(f"{lane_name}\0{letter_name}".encode()).hexdigest()
    return Path(checkout) / ".magnolia-agents" / "generation-receipts" / f"{key}.json"


def write_generation_receipt(
    checkout: str | Path, lane: str, letter: str | Path
) -> Path:
    """Persist lane, path, and content digest for a daemon-generated letter."""
    checkout_path = _canonical_directory(Path(checkout), "checkout")
    lane_name = _safe_identifier(lane, "lane identifier")
    letter_path = Path(letter)
    digest = _file_digest(letter_path)
    receipt = generation_receipt_path(checkout_path, lane_name, letter_path)
    state = checkout_path / ".magnolia-agents"
    _ensure_plain_directory(state, checkout_path, "daemon state directory")
    _ensure_plain_directory(
        receipt.parent, checkout_path, "generation receipt directory"
    )
    payload = {
        "schema_version": RECEIPT_SCHEMA,
        "lane": lane_name,
        "letter": letter_path.name,
        "content_digest": digest,
        "generated_at": datetime.now(timezone.utc).isoformat(),
    }
    descriptor, temporary = tempfile.mkstemp(
        prefix=f".{receipt.name}.", dir=str(receipt.parent)
    )
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
            json.dump(payload, handle, sort_keys=True)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, receipt)
    finally:
        try:
            os.unlink(temporary)
        except FileNotFoundError:
            pass
    return receipt


def _safe_identifier(
    value: Any, label: str, letter: Path | None = None
) -> str:
    if not isinstance(value, str) or SAFE_IDENTIFIER.fullmatch(value) is None:
        raise AdmissionRefused(
            f"unsafe {label}: {value!r}", letter=letter, can_mark=letter is not None
        )
    return value


def _canonical_directory(path: Path, label: str) -> Path:
    if path.is_symlink():
        raise AdmissionRefused(f"{label} must not be a symlink")
    try:
        resolved = path.resolve(strict=True)
    except OSError as exc:
        raise AdmissionRefused(f"{label} does not exist: {path}") from exc
    if not resolved.is_dir():
        raise AdmissionRefused(f"{label} is not a directory: {path}")
    return resolved


def _projects_root(checkout: Path) -> Path:
    candidates = (checkout / "opencode_cc_mem" / "projects", checkout / "projects")
    projects = next((candidate for candidate in candidates if candidate.exists()), candidates[0])
    _require_plain_directory(projects, checkout, "projects root")
    return projects.resolve()


def _require_plain_directory(path: Path, boundary: Path, label: str) -> None:
    _require_lexical_containment(path, boundary, label)
    _reject_symlinks(path, boundary, label)
    if not path.is_dir():
        raise AdmissionRefused(f"{label} does not exist or is not a directory: {path}")


def _ensure_plain_directory(path: Path, boundary: Path, label: str) -> None:
    _require_lexical_containment(path, boundary, label)
    parent = path.parent
    if parent != boundary:
        _reject_symlinks(parent, boundary, label)
    if path.exists() or path.is_symlink():
        _require_plain_directory(path, boundary, label)
        return
    path.mkdir()
    _require_plain_directory(path, boundary, label)


def _require_registered_lane(lane_project: Path) -> None:
    config = lane_project / "agent.json"
    if config.is_symlink() or not config.is_file():
        raise AdmissionRefused("lane is not canonically registered by agent.json")
    try:
        payload = json.loads(config.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise AdmissionRefused("lane agent.json is unreadable or invalid") from exc
    if not isinstance(payload, dict) or payload.get("enabled") is False:
        raise AdmissionRefused("lane is disabled or has an invalid registration")


def _letter_path(letter: str | Path, inbox: Path, projects: Path) -> Path:
    supplied = Path(letter)
    absolute = Path(os.path.abspath(os.fspath(supplied)))
    try:
        absolute.relative_to(inbox)
    except ValueError as exc:
        raise AdmissionRefused("letter is outside the canonical lane inbox") from exc
    _reject_symlinks(absolute, projects, "letter")
    if absolute.suffixes[-2:] != [".task", ".md"]:
        raise AdmissionRefused("letter is not a .task.md file", letter=absolute)
    try:
        mode = absolute.stat(follow_symlinks=False).st_mode
    except OSError as exc:
        raise AdmissionRefused("letter does not exist", letter=absolute) from exc
    if not stat.S_ISREG(mode):
        raise AdmissionRefused("letter is not a regular file", letter=absolute)
    return absolute.resolve()


def _read_header(letter: Path) -> str:
    flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0)
    try:
        descriptor = os.open(letter, flags)
        with os.fdopen(descriptor, "rb") as handle:
            body = handle.read(HEADER_BYTES + 1)
    except OSError as exc:
        raise AdmissionRefused("letter header cannot be read safely", letter=letter) from exc
    return body[:HEADER_BYTES].decode("utf-8", errors="replace")


def _sender_from_header(header: str, letter: Path) -> str:
    senders = []
    for line in header.splitlines()[:HEADER_LINES]:
        match = FROM_HEADER.match(line)
        if match:
            senders.append(match.group(1).strip())
    if len(senders) != 1:
        raise AdmissionRefused(
            "letter header must contain exactly one from: identity",
            letter=letter,
            can_mark=True,
        )
    return senders[0]


def _admit_scheduled(
    checkout: Path, lane: str, relative: Path, letter: Path
) -> None:
    match = SCHEDULED_NAME.fullmatch(letter.name)
    if relative.parent != Path(".") or match is None or match.group(1) != lane:
        raise AdmissionRefused(
            "agents-daemon sender is restricted to the scheduled letter path",
            letter=letter,
            can_mark=True,
        )
    receipt = generation_receipt_path(checkout, lane, letter)
    _reject_symlinks(receipt, checkout, "generation receipt")
    if not receipt.is_file():
        raise AdmissionRefused(
            "scheduled letter has no persisted generation receipt",
            letter=letter,
            can_mark=True,
        )
    try:
        payload = json.loads(receipt.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise AdmissionRefused(
            "scheduled generation receipt is invalid", letter=letter, can_mark=True
        ) from exc
    if (
        not isinstance(payload, dict)
        or payload.get("schema_version") != RECEIPT_SCHEMA
        or payload.get("lane") != lane
        or payload.get("letter") != letter.name
    ):
        raise AdmissionRefused(
            "scheduled generation receipt does not match lane and letter",
            letter=letter,
            can_mark=True,
        )
    if payload.get("content_digest") != _file_digest(letter):
        raise AdmissionRefused(
            "scheduled generation receipt content digest does not match",
            letter=letter,
            can_mark=True,
        )


def _file_digest(path: Path) -> str:
    flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0)
    try:
        descriptor = os.open(path, flags)
        with os.fdopen(descriptor, "rb") as handle:
            if not stat.S_ISREG(os.fstat(handle.fileno()).st_mode):
                raise AdmissionRefused("letter is not a regular file")
            digest = hashlib.sha256()
            for block in iter(lambda: handle.read(1024 * 1024), b""):
                digest.update(block)
            return digest.hexdigest()
    except OSError as exc:
        raise AdmissionRefused("letter content cannot be hashed safely") from exc


def _require_lexical_containment(path: Path, boundary: Path, label: str) -> None:
    absolute = Path(os.path.abspath(os.fspath(path)))
    try:
        absolute.relative_to(boundary)
    except ValueError as exc:
        raise AdmissionRefused(f"{label} escapes the checkout projects") from exc


def _reject_symlinks(path: Path, boundary: Path, label: str) -> None:
    _require_lexical_containment(path, boundary, label)
    relative = Path(os.path.abspath(os.fspath(path))).relative_to(boundary)
    current = boundary
    if current.is_symlink():
        raise AdmissionRefused(f"{label} crosses a symlink: {current}")
    for part in relative.parts:
        current = current / part
        if current.is_symlink():
            raise AdmissionRefused(f"{label} crosses a symlink: {current}")
