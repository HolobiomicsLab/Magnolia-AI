"""Fail-closed admission and descriptor-safe I/O for helper letters."""

from __future__ import annotations

import hashlib
import json
import os
import re
import stat
import subprocess
import sys
import tempfile
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from campaign_runner.executors.contracts import UsageRecord
from campaign_runner.governor import Admission, AdmissionRequest, Governor, QuotaWaiting
from campaign_runner.process import ProcessControl, run_process
from campaign_runner.routing import resolve_route
from compchem_memory.harness.memory_service import is_provider_api_key_name


SAFE_IDENTIFIER = re.compile(r"[A-Za-z0-9][A-Za-z0-9_-]{0,127}")
FROM_HEADER = re.compile(r"^[\s>*-]*from:\s*(\S.*?)\s*$", re.IGNORECASE)
STATUS_HEADER = re.compile(r"^[\s>*-]*status:\s*\S+?[ \t]*$", re.IGNORECASE)
WAIT_FOR_HEADER = re.compile(r"^\s*wait_for:\s*job:", re.IGNORECASE)
JOB_RESULT_HEADER = re.compile(r"^##\s+Job result\b", re.IGNORECASE)
SCHEDULED_NAME = re.compile(
    r"\d{4}-\d{2}-\d{2}_([A-Za-z0-9][A-Za-z0-9_-]{0,127})-scheduled-sweep\.task\.md"
)
HEADER_LINES = 15
HEADER_BYTES = 16 * 1024
RECEIPT_SCHEMA = "magnolia-helper-generation-v2"
_PROBE_CLIENT_PREFIX = {
    "claude": "claude-code",
    "codex": "codex",
    "vibe": "vibe",
}
_SETUP_FORBIDDEN_ENV = frozenset({
    "CLAUDE_CONFIG_DIR",
    "CODEX_HOME",
    "VIBE_HOME",
    "CODEX_API_KEY",
    "ANTHROPIC_API_KEY",
    "ANTHROPIC_AUTH_TOKEN",
    "OPENAI_API_KEY",
    "MISTRAL_API_KEY",
    "CODE_MODE_HOST",
    "CODEX_EXEC_SERVER_URL",
})


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


def _probe_route(harness: str, model: str | None):
    """Resolve one setup probe onto the same subscription key as its client."""
    if harness == "opencode":
        selected = model or "opencode-go/default"
    else:
        prefix = _PROBE_CLIENT_PREFIX.get(harness)
        if prefix is None:
            raise AdmissionRefused(f"unsupported setup probe harness: {harness}")
        selected = f"{prefix}/{model or 'default'}"
    return resolve_route("evaluator", {"model": selected})


def _probe_environment(source: dict[str, str], harness: str) -> dict[str, str]:
    """Filter billing credentials and config-home overrides from a probe."""
    strips_provider_keys = harness != "opencode"
    environment = {
        name: value
        for name, value in source.items()
        if (
            name not in _SETUP_FORBIDDEN_ENV
            or (not strips_provider_keys and is_provider_api_key_name(name))
        )
        and (not strips_provider_keys or not is_provider_api_key_name(name))
        and not name.upper().endswith("_AUTH_TOKEN")
    }
    environment["MAGNOLIA_DOORBELL"] = "0"
    if harness == "vibe":
        environment["VIBE_ENABLE_TELEMETRY"] = "false"
        environment.pop("VIBE_DISABLE_TELEMETRY", None)
    return environment


def _setup_probe_argv(
    harness: str, binary: str, model: str | None, prompt: str,
) -> list[str]:
    """Build the characterized noninteractive connectivity command."""
    if harness == "opencode":
        argv = [binary, "run"]
        if model:
            argv.extend(["-m", model])
        return [*argv, prompt]
    if harness == "claude":
        argv = [binary, "-p", prompt]
        if model:
            argv.extend(["--model", model])
        return argv
    if harness == "codex":
        argv = [binary, "exec"]
        if model:
            argv.extend(["-m", model])
        return [*argv, prompt]
    if harness == "vibe":
        argv = [binary, "-p", prompt]
        if model:
            argv.extend(["--model", model])
        return argv
    raise AdmissionRefused(f"unsupported setup probe harness: {harness}")


