"""Path and secret-safety helpers."""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any, Iterable


_SECRET_KEY_TEXT = (
    r"(?:API[_-]?KEY|APIKEY|TOKEN|SECRET|PASSWORD|"
    r"[A-Z0-9_.-]*[_-](?:API[_-]?KEY|TOKEN|SECRET|PASSWORD)"
    r"(?:[_-][A-Z0-9_]+)*)"
)
_SECRET_ASSIGNMENT = (
    rf"(?i)(?P<prefix>[\"']?{_SECRET_KEY_TEXT}[\"']?\s*[:=]\s*)"
)
_SECRET_PATTERNS = (
    re.compile(r"\bsk-[A-Za-z0-9_-]{12,}\b"),
    re.compile(r"\bolp_[A-Za-z0-9]{12,}\b"),
    re.compile(r"(?i)\bBearer\s+[A-Za-z0-9._~+/=-]{12,}"),
    re.compile(
        _SECRET_ASSIGNMENT
        + r"(?P<quote>[\"'])(?P<value>[^\"']{8,})(?P=quote)"
    ),
    re.compile(
        _SECRET_ASSIGNMENT
        + r"(?P<quote>[\"']?)(?P<value>[^\s'\",;]{8,})(?P=quote)"
    ),
)
def _secret_key(key: str) -> bool:
    lowered = key.lower()
    if lowered in {
        "token",
        "secret",
        "password",
        "api_key",
        "apikey",
        "access_token",
        "refresh_token",
        "auth_token",
        "bearer_token",
    }:
        return True
    if "api_key" in lowered or "apikey" in lowered:
        return True
    return lowered.endswith(("_token", "_secret", "_password"))


class SecurityError(Exception):
    """Raised when a path or value violates a campaign safety rule."""


def ensure_within(root: str | Path, path: str | Path) -> Path:
    base = Path(root).resolve()
    candidate = Path(path)
    resolved = candidate.resolve() if candidate.is_absolute() else (base / candidate).resolve()
    try:
        resolved.relative_to(base)
    except ValueError as exc:
        raise SecurityError(f"path escapes allowed root: {path}") from exc
    return resolved


def redact_secrets(text: str) -> str:
    redacted = text
    for pattern in _SECRET_PATTERNS:
        redacted = pattern.sub(_redaction, redacted)
    return redacted


def _redaction(match: re.Match[str]) -> str:
    groups = match.groupdict()
    if "prefix" not in groups:
        return "<redacted>"
    quote = groups.get("quote") or ""
    return f"{match.group('prefix')}{quote}<redacted>{quote}"


def redact_structure(value: Any) -> Any:
    if isinstance(value, str):
        return redact_secrets(value)
    if isinstance(value, list):
        return [redact_structure(item) for item in value]
    if isinstance(value, dict):
        return {
            key: (
                "<redacted>"
                if isinstance(key, str) and _secret_key(key) and item not in (None, "")
                else redact_structure(item)
            )
            for key, item in value.items()
        }
    return value


def redact_file(path: str | Path) -> bool:
    file_path = Path(path)
    try:
        original = file_path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return False
    redacted = redact_secrets(original)
    if redacted == original:
        return False
    temporary = file_path.with_name(f".{file_path.name}.redact.tmp")
    temporary.write_text(redacted, encoding="utf-8")
    temporary.replace(file_path)
    return True


def redact_paths(paths: Iterable[str | Path]) -> int:
    changed = 0
    for path in paths:
        if redact_file(path):
            changed += 1
    return changed
