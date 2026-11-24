"""Campaign agents must not read service credentials through any file tool."""

from fnmatch import fnmatchcase
from pathlib import Path

import pytest
import yaml

AGENTS = (
    "campaign-worker",
    "campaign-reviewer",
)
ACTIONS = ("read", "glob", "grep")
SERVICE_FILES = (
    ".magnolia/campaign-service.json",
    ".magnolia/campaign-service.log",
    "projects/demo/.magnolia/campaign-service.json",
    "projects/demo/.magnolia/campaign-service.log",
)


def permissions(agent: str) -> list[dict]:
    path = Path(__file__).parents[1] / ".opencode" / "agents" / f"{agent}.md"
    frontmatter = path.read_text(encoding="utf-8").split("---", 2)[1]
    return yaml.safe_load(frontmatter)["permissions"]


def effect(rules: list[dict], action: str, resource: str) -> str:
    """Apply matching rules in order, with the default deny as a backstop."""
    matching = [
        rule for rule in rules
        if rule["action"] in (action, "*") and fnmatchcase(resource, rule["resource"])
    ]
    return matching[-1]["effect"] if matching else "deny"


@pytest.mark.parametrize("agent", AGENTS)
@pytest.mark.parametrize("action", ACTIONS)
@pytest.mark.parametrize("resource", SERVICE_FILES)
def test_service_files_are_denied_for_every_file_read_action(agent, action, resource):
    assert effect(permissions(agent), action, resource) == "deny"


@pytest.mark.parametrize("agent", AGENTS)
@pytest.mark.parametrize("action", ACTIONS)
def test_unrelated_project_files_remain_readable(agent, action):
    assert effect(permissions(agent), action, "projects/demo/results/result.yaml") == "allow"