def run_setup_probe(
    harness: str,
    model: str | None,
    prompt: str,
    project_dir: str | Path,
    binary: str,
    *,
    governor: Governor | None = None,
    environment: dict[str, str] | None = None,
    timeout_seconds: int = 180,
) -> subprocess.CompletedProcess[str]:
    """Run one bounded, receipted probe with the harness's documented environment."""
    route = _probe_route(harness, model)
    usage_governor = governor or Governor()
    project = Path(project_dir).resolve()
    request = AdmissionRequest(
        route,
        f"setup-probe:{uuid.uuid4().hex}",
        "setup-probe",
        hashlib.sha256(str(project).encode()).hexdigest(),
        "setup-probe",
        1,
    )
    admitted = usage_governor.admit(request)
    if isinstance(admitted, QuotaWaiting):
        raise AdmissionRefused(f"setup probe waiting for {admitted.reason}")
    assert isinstance(admitted, Admission)
    child_environment = _probe_environment(
        dict(os.environ if environment is None else environment), harness,
    )
    argv = _setup_probe_argv(harness, binary, model, prompt)
    with tempfile.TemporaryDirectory(prefix="magnolia-setup-probe-") as temporary:
        output = Path(temporary) / "stdout"
        errors = Path(temporary) / "stderr"
        control = ProcessControl(
            on_start=lambda pid, pgid: admitted.mark_started(pid=pid, pgid=pgid),
            hold_before_exec=True,
        )
        with admitted:
            result = run_process(
                argv, project, child_environment, timeout_seconds, output, errors, control,
            )
            stdout = output.read_text(encoding="utf-8", errors="replace")
            stderr = errors.read_text(encoding="utf-8", errors="replace")
            if result.client_started:
                admitted.release(
                    usage=UsageRecord(turns=1, source="setup-probe"),
                    outcome=result.outcome,
                )
            else:
                admitted.cancel_before_launch()
        return subprocess.CompletedProcess(argv, result.returncode, stdout, stderr)


def _setup_probe_main(argv: list[str]) -> int:
    """CLI bridge used by the Bash launcher without serializing secrets."""
    import argparse

    parser = argparse.ArgumentParser(prog="campaign_runner.helper_admission setup-probe")
    parser.add_argument("command", choices=("setup-probe",))
    parser.add_argument("--harness", required=True)
    parser.add_argument("--model")
    parser.add_argument("--prompt", required=True)
    parser.add_argument("--project", required=True, type=Path)
    parser.add_argument("--binary", required=True)
    args = parser.parse_args(argv)
    try:
        result = run_setup_probe(
            args.harness, args.model, args.prompt, args.project, args.binary,
        )
    except AdmissionRefused as error:
        print(str(error), file=sys.stderr)
        return 1
    sys.stdout.write(result.stdout)
    sys.stderr.write(result.stderr)
    return result.returncode


@dataclass
class AdmittedLetter:
    """A pinned letter inode and its validated containing directory."""

    checkout: Path
    projects_root: Path
    lane: str
    lane_project: Path
    inbox: Path
    letter: Path
    sender: str
    sender_project: Path | None
    parent: Path
    parent_fd: int = field(repr=False)
    letter_fd: int = field(repr=False)
    letter_identity: tuple[int, int] = field(repr=False)
    is_scheduled: bool = False
    generation_id: str | None = None
    generation_consumed: bool = False

    def close(self) -> None:
        """Close the pinned file and directory descriptors."""
        for name in ("letter_fd", "parent_fd"):
            descriptor = getattr(self, name, -1)
            if descriptor >= 0:
                os.close(descriptor)
                setattr(self, name, -1)

    def __enter__(self) -> "AdmittedLetter":
        return self

    def __exit__(self, *unused: object) -> None:
        self.close()

    def __del__(self) -> None:
        try:
            self.close()
        except Exception:
            pass

    def read_text(self) -> str:
        """Read exactly the regular file inode admitted by the gate."""
        return _read_descriptor(self.letter_fd).decode("utf-8", errors="replace")

    def digest(self) -> str:
        """Hash exactly the admitted letter inode."""
        return hashlib.sha256(_read_descriptor(self.letter_fd)).hexdigest()

    def write_text(self, text: str) -> None:
        """Atomically replace the admitted letter inside its pinned directory."""
        self._assert_current_letter()
        _atomic_write_at(self.parent_fd, self.letter.name, text.encode())
        replacement = _open_regular_at(self.parent_fd, self.letter.name)
        os.close(self.letter_fd)
        self.letter_fd = replacement
        details = os.fstat(replacement)
        self.letter_identity = (details.st_dev, details.st_ino)

    def write_reply(self, text: str, *, replace_existing: bool = True) -> Path:
        """Atomically write a regular sibling reply without following links."""
        self._assert_current_letter()
        name = self.letter.stem.replace(".task", "") + "_reply.md"
        _check_replace_target(self.parent_fd, name, replace_existing)
        _atomic_write_at(self.parent_fd, name, text.encode())
        return self.parent / name

    def _assert_parent(self) -> None:
        _reject_symlinks(self.parent, self.inbox, "letter directory")
        try:
            current = self.parent.stat(follow_symlinks=False)
            pinned = os.fstat(self.parent_fd)
        except OSError as exc:
            raise AdmissionRefused("validated letter directory is no longer available") from exc
        if (current.st_dev, current.st_ino) != (pinned.st_dev, pinned.st_ino):
            raise AdmissionRefused("validated letter directory changed after admission")

    def _assert_current_letter(self) -> None:
        self._assert_parent()
        try:
            current = os.stat(
                self.letter.name, dir_fd=self.parent_fd, follow_symlinks=False
            )
        except OSError as exc:
            raise AdmissionRefused("letter changed after admission") from exc
        if not stat.S_ISREG(current.st_mode) or (
            current.st_dev,
            current.st_ino,
        ) != self.letter_identity:
            raise AdmissionRefused("letter changed after admission")


