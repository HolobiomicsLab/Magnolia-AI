"""Path and secret-safety helpers."""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any


_SECRET_PATTERNS = (
    re.compile(r"\bsk-[A-Za-z0-9_-]{12,}\b"),
    re.compile(r"\bolp_[A-Za-z0-9]{12,}\b"),
    re.compile(r"(?i)\b(api[_-]?key|token|secret)\s*[:=]\s*[^\s,;]+"),
    re.compile(r"\b[A-Fa-f0-9]{32,}\b"),
)


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
        redacted = pattern.sub("<redacted>", redacted)
    return redacted


def redact_structure(value: Any) -> Any:
    if isinstance(value, str):
        return redact_secrets(value)
    if isinstance(value, list):
        return [redact_structure(item) for item in value]
    if isinstance(value, dict):
        return {key: redact_structure(item) for key, item in value.items()}
    return value
