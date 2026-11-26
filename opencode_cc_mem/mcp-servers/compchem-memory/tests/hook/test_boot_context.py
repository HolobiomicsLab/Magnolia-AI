"""Boot payload assembly from the resolved project paths."""

from pathlib import Path

from compchem_memory.hook.boot_context import MAX_PAYLOAD_CHARS, build_boot_payload
from compchem_memory.hook.project import ProjectPaths


def _project(tmp_path: Path, name: str = "alpha") -> ProjectPaths:
    project_dir = tmp_path / "projects" / name
    store = project_dir / ".magnolia"
    store.mkdir(parents=True)
    return ProjectPaths(tmp_path, project_dir, store, name, False)


def test_boot_payload_headers_sections_and_rule_order(tmp_path):
    paths = _project(tmp_path)
    (paths.store / "boot-context.md").write_text("\nBoot text\n", encoding="utf-8")
    (paths.store / "audit-report.md").write_text("Audit text\n\n", encoding="utf-8")
    rules = tmp_path / "rules"
    rules.mkdir()
    (rules / "z-last.md").write_text("Last rule\n", encoding="utf-8")
    (rules / "a-first.md").write_text("\nFirst rule\n", encoding="utf-8")
    (rules / "ignored.txt").write_text("Not a rule", encoding="utf-8")

    assert build_boot_payload(paths) == "\n\n".join(
        [
            "<!-- magnolia-boot-context:alpha -->",
            "[MAGNOLIA BOOT CONTEXT — project alpha]",
            "--- boot-context.md ---\nBoot text",
            "--- audit-report.md ---\nAudit text",
            "--- rules/a-first.md ---\nFirst rule",
            "--- rules/z-last.md ---\nLast rule",
        ]
    )


def test_boot_payload_omits_empty_sections_and_missing_rules(tmp_path):
    paths = _project(tmp_path)
    (paths.store / "boot-context.md").write_text(" \n\t", encoding="utf-8")

    assert build_boot_payload(paths) == (
        "<!-- magnolia-boot-context:alpha -->\n\n"
        "[MAGNOLIA BOOT CONTEXT — project alpha]"
    )


def test_boot_payload_truncates_prefix_before_adding_suffix(tmp_path):
    paths = _project(tmp_path)
    (paths.store / "boot-context.md").write_text("x" * MAX_PAYLOAD_CHARS, encoding="utf-8")

    payload = build_boot_payload(paths)

    assert len(payload) == MAX_PAYLOAD_CHARS + len("\n\n[truncated at 256k chars]")
    assert payload.endswith("\n\n[truncated at 256k chars]")
    assert build_boot_payload(paths)[:MAX_PAYLOAD_CHARS] == payload[:MAX_PAYLOAD_CHARS]


def test_agents_md_first_when_requested_and_fail_open(tmp_path):
    paths = _project(tmp_path)
    (paths.store / "boot-context.md").write_text("Boot text", encoding="utf-8")
    default = build_boot_payload(paths)
    assert build_boot_payload(paths, include_agents_md=True) == default  # no AGENTS.md: unchanged
    (tmp_path / "AGENTS.md").write_text("Agents rules\n", encoding="utf-8")
    assert build_boot_payload(paths) == default
    payload = build_boot_payload(paths, include_agents_md=True)
    sections = payload.split("\n\n")
    assert sections[2] == "--- AGENTS.md ---\nAgents rules"
    assert sections[3] == "--- boot-context.md ---\nBoot text"


def test_vibe_boot_rules_use_vibe_tool_names_and_drop_cross_client_paragraph(tmp_path):
    paths = _project(tmp_path)
    (tmp_path / "AGENTS.md").write_text(
        "Use compchem-tools_run_shell and compchem-memory_memory_search.\n\n"
        "## Tool names in other clients\nIgnore this cross-client explanation.\n\n"
        "## Keep\nStill here.\n",
        encoding="utf-8",
    )

    payload = build_boot_payload(paths, include_agents_md=True, harness="vibe")

    assert "mcp_compchem-tools.run_shell" in payload
    assert "mcp_compchem-memory.memory_search" in payload
    assert "Tool names in other clients" not in payload
    assert "## Keep" in payload