def admit_letter(
    checkout: str | Path, lane: str, letter: str | Path
) -> AdmittedLetter:
    """Validate and pin one registered-lane letter and same-checkout sender."""
    lane_name = _safe_identifier(lane, "lane identifier")
    checkout_path = _canonical_directory(Path(checkout), "checkout")
    projects = _projects_root(checkout_path)
    lane_project = projects / lane_name
    _require_plain_directory(lane_project, projects, "lane")
    _require_registered_lane(lane_project)
    inbox = lane_project / "inbox"
    _require_plain_directory(inbox, projects, "inbox")
    letter_path = _letter_path(letter, inbox, projects)
    parent_fd = _open_directory(letter_path.parent, inbox, "letter directory")
    try:
        letter_fd = _open_regular_at(parent_fd, letter_path.name)
        details = os.fstat(letter_fd)
        header = _read_descriptor(letter_fd, HEADER_BYTES).decode(
            "utf-8", errors="replace"
        )
        sender = _sender_from_header(header, letter_path)
        relative = letter_path.relative_to(inbox)
        if sender == "agents-daemon":
            generation_id, consumed = _admit_scheduled(
                checkout_path, lane_name, relative, letter_path, letter_fd
            )
            return AdmittedLetter(
                checkout_path,
                projects,
                lane_name,
                lane_project.resolve(),
                inbox.resolve(),
                letter_path,
                sender,
                None,
                letter_path.parent,
                parent_fd,
                letter_fd,
                (details.st_dev, details.st_ino),
                True,
                generation_id,
                consumed,
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
            letter_path.parent,
            parent_fd,
            letter_fd,
            (details.st_dev, details.st_ino),
        )
    except Exception:
        if "letter_fd" in locals():
            os.close(letter_fd)
        os.close(parent_fd)
        raise


def write_scheduled_letter(
    checkout: str | Path, lane: str, name: str, content: str
) -> Path:
    """Atomically create a daemon sweep through a validated inbox descriptor."""
    lane_name = _safe_identifier(lane, "lane identifier")
    checkout_path = _canonical_directory(Path(checkout), "checkout")
    projects = _projects_root(checkout_path)
    lane_project = projects / lane_name
    _require_plain_directory(lane_project, projects, "lane")
    _require_registered_lane(lane_project)
    inbox = lane_project / "inbox"
    _require_plain_directory(inbox, projects, "inbox")
    if Path(name).name != name or SCHEDULED_NAME.fullmatch(name) is None:
        raise AdmissionRefused("unsafe scheduled letter name")
    descriptor = _open_directory(inbox, projects, "inbox")
    try:
        _atomic_write_at(descriptor, name, content.encode())
    finally:
        os.close(descriptor)
    return inbox / name


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
    """Persist a one-shot receipt for the immutable scheduled request."""
    checkout_path = _canonical_directory(Path(checkout), "checkout")
    lane_name = _safe_identifier(lane, "lane identifier")
    letter_path = Path(letter)
    receipt = generation_receipt_path(checkout_path, lane_name, letter_path)
    state = checkout_path / ".magnolia-agents"
    _ensure_plain_directory(state, checkout_path, "daemon state directory")
    _ensure_plain_directory(receipt.parent, checkout_path, "generation receipt directory")
    payload = {
        "schema_version": RECEIPT_SCHEMA,
        "generation_id": uuid.uuid4().hex,
        "lane": lane_name,
        "letter": letter_path.name,
        "request_digest": _scheduled_request_digest(_read_safe_file(letter_path)),
        "consumed": False,
        "generated_at": datetime.now(timezone.utc).isoformat(),
    }
    _atomic_write_path(receipt, json.dumps(payload, sort_keys=True) + "\n")
    return receipt


