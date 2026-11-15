"""Turn ``@agent <request>`` mentions into inbox letters for the agents daemon.

Ported from ``.opencode/plugins/magnolia-agent-doorbell.ts``. A mention of a
registered helper agent is replaced by a short note and the rest of the line
becomes ``projects/<agent>/inbox/from-<source>/<date>_<slug>_<hhmmss>.task.md``
with ``status: open``. The daemon picks the letter up and runs it headlessly.

The mention set is the default pair plus every ``projects/<name>/agent.json``
that does not say ``enabled: false``. ``MAGNOLIA_DOORBELL=0`` disables the
doorbell. Delivery failures are swallowed: a doorbell must never break a
session. Like the plugin, delivery appends the complete letter in one write so
colliding filenames retain every request.
"""

from __future__ import annotations

import json
import os
import re
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Mapping

from compchem_memory.hook.project import ProjectPaths
from compchem_memory.hook.state import private_mkdir

DEFAULT_AGENTS = ("literature", "xiulian")
_REPLACEMENT = "[→ sent to @{agent}; the agents daemon will run it]"
_FALLBACK_REQUEST = "(see attached session context; ask the sender if unclear)"


@dataclass
class DoorbellResult:
    """The rewritten prompt and the letters written for it."""

    text: str
    sent: list[str]
    letters: list[Path]


def doorbell_disabled(env: Mapping[str, str] | None = None) -> bool:
    """Return whether ``MAGNOLIA_DOORBELL`` is exactly ``0`` (case-insensitive)."""
    source = os.environ if env is None else env
    return str(source.get("MAGNOLIA_DOORBELL", "")).lower() == "0"


def registry_agents(root: Path) -> list[str]:
    """Return the default agents plus every enabled project-declared agent."""
    names = list(DEFAULT_AGENTS)
    try:
        entries = (root / "projects").iterdir()
    except OSError:
        return names
    for entry in entries:
        if not entry.is_dir():
            continue
        config_path = entry / "agent.json"
        if not config_path.exists():
            continue
        try:
            config = json.loads(config_path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        if isinstance(config, dict) and config.get("enabled") is False:
            continue
        if entry.name not in names:
            names.append(entry.name)
    return names


def _mention_patterns(agents: list[str]) -> tuple[re.Pattern[str], re.Pattern[str]]:
    alternatives = "|".join(re.escape(agent) for agent in agents)
    return (
        re.compile(rf"@({alternatives})\b[,:]?\s*([^\n]*)", re.IGNORECASE | re.ASCII),
        re.compile(rf"@({alternatives})\b", re.IGNORECASE | re.ASCII),
    )


def _slug(request: str, now: datetime) -> str:
    stamp = now.strftime("%Y-%m-%dT%H:%M:%S.") + f"{now.microsecond // 1000:03d}Z"
    words = re.sub(r"[^a-z0-9]+", " ", request.lower()).strip().split()
    slug_words = "-".join(words[:4])[:40] or "request"
    return f"{stamp[:10]}_{slug_words}_{stamp[11:19].replace(':', '')}"


def _title(request: str) -> str:
    """Return a single safe line for the letter's task heading."""
    return re.sub(r"[\x00-\x1f\x7f-\x9f\u2028\u2029]+", " ", request).strip()[:80]


def _letter(request: str, source: str, now: datetime) -> str:
    stamp = now.strftime("%Y-%m-%dT%H:%M:%S.") + f"{now.microsecond // 1000:03d}Z"
    return "\n".join(
        [
            f"# Task: {_title(request)}",
            "",
            f"- from: {source}",
            f"- date: {stamp}",
            "- status: open",
            "",
            "## Request",
            "",
            request.strip() or _FALLBACK_REQUEST,
            "",
            "",
        ]
    )


def _append_letter(path: Path, content: str) -> None:
    private_mkdir(path.parent)
    descriptor = os.open(path, os.O_APPEND | os.O_CREAT | os.O_WRONLY, 0o600)
    with os.fdopen(descriptor, "wb", closefd=True) as letter:
        os.write(letter.fileno(), content.encode("utf-8"))


def _deliver(paths: ProjectPaths, agent: str, request: str, now: datetime) -> Path:
    directory = paths.root / "projects" / agent / "inbox" / f"from-{paths.name}"
    letter = directory / f"{_slug(request, now)}.task.md"
    _append_letter(letter, _letter(request, paths.name, now))
    return letter


def process_prompt(
    text: str,
    paths: ProjectPaths,
    env: Mapping[str, str] | None = None,
) -> DoorbellResult:
    """Rewrite agent mentions and write one letter per delivered request."""
    if doorbell_disabled(env):
        return DoorbellResult(text, [], [])
    mention, check = _mention_patterns(registry_agents(paths.root))
    if not check.search(text):
        return DoorbellResult(text, [], [])
    now = datetime.now(timezone.utc)
    sent: list[str] = []
    letters: list[Path] = []
    delivered: set[str] = set()

    def replace(match: re.Match[str]) -> str:
        agent, request = match.group(1), (match.group(2) or "").strip()
        if not request:
            return match.group(0)
        target = agent.lower()
        if target in delivered:
            return _REPLACEMENT.format(agent=agent)
        try:
            letters.append(_deliver(paths, target, request, now))
        except OSError:
            return match.group(0)
        delivered.add(target)
        sent.append(f"@{agent}")
        return _REPLACEMENT.format(agent=agent)

    rewritten = mention.sub(replace, text)
    return DoorbellResult(rewritten if sent else text, sent, letters)
