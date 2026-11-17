"""Byte parity for boot-context decoding and UTF-16 truncation edges."""

import json
from pathlib import Path

import pytest

from compchem_memory.hook.boot_context import MAX_PAYLOAD_CHARS, build_boot_payload
from compchem_memory.hook.project import ProjectPaths
from .test_parity_plugins import BOOT_DRIVER, NODE, run_plugin

pytestmark = pytest.mark.skipif(NODE is None, reason="node is not installed")


def _workspace(tmp_path: Path) -> ProjectPaths:
    project_dir = tmp_path / "projects" / "alpha"
    store = project_dir / ".magnolia"
    store.mkdir(parents=True)
    (tmp_path / ".magnolia").mkdir()
    (tmp_path / ".magnolia" / ".active-project").write_text("alpha\n", encoding="utf-8")
    (store / "audit-report.md").write_text("Audit text\n", encoding="utf-8")
    (tmp_path / "rules").mkdir()
    return ProjectPaths(tmp_path, project_dir, store, "alpha", False)


def _plugin_payload(tmp_path: Path) -> str:
    return json.loads(run_plugin("magnolia-boot-context.ts", BOOT_DRIVER, tmp_path, tmp_path))[0]


def test_crlf_rules_file_matches_plugin(tmp_path):
    paths = _workspace(tmp_path)
    (paths.store / "boot-context.md").write_bytes(b"Boot text\r\n")
    (tmp_path / "rules" / "crlf.md").write_bytes(b"first\r\nsecond\r\n")

    assert build_boot_payload(paths) == _plugin_payload(tmp_path)


def test_invalid_utf8_matches_plugin_replacement(tmp_path):
    paths = _workspace(tmp_path)
    (paths.store / "boot-context.md").write_bytes(b"before\xffafter\n")

    assert build_boot_payload(paths) == _plugin_payload(tmp_path)


@pytest.mark.parametrize("split_pair", [False, True], ids=["after-emoji", "split-surrogate-pair"])
def test_utf16_truncation_matches_plugin(tmp_path, split_pair):
    paths = _workspace(tmp_path)
    prefix = "<!-- magnolia-boot-context:alpha -->\n\n[MAGNOLIA BOOT CONTEXT — project alpha]\n\n--- boot-context.md ---\n"
    (paths.store / "boot-context.md").write_text(
        "😀" + "x" * (MAX_PAYLOAD_CHARS - len(prefix) - 2 + int(split_pair)) + "😀tail",
        encoding="utf-8",
    )

    plugin_payload = _plugin_payload(tmp_path)
    python_payload = build_boot_payload(paths)

    expected_payload = plugin_payload.removesuffix("\ud83d") if split_pair else plugin_payload
    assert python_payload == expected_payload
    assert "\n\n[truncated at 256k chars]" in python_payload