def consume_generation_receipt(admitted: AdmittedLetter) -> None:
    """Mark one admitted scheduled generation consumed exactly once."""
    if not admitted.is_scheduled or admitted.generation_id is None:
        return
    receipt = generation_receipt_path(admitted.checkout, admitted.lane, admitted.letter)
    payload = _read_receipt(receipt, admitted.checkout)
    if payload.get("generation_id") != admitted.generation_id:
        raise AdmissionRefused("scheduled generation changed after admission")
    payload["consumed"] = True
    payload["consumed_at"] = datetime.now(timezone.utc).isoformat()
    _atomic_write_path(receipt, json.dumps(payload, sort_keys=True) + "\n")
    admitted.generation_consumed = True


def _safe_identifier(value: Any, label: str, letter: Path | None = None) -> str:
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
    if path.parent != boundary:
        _reject_symlinks(path.parent, boundary, label)
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
    absolute = Path(os.path.abspath(os.fspath(Path(letter))))
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
    return absolute


def _sender_from_header(header: str, letter: Path) -> str:
    senders = [
        match.group(1).strip()
        for line in header.splitlines()[:HEADER_LINES]
        if (match := FROM_HEADER.match(line))
    ]
    if len(senders) != 1:
        raise AdmissionRefused(
            "letter header must contain exactly one from: identity",
            letter=letter,
            can_mark=True,
        )
    return senders[0]


def _admit_scheduled(
    checkout: Path, lane: str, relative: Path, letter: Path, letter_fd: int
) -> tuple[str, bool]:
    match = SCHEDULED_NAME.fullmatch(letter.name)
    if relative.parent != Path(".") or match is None or match.group(1) != lane:
        raise AdmissionRefused(
            "agents-daemon sender is restricted to the scheduled letter path",
            letter=letter,
            can_mark=True,
        )
    receipt = generation_receipt_path(checkout, lane, letter)
    payload = _read_receipt(receipt, checkout)
    if (
        payload.get("schema_version") != RECEIPT_SCHEMA
        or payload.get("lane") != lane
        or payload.get("letter") != letter.name
    ):
        raise AdmissionRefused(
            "scheduled generation receipt does not match lane and letter",
            letter=letter,
            can_mark=True,
        )
    digest = _scheduled_request_digest(_read_descriptor(letter_fd))
    if payload.get("request_digest") != digest:
        raise AdmissionRefused(
            "scheduled generation receipt request digest does not match",
            letter=letter,
            can_mark=True,
        )
    generation_id = payload.get("generation_id")
    if not isinstance(generation_id, str) or not generation_id:
        raise AdmissionRefused("scheduled generation receipt has no generation id")
    return generation_id, payload.get("consumed") is True


def _read_receipt(path: Path, checkout: Path) -> dict[str, Any]:
    _reject_symlinks(path, checkout, "generation receipt")
    try:
        payload = json.loads(_read_safe_file(path).decode("utf-8"))
    except (OSError, json.JSONDecodeError, AdmissionRefused) as exc:
        raise AdmissionRefused("scheduled letter has no valid generation receipt") from exc
    if not isinstance(payload, dict):
        raise AdmissionRefused("scheduled generation receipt is invalid")
    return payload


def _scheduled_request_digest(content: bytes) -> str:
    text = content.decode("utf-8", errors="replace")
    lines = text.splitlines(keepends=True)
    request_lines = []
    for line in lines:
        if JOB_RESULT_HEADER.match(line.rstrip("\r\n")):
            break
        request_lines.append(line)
    immutable = "".join(
        line
        for index, line in enumerate(request_lines)
        if index >= HEADER_LINES
        or (
            STATUS_HEADER.match(line.rstrip("\r\n")) is None
            and WAIT_FOR_HEADER.match(line.rstrip("\r\n")) is None
        )
    ).rstrip("\r\n") + "\n"
    return hashlib.sha256(immutable.encode("utf-8")).hexdigest()


