"""Bounded per-session retry timing for memory extraction."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any, Mapping


MAX_EXTRACTION_ATTEMPTS = 5
BASE_RETRY_SECONDS = 60
MAX_RETRY_SECONDS = 3600


def retry_is_due(
    retry: Mapping[str, Any] | None, *, now: datetime | None = None
) -> bool:
    """Return whether a failed session may make its next extraction attempt."""
    if not retry:
        return True
    if retry.get("exhausted") is True:
        return False
    raw = retry.get("next_retry_at")
    if not isinstance(raw, str) or not raw:
        return True
    try:
        due = datetime.fromisoformat(raw)
    except ValueError:
        return True
    if due.tzinfo is None or due.utcoffset() is None:
        return True
    current = now or datetime.now(timezone.utc)
    return current.astimezone(timezone.utc) >= due.astimezone(timezone.utc)


def next_retry(
    retry: Mapping[str, Any] | None, *, now: datetime | None = None
) -> dict[str, Any]:
    """Increment one session's retry state with capped exponential backoff."""
    prior = retry.get("attempts", 0) if retry else 0
    attempts = int(prior) + 1 if isinstance(prior, int) and prior >= 0 else 1
    exhausted = attempts >= MAX_EXTRACTION_ATTEMPTS
    current = (now or datetime.now(timezone.utc)).astimezone(timezone.utc)
    delay = min(BASE_RETRY_SECONDS * 2 ** (attempts - 1), MAX_RETRY_SECONDS)
    return {
        "attempts": attempts,
        "next_retry_at": None if exhausted else (current + timedelta(seconds=delay)).isoformat(),
        "exhausted": exhausted,
    }
