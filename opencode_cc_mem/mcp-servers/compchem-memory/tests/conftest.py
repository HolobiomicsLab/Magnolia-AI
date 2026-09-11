"""Test-suite-wide fixtures.

`_isolate_llm_env` autouse: clears every env var the LLM resolver looks at
before each test. This prevents the test runner's shell environment
(e.g. DEEPSEEK_API_KEY exported in dev sessions) from accidentally
activating live LLM calls inside tests that aren't designed for it.

Tests that need to exercise the LLM path explicitly opt in via
`monkeypatch.setenv(...)` per-test (see tests/test_llm_providers.py).
"""
import os
import tempfile

import pytest


_LLM_ENV_VARS = (
    "MAGNOLIA_MEMORY_PROVIDER",
    "MAGNOLIA_MEMORY_MODEL",
    "MAGNOLIA_LLM_PROVIDER",   # deprecated alias, still read
    "MAGNOLIA_LLM_MODEL",      # deprecated alias, still read
    "MAGNOLIA_LLM_API_KEY",
    "ANTHROPIC_API_KEY",
    "DEEPSEEK_API_KEY",
    "DEEPSEEK_BASE_URL",
    "OPENAI_API_KEY",
    "OPENAI_BASE_URL",
    "KIMI_PLAN_MAGNOLIA_API_KEY",
    "KIMI_API_KEY",
    "KIMI_BASE_URL",
)

# The magnolia wrapper exports a cwd-relative project pin ("projects/<name>").
# Redirect it to a throwaway absolute dir: importing compchem_memory.server
# runs its startup scan against this value and would otherwise scaffold a
# ghost project store inside the package tree.
os.environ["MAGNOLIA_PROJECT_DIR"] = tempfile.mkdtemp(prefix="magnolia-tests-")


@pytest.fixture(autouse=True)
def _isolate_llm_env(monkeypatch):
    """Clear LLM-related env vars before every test."""
    for var in _LLM_ENV_VARS:
        monkeypatch.delenv(var, raising=False)


@pytest.fixture(autouse=True)
def _redirect_default_capture_dir(monkeypatch, tmp_path):
    """@captured defaults project_dir to the cwd when a tool omits it; send
    that to the per-test tmp dir so suites never scaffold <package>/.magnolia."""
    from compchem_memory import capture as _capture

    real = _capture.get_session_manager

    def redirected(project_dir, *a, **k):
        if str(project_dir) in (".", ""):
            project_dir = str(tmp_path)
        return real(project_dir, *a, **k)

    monkeypatch.setattr(_capture, "get_session_manager", redirected)

    real_notices = _capture._attach_distill_notices

    def notices_redirected(result, project_dir):
        if str(project_dir) in (".", ""):
            project_dir = str(tmp_path)
        return real_notices(result, project_dir)

    monkeypatch.setattr(_capture, "_attach_distill_notices", notices_redirected)