def _open_directory(path: Path, boundary: Path, label: str) -> int:
    _require_plain_directory(path, boundary, label)
    flags = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0) | getattr(os, "O_NOFOLLOW", 0)
    descriptor = -1
    try:
        descriptor = os.open(path, flags)
        current = path.stat(follow_symlinks=False)
        pinned = os.fstat(descriptor)
    except OSError as exc:
        if descriptor >= 0:
            os.close(descriptor)
        raise AdmissionRefused(f"{label} cannot be opened safely") from exc
    if (current.st_dev, current.st_ino) != (pinned.st_dev, pinned.st_ino):
        os.close(descriptor)
        raise AdmissionRefused(f"{label} changed during admission")
    return descriptor


def _open_regular_at(directory_fd: int, name: str) -> int:
    flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0)
    try:
        descriptor = os.open(name, flags, dir_fd=directory_fd)
        if not stat.S_ISREG(os.fstat(descriptor).st_mode):
            os.close(descriptor)
            raise AdmissionRefused("letter is not a regular file")
        return descriptor
    except OSError as exc:
        raise AdmissionRefused("letter cannot be opened safely") from exc


def _read_descriptor(descriptor: int, limit: int | None = None) -> bytes:
    offset = 0
    chunks: list[bytes] = []
    while limit is None or offset < limit:
        size = 1024 * 1024 if limit is None else min(1024 * 1024, limit - offset)
        block = os.pread(descriptor, size, offset)
        if not block:
            break
        chunks.append(block)
        offset += len(block)
    return b"".join(chunks)


def _read_safe_file(path: Path) -> bytes:
    flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0)
    descriptor = -1
    try:
        descriptor = os.open(path, flags)
        if not stat.S_ISREG(os.fstat(descriptor).st_mode):
            raise AdmissionRefused("file is not regular")
        return _read_descriptor(descriptor)
    except OSError as exc:
        raise AdmissionRefused("file cannot be read safely") from exc
    finally:
        if descriptor >= 0:
            os.close(descriptor)


def _check_replace_target(directory_fd: int, name: str, replace_existing: bool) -> None:
    try:
        details = os.stat(name, dir_fd=directory_fd, follow_symlinks=False)
    except FileNotFoundError:
        return
    if not stat.S_ISREG(details.st_mode):
        raise AdmissionRefused("reply target is not a regular file")
    if not replace_existing:
        raise AdmissionRefused("reply already exists")


def _atomic_write_at(directory_fd: int, name: str, content: bytes) -> None:
    temporary = f".{name}.{uuid.uuid4().hex}.tmp"
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0)
    descriptor = -1
    try:
        try:
            current = os.stat(name, dir_fd=directory_fd, follow_symlinks=False)
            preserved_mode = stat.S_IMODE(current.st_mode) if stat.S_ISREG(current.st_mode) else None
        except FileNotFoundError:
            preserved_mode = None
        descriptor = os.open(
            temporary,
            flags,
            preserved_mode if preserved_mode is not None else 0o666,
            dir_fd=directory_fd,
        )
        if preserved_mode is not None:
            os.fchmod(descriptor, preserved_mode)
        view = memoryview(content)
        while view:
            written = os.write(descriptor, view)
            if written <= 0:
                raise OSError("short atomic write")
            view = view[written:]
        os.fsync(descriptor)
        os.close(descriptor)
        descriptor = -1
        os.replace(temporary, name, src_dir_fd=directory_fd, dst_dir_fd=directory_fd)
    finally:
        if descriptor >= 0:
            os.close(descriptor)
        try:
            os.unlink(temporary, dir_fd=directory_fd)
        except FileNotFoundError:
            pass


def _atomic_write_path(path: Path, text: str) -> None:
    descriptor, temporary = tempfile.mkstemp(prefix=f".{path.name}.", dir=str(path.parent))
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
            handle.write(text)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
    finally:
        try:
            os.unlink(temporary)
        except FileNotFoundError:
            pass


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


if __name__ == "__main__":
    raise SystemExit(_setup_probe_main(sys.argv[1:]))
