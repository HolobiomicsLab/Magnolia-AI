"""Registered OpenCode recipes that preserve each legacy caller contract."""

from __future__ import annotations

import json
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Mapping, Sequence

from campaign_runner.service import ensure_service, merge_agent_steps


def _effective_cap(role_cap: int | None, governor_cap: int | None) -> int | None:
    caps = [cap for cap in (role_cap, governor_cap) if cap is not None]
    return min(caps) if caps else None


def _cap_environment(
    environment: Mapping[str, str], agent: str, cap: int | None
) -> dict[str, str]:
    updated = dict(environment)
    if cap is not None:
        updated["OPENCODE_CONFIG_CONTENT"] = merge_agent_steps(
            updated.get("OPENCODE_CONFIG_CONTENT"), {agent: cap}
        )
    return updated


@dataclass(frozen=True)
class OpenCodeHelperExecutor:
    """The daemon's JSON-event recipe, without campaign-only flags."""

    binary: str
    model: str
    role_cap: int = 40
    governor_cap: int | None = None

    @property
    def effective_cap(self) -> int | None:
        """Return the governed helper cap, or no delta when ungoverned."""
        if self.governor_cap is None:
            return None
        return _effective_cap(self.role_cap, self.governor_cap)

    def argv(self, extra_args: Sequence[str]) -> list[str]:
        """Build the legacy helper argv without campaign-only flags."""
        return [
            self.binary,
            "run",
            "--model",
            self.model,
            "--format",
            "json",
            *extra_args,
        ]

    def environment_with_cap(self, environment: Mapping[str, str]) -> dict[str, str]:
        """Add only the governed default-agent step cap."""
        return _cap_environment(environment, "build", self.effective_cap)

    def run(
        self,
        extra_args: Sequence[str],
        *,
        cwd: str | Path,
        environment: Mapping[str, str],
        timeout_seconds: int,
    ) -> subprocess.CompletedProcess[str]:
        """Run one legacy helper invocation and return its plain process result."""
        return subprocess.run(
            self.argv(extra_args),
            cwd=str(cwd),
            env=self.environment_with_cap(environment),
            timeout=timeout_seconds,
            capture_output=True,
            text=True,
        )


@dataclass(frozen=True)
class OpenCodeEvaluatorExecutor:
    """The standalone evaluator/catalog recipe with plain stdout."""

    binary: str
    model: str
    db_path: str | None = None
    role_cap: int = 1
    governor_cap: int | None = None

    @property
    def effective_cap(self) -> int | None:
        """Return the governed evaluator cap, or no delta when ungoverned."""
        if self.governor_cap is None:
            return None
        return _effective_cap(self.role_cap, self.governor_cap)

    def run_argv(self, prompt: str) -> list[str]:
        """Build the standalone generative-evaluator argv."""
        return [self.binary, "--standalone", "run", "--model", self.model, prompt]

    def catalog_argv(self) -> list[str]:
        """Build the standalone model-catalog argv."""
        return [self.binary, "--standalone", "api", "get", "/api/model"]

    def environment_with_cap(self, environment: Mapping[str, str]) -> dict[str, str]:
        """Add the private database and governed default-agent cap."""
        updated = _cap_environment(environment, "build", self.effective_cap)
        if self.db_path:
            updated["OPENCODE_DB"] = self.db_path
        return updated

    def run(
        self, prompt: str, *, environment: Mapping[str, str], timeout_seconds: int
    ) -> subprocess.CompletedProcess[str]:
        """Run the evaluator while retaining its plain-stdout contract."""
        return subprocess.run(
            self.run_argv(prompt),
            env=self.environment_with_cap(environment),
            stdin=subprocess.DEVNULL,
            capture_output=True,
            text=True,
            timeout=timeout_seconds,
            check=False,
        )

    def catalog(
        self, *, environment: Mapping[str, str], timeout_seconds: int
    ) -> subprocess.CompletedProcess[str]:
        """Query the evaluator's private standalone model catalog."""
        return subprocess.run(
            self.catalog_argv(),
            env=self.environment_with_cap(environment),
            capture_output=True,
            text=True,
            timeout=timeout_seconds,
            check=False,
        )


@dataclass(frozen=True)
class OpenCodeServiceExecutor:
    """The dedicated-service startup recipe and cap renderer."""

    binary: str
    db_path: str | None
    project_dir: Path | None
    governor_cap: int | None = None

    def argv(self, port: int) -> list[str]:
        """Build the dedicated-service startup argv."""
        return [
            self.binary,
            "serve",
            "--hostname",
            "127.0.0.1",
            "--port",
            str(port),
        ]

    def config_content(
        self, config_content: str | None, steps: Mapping[str, int]
    ) -> str:
        """Merge named startup steps after applying the governor ceiling."""
        effective = {
            name: min(limit, self.governor_cap)
            if self.governor_cap is not None
            else limit
            for name, limit in steps.items()
        }
        return merge_agent_steps(config_content, effective)

    def config_with_caps(self, config_content: str | None) -> str:
        """Clamp every configured agent step limit to the governor cap."""
        try:
            value = json.loads(config_content or "{}")
        except (TypeError, ValueError):
            value = {}
        agents = value.get("agents") if isinstance(value, dict) else None
        if not isinstance(agents, dict):
            agents = {}
        steps = {
            str(name): entry["steps"]
            for name, entry in agents.items()
            if isinstance(entry, dict)
            and isinstance(entry.get("steps"), int)
            and not isinstance(entry["steps"], bool)
            and entry["steps"] > 0
        }
        return self.config_content(config_content, steps)

    def ensure(
        self,
        *,
        config_content: str | None,
        required_agents: Sequence[str],
        timeout_seconds: float = 30.0,
    ) -> dict[str, object]:
        """Start or verify the managed service without replacing active work."""
        if self.project_dir is None or self.db_path is None:
            raise ValueError("service recipe requires project_dir and db_path")
        effective_config = (
            self.config_with_caps(config_content)
            if self.governor_cap is not None
            else config_content
        )
        return ensure_service(
            self.project_dir,
            self.binary,
            self.db_path,
            timeout_seconds=timeout_seconds,
            config_content=effective_config,
            required_agents=required_agents,
        )
