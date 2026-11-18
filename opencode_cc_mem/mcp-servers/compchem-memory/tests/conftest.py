"""Test-suite-wide fixtures.

`_isolate_llm_env` autouse: clears every env var the LLM resolver looks at
before each test. This prevents the test runner's shell environment
(e.g. DEEPSEEK_API_KEY exported in dev sessions) from accidentally
activating live LLM calls inside tests that aren't designed for it.

Tests that need to exercise the LLM path explicitly opt in via
`monkeypatch.setenv(...)` per-test (see tests/test_llm_providers.py).

`_isolate_session_list` autouse: same idea for the handover skip-before-export
probe — generate_handover must never shell out to the live opencode CLI in
tests. The stub returns None (= listing unavailable), which preserves the
legacy export-everything behavior. Tests exercising the skip path inject
`session_updates=` or patch the function themselves.
"""
import os
import sys
import tempfile
from pathlib import Path

import pytest

from compchem_memory.harness.memory_service import private_services_root


_REAL_HOME_MARKER = "MAGNOLIA_PYTEST_REAL_HOME"
_REAL_HOME = Path(os.environ.get(_REAL_HOME_MARKER, Path.home())).resolve()
_OWNER_STATE_ROOTS = (
    _REAL_HOME / ".local" / "state" / "magnolia",
    _REAL_HOME / ".config" / "magnolia",
    private_services_root({**os.environ, "HOME": str(_REAL_HOME)}).parent,
)
_TEST_ROOT = Path(tempfile.mkdtemp(prefix="magnolia-memory-pytest-state-"))
_ISOLATED_ENV = {
    "HOME": str(_TEST_ROOT / "home"),
    "XDG_STATE_HOME": str(_TEST_ROOT / "state"),
    "XDG_CONFIG_HOME": str(_TEST_ROOT / "config"),
    _REAL_HOME_MARKER: str(_REAL_HOME),
}
_ORIGINAL_ENV = {name: os.environ.get(name) for name in _ISOLATED_ENV}


def _tree_metadata(root: Path) -> tuple[tuple[str, int], ...]:
    """List path names and mtimes without reading file contents."""
    if not root.exists() and not root.is_symlink():
        return ()
    rows = [(".", root.lstat().st_mtime_ns)]
    for path in sorted(root.rglob("*"), key=lambda item: str(item)):
        rows.append((str(path.relative_to(root)), path.lstat().st_mtime_ns))
    return tuple(rows)


_AUDITED_STATE_PATHS: list[Path] = []


def _record_state_path(event, args):
    """Record path metadata for filesystem mutations, never file contents."""
    indexes = (0, 1) if event in {"os.rename", "os.replace"} else (0,)
    if event not in {"os.mkdir", "os.rmdir", "os.remove", "os.rename", "os.replace"}:
        return
    for index in indexes:
        try:
            raw = args[index]
            if isinstance(raw, (str, bytes, os.PathLike)):
                _AUDITED_STATE_PATHS.append(Path(os.fsdecode(raw)).resolve())
        except (IndexError, OSError, TypeError, ValueError):
            continue


sys.addaudithook(_record_state_path)


def _audited_mutation_count(root: Path) -> int:
    canonical = root.resolve()
    return sum(
        path == canonical or path.is_relative_to(canonical)
        for path in _AUDITED_STATE_PATHS
    )


def _owner_state_metadata(roots):
    """Snapshot content-free tree and in-process mutation metadata."""
    return {
        root: (_tree_metadata(root), _audited_mutation_count(root))
        for root in roots
    }


_OWNER_STATE_BEFORE = _owner_state_metadata(_OWNER_STATE_ROOTS)
for _name, _value in _ISOLATED_ENV.items():
    os.environ[_name] = _value
Path(_ISOLATED_ENV["HOME"]).mkdir(parents=True)


@pytest.fixture(scope="session", autouse=True)
def _isolate_magnolia_state():
    """Redirect state and fail if the owner trees change during this suite."""
    try:
        yield
        after = _owner_state_metadata(_OWNER_STATE_ROOTS)
        assert after == _OWNER_STATE_BEFORE, (
            "tests created or modified owner Magnolia state; "
            "compare listings and mtimes under the guarded roots"
        )
    finally:
        for name, value in _ORIGINAL_ENV.items():
            if value is None:
                os.environ.pop(name, None)
            else:
                os.environ[name] = value


@pytest.fixture
def owner_state_metadata():
    """Expose the content-free guard snapshot for its contract test."""
    return _owner_state_metadata


