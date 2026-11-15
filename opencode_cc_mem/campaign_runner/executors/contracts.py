"""Typed execution, outcome, identity and usage contracts."""

from __future__ import annotations

from dataclasses import dataclass, field, fields
from datetime import datetime, timezone
from pathlib import Path
from typing import TYPE_CHECKING, Any, Mapping

if TYPE_CHECKING:
    from campaign_runner.routing import ResolvedRoute


@dataclass
class LimitInfo:
    reason: str
    reset_at: datetime | None
    evidence_source: str

    def __post_init__(self) -> None:
        if self.reset_at is not None and (
            self.reset_at.tzinfo is None or self.reset_at.utcoffset() is None
        ):
            raise ValueError("limit reset_at must be timezone-aware UTC")
        if self.reset_at is not None:
            self.reset_at = self.reset_at.astimezone(timezone.utc)


@dataclass
class RunOutcome:
    kind: str
    limit: LimitInfo | None = None

    def __post_init__(self) -> None:
        if self.kind not in {"success", "failure", "timeout", "cancelled", "quota", "refused"}:
            raise ValueError(f"unknown run outcome: {self.kind}")
        if self.kind == "quota" and self.limit is None:
            self.limit = LimitInfo("quota or rate limit", None, "unspecified")

    @property
    def overrides_repair(self) -> bool:
        return self.kind == "quota"


@dataclass(frozen=True)
class ExecContext:
    route: "ResolvedRoute | None"
    role: str
    project_dir: Path | None = None
    run_dir: Path | None = None
    timeout_seconds: int = 1800
    role_cap: int | None = None
    opencode_binary: str | None = None
    opencode_server: str | None = None
    opencode_db: str | None = None
    agent: str = "worker"
    role_capabilities: Mapping[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class SessionIdentity:
    client: str
    model: str | None
    session_id: str
    project_digest: str
    role: str
    policy_digest: str

    @property
    def hook_harness(self) -> str:
        return "claude" if self.client == "claude-code" else self.client

    def transcript_ref(self) -> dict[str, str]:
        """The harness-qualified identity required by the existing SessionRef shape."""
        return {"harness": self.client, "session_id": self.session_id}

    def permits_resume(self, other: "SessionIdentity") -> bool:
        return (
            self.client,
            self.model,
            self.session_id,
            self.project_digest,
            self.role,
            self.policy_digest,
        ) == (
            other.client,
            other.model,
            other.session_id,
            other.project_digest,
            other.role,
            other.policy_digest,
        )


def _number(value: Any) -> float | int | None:
    return value if isinstance(value, (int, float)) and not isinstance(value, bool) else None


@dataclass
class UsageRecord:
    schema_version: str = "magnolia-usage-v1"
    invocation_id: str | None = None
    parent_run_id: str | None = None
    started_at: datetime | None = None
    ended_at: datetime | None = None
    client: str | None = None
    resolved_model: str | None = None
    reported_model: str | None = None
    subscription_key: str | None = None
    run_kind: str | None = None
    project_digest: str | None = None
    session_identity: SessionIdentity | None = None
    turns: int | None = None
    input_tokens: float | int | None = None
    output_tokens: float | int | None = None
    reasoning_tokens: float | int | None = None
    cached_tokens: float | int | None = None
    cache_write_tokens: float | int | None = None
    cost: float | int | None = None
    duration_seconds: float | int | None = None
    outcome: RunOutcome | None = None
    source: str = "unknown"
    completeness: str = "unknown"
    raw: Mapping[str, Any] | None = field(default=None, repr=False)

    @classmethod
    def from_raw(
        cls,
        raw: Mapping[str, Any],
        *,
        baseline: "UsageRecord | None" = None,
        session_identity: SessionIdentity | None = None,
        **metadata: Any,
    ) -> "UsageRecord":
        """Normalize cumulative raw usage; OpenCode input includes cached input."""
        tokens = raw.get("tokens") if isinstance(raw.get("tokens"), Mapping) else raw
        cache = tokens.get("cache") if isinstance(tokens.get("cache"), Mapping) else {}
        counters = {
            "input_tokens": _number(tokens.get("input", tokens.get("input_tokens"))),
            "output_tokens": _number(tokens.get("output", tokens.get("output_tokens"))),
            "reasoning_tokens": _number(tokens.get("reasoning", tokens.get("reasoning_tokens"))),
            "cached_tokens": _number(cache.get("read", tokens.get("cached_tokens"))),
            "cache_write_tokens": _number(cache.get("write", tokens.get("cache_write_tokens"))),
            "cost": _number(raw.get("cost", raw.get("cost_usd"))),
        }
        if baseline is not None:
            for key, value in tuple(counters.items()):
                previous = getattr(baseline, key)
                counters[key] = value - previous if value is not None and previous is not None else None
        known = [key for key in counters if key != "cost"]
        completeness = "complete" if all(counters[key] is not None for key in known) else "partial"
        values = {
            key: item.default
            for key, item in ((item.name, item) for item in fields(cls))
            if key in metadata
        }
        values.update({key: value for key, value in metadata.items() if key in values})
        values.update(counters)
        values["source"] = str(raw.get("schema_version", "unknown"))
        values["completeness"] = completeness
        values["raw"] = raw
        if session_identity is not None:
            values.update(
                client=session_identity.client,
                resolved_model=session_identity.model,
                project_digest=session_identity.project_digest,
                session_identity=session_identity,
            )
        return cls(**values)

    def to_dict(self) -> dict[str, Any]:
        values = {
            item.name: getattr(self, item.name)
            for item in fields(self)
            if item.name != "raw"
        }
        values["started_at"] = self.started_at.isoformat() if self.started_at else None
        values["ended_at"] = self.ended_at.isoformat() if self.ended_at else None
        if self.session_identity:
            values["session_identity"] = {
                item.name: getattr(self.session_identity, item.name)
                for item in fields(SessionIdentity)
            }
        if self.outcome:
            values["outcome"] = {
                "kind": self.outcome.kind,
                "limit": (
                    {
                        "reason": self.outcome.limit.reason,
                        "reset_at": self.outcome.limit.reset_at.isoformat()
                        if self.outcome.limit.reset_at
                        else None,
                        "evidence_source": self.outcome.limit.evidence_source,
                    }
                    if self.outcome.limit
                    else None
                ),
            }
        return values
