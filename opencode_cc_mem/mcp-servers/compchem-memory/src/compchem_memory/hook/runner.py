"""Run Magnolia's hook from this repository under isolated Python."""

from __future__ import annotations

import importlib.util
import os
import sys


MEMORY_SERVER_MODE = "--memory-server"
BACKGROUND_MODE = "--background"
MEMORY_HTTP_MODE = "--memory-http"
READ_SERVER_MODE = "--read-server"


def _repository_source() -> str:
    runner = os.path.abspath(__file__)
    relative = os.path.join(
        "mcp-servers", "compchem-memory", "src", "compchem_memory", "hook", "runner.py"
    )
    if runner != os.path.realpath(runner) or not runner.endswith(os.sep + relative):
        raise SystemExit("Magnolia hook runner must be a regular file in its repository source tree")
    return os.path.dirname(os.path.dirname(os.path.dirname(runner)))


def repository_runner_path() -> str:
    """Return this validated repository runner without resolving its spelling."""
    _repository_source()
    return os.path.abspath(__file__)


def _load_repository_package(source: str) -> None:
    """Load compchem_memory by package path without exposing sibling modules."""
    package_dir = os.path.join(source, "compchem_memory")
    spec = importlib.util.spec_from_file_location(
        "compchem_memory",
        os.path.join(package_dir, "__init__.py"),
        submodule_search_locations=[package_dir],
    )
    if spec is None or spec.loader is None:
        raise SystemExit("Magnolia hook package could not be loaded from repository source")
    package = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = package
    try:
        spec.loader.exec_module(package)
    except Exception:
        sys.modules.pop(spec.name, None)
        raise


def _apply_run_owned_pycache() -> None:
    """Redirect bytecode before importing the repository package."""
    prefix = os.environ.get("PYTHONPYCACHEPREFIX")
    if prefix:
        sys.pycache_prefix = prefix


def main() -> None:
    """Pin imports and run one repository-owned hook process."""
    _apply_run_owned_pycache()
    source = _repository_source()
    repository = os.path.realpath(source)
    sys.path[:] = [path for path in sys.path if os.path.realpath(path) != repository]
    _load_repository_package(source)
    if sys.argv[1:2] == [MEMORY_SERVER_MODE]:
        sys.argv = [sys.argv[0], *sys.argv[2:]]
        from compchem_memory.server_start import install_stdio_guards

        install_stdio_guards(os.environ.get("MAGNOLIA_PROJECT_DIR", "."))
        from fastmcp import FastMCP
        from compchem_memory.memory_http import ToolAllowlistMiddleware
        from compchem_memory.server import mcp
        from compchem_memory.tool_catalog import (
            MEMORY_TOOL_ALLOWLIST_ENV,
            resolve_memory_tool_environment,
        )

        allowlist = resolve_memory_tool_environment(os.environ, legacy_stdio=True)
        if MEMORY_TOOL_ALLOWLIST_ENV not in os.environ:
            mcp.run()
            return
        filtered = FastMCP("compchem-memory-stdio-role")
        filtered.add_middleware(ToolAllowlistMiddleware(allowlist.server_names))
        filtered.mount(mcp)
        filtered.run()
        return
    if sys.argv[1:2] == [MEMORY_HTTP_MODE]:
        sys.argv = [sys.argv[0], *sys.argv[2:]]
        from compchem_memory.memory_http import main as memory_http_main

        memory_http_main()
        return
    if sys.argv[1:2] == [READ_SERVER_MODE]:
        sys.argv = [sys.argv[0], *sys.argv[2:]]
        from compchem_memory.harness.read_server import main as read_server_main

        read_server_main()
        return
    if sys.argv[1:2] == [BACKGROUND_MODE]:
        sys.argv = [sys.argv[0], *sys.argv[2:]]
        from compchem_memory.hook.background import main as background_main

        background_main()
        return
    from compchem_memory.hook.__main__ import main as hook_main

    hook_main()


if __name__ == "__mp_main__":
    _apply_run_owned_pycache()
    _load_repository_package(_repository_source())
elif __name__ == "__main__":
    main()