_LLM_ENV_VARS = (
    "MAGNOLIA_MEMORY_PROVIDER",
    "MAGNOLIA_MEMORY_MODEL",
    "MAGNOLIA_LLM_PROVIDER",   # deprecated alias, still read
    "MAGNOLIA_LLM_MODEL",      # deprecated alias, still read
    "MAGNOLIA_LLM_API_KEY",
    "MAGNOLIA_OPENAI_API_KEY",
    "MAGNOLIA_MISTRAL_API_KEY",
    "MAGNOLIA_DIRECT_FALLBACK_PROVIDER",
    "MAGNOLIA_DIRECT_FALLBACK_MODEL",
    "ANTHROPIC_API_KEY",
    "DEEPSEEK_API_KEY",
    "DEEPSEEK_BASE_URL",
    "OPENAI_API_KEY",
    "OPENAI_BASE_URL",
    "OPENROUTER_API_KEY",
    "KIMI_PLAN_MAGNOLIA_API_KEY",
    "KIMI_API_KEY",
    "KIMI_BASE_URL",
    "MISTRAL_API_KEY",
    "MISTRAL_BASE_URL",
    "ZHIPU_API_KEY",
    "ZAI_API_KEY",
    "ZAI_BASE_URL",
)

# The magnolia wrapper exports a cwd-relative project pin ("projects/<name>").
# Redirect it to a throwaway absolute dir: importing compchem_memory.server
# runs its startup scan against this value and would otherwise scaffold a
# ghost project store inside the package tree.
os.environ["MAGNOLIA_PROJECT_DIR"] = str(_TEST_ROOT / "project")


@pytest.fixture(autouse=True)
def _isolate_llm_env(monkeypatch):
    """Clear LLM-related env vars before every test."""
    for var in _LLM_ENV_VARS:
        monkeypatch.delenv(var, raising=False)


@pytest.fixture(autouse=True)
def _isolate_opencode_runtime_env(monkeypatch):
    """Tests run as v1 unless they opt in: a developer shell inside a v2
    Magnolia session carries these, and they switch the OpenCode calls."""
    for var in ("MAGNOLIA_OPENCODE_V2", "MAGNOLIA_OPENCODE_V2_BIN", "OPENCODE_DB"):
        monkeypatch.delenv(var, raising=False)


@pytest.fixture(autouse=True)
def _isolate_session_list(monkeypatch):
    """Never shell out to `opencode session list` from the test suite.

    Returning None means 'listing unavailable', which makes generate_handover
    export every session (the pre-2026-09-22 behavior). Tests that exercise
    skip-before-export inject `session_updates=` explicitly."""
    from compchem_memory import handover as _handover

    monkeypatch.setattr(_handover, "_list_session_updates", lambda: None)


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

    def notices_redirected(result, project_dir, tool_name=""):
        if str(project_dir) in (".", ""):
            project_dir = str(tmp_path)
        return real_notices(result, project_dir, tool_name)

    monkeypatch.setattr(_capture, "_attach_distill_notices", notices_redirected)


@pytest.fixture(autouse=True)
def _hermetic_magnolia_flags(monkeypatch):
    """Strip inherited Magnolia switch env vars (2026-10-07).

    The long-lived compchem-tools daemon exports MAGNOLIA_CONSOLIDATION_AUTO /
    AUTO_CONFIRM / RETIREMENT from whatever wrapper launch started it, and
    pytest runs under run_shell inherit them — the auto-band then fires
    INSIDE unit tests (observed: 25 spurious consolidation failures after a
    --profile full era daemon replaced an older one). Tests that exercise a
    switch set it explicitly via monkeypatch; nothing may depend on the
    ambient environment."""
    for var in (
        "MAGNOLIA_CONSOLIDATION_AUTO",
        "MAGNOLIA_CONSOLIDATION_AUTO_MAX",
        "MAGNOLIA_AUTO_CONFIRM",
        "MAGNOLIA_RETIREMENT",
        "MAGNOLIA_PROSE_NUMBERS",
        "MAGNOLIA_CONSOLIDATION_SEED",
        "MAGNOLIA_CONSOLIDATION_SIBLING",
        "MAGNOLIA_CONSOLIDATION_FLOOR",
        "MAGNOLIA_ADMISSION_GATE",
        "MAGNOLIA_DISTILL_SECOND_PASS",
        "MAGNOLIA_HOOK_POLICY",
        # OpenCode v2 pointers: project_magnolia-exp runs the suite from a
        # v2 session, so these would otherwise leak in and change exporter /
        # handover behavior. Tests that exercise v2 mode set them explicitly.
        "OPENCODE_DB",
        "MAGNOLIA_OPENCODE_V2",
    ):
        monkeypatch.delenv(var, raising=False)
