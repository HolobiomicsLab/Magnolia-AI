import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from campaign_runner.security import (  # noqa: E402
    SecurityError,
    ensure_within,
    redact_secrets,
    redact_structure,
)


def test_ensure_within_accepts_child_and_rejects_escape(tmp_path):
    child = ensure_within(tmp_path, "work/out.txt")
    assert child == (tmp_path / "work" / "out.txt").resolve()
    with pytest.raises(SecurityError, match="escapes"):
        ensure_within(tmp_path, "../out.txt")


def test_ensure_within_rejects_symlink_escape(tmp_path):
    outside = tmp_path.parent / "outside"
    outside.mkdir(exist_ok=True)
    (outside / "secret.txt").write_text("secret")
    link = tmp_path / "link"
    link.symlink_to(outside, target_is_directory=True)
    with pytest.raises(SecurityError, match="escapes"):
        ensure_within(tmp_path, "link/secret.txt")


def test_redact_secrets_and_structure():
    text = "api_key=abcdef123456 and sk-abcdefghijklmnop and token: abcdefghijklmnop"
    redacted = redact_secrets(text)
    assert "abcdef123456" not in redacted
    assert "sk-abcdefghijklmnop" not in redacted
    value = {"error": "api_key=secretvalue", "items": ["sk-abcdefghijklmnop"]}
    clean = redact_structure(value)
    assert "secretvalue" not in clean["error"]
    assert clean["items"] == ["<redacted>"]
