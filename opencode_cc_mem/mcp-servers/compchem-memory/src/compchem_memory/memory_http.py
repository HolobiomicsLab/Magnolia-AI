"""Authenticated loopback HTTP entry point for the project-bound memory app."""

from __future__ import annotations

import argparse
import hmac
import os
from pathlib import Path
from typing import Any, Awaitable, Callable

from fastmcp import FastMCP
from fastmcp.exceptions import ToolError
from fastmcp.server.auth import StaticTokenVerifier
from fastmcp.server.dependencies import get_http_headers
from fastmcp.server.middleware import Middleware, MiddlewareContext
from starlette.requests import Request
from starlette.responses import JSONResponse

from compchem_memory.harness.memory_service import (
    TOKEN_ENV,
    _process_identity,
    project_digest,
)


def _request_project_headers() -> tuple[str, ...]:
    """Return every project hint honoured by project resolution."""
    try:
        headers = get_http_headers(
            include={"x-project-dir", "x-magnolia-project-dir"}
        )
    except RuntimeError:
        return ()
    values = (
        headers.get("x-project-dir") or headers.get("X-Project-Dir"),
        headers.get("x-magnolia-project-dir")
        or headers.get("X-Magnolia-Project-Dir"),
    )
    return tuple(value for value in values if isinstance(value, str) and value.strip())


class ProjectBindingMiddleware(Middleware):
    """Reject cross-project HTTP messages before mounted tools or resources run."""

    def __init__(self, project_dir: Path) -> None:
        self.project_dir = project_dir.expanduser().resolve()

    def _validate(self, value: Any) -> None:
        if value in (None, ""):
            return
        target = Path(str(value)).expanduser().resolve()
        if target != self.project_dir:
            raise ToolError(
                f"memory HTTP service is bound to project {self.project_dir}; "
                "cross-project request refused"
            )

    def _validate_header(self) -> None:
        for value in _request_project_headers():
            self._validate(value)

    async def on_request(
        self,
        context: MiddlewareContext[Any],
        call_next: Callable[[MiddlewareContext[Any]], Awaitable[Any]],
    ) -> Any:
        self._validate_header()
        return await call_next(context)

    async def on_call_tool(
        self,
        context: MiddlewareContext[Any],
        call_next: Callable[[MiddlewareContext[Any]], Awaitable[Any]],
    ) -> Any:
        self._validate_header()
        arguments = getattr(context.message, "arguments", None)
        if isinstance(arguments, dict):
            self._validate(arguments.get("project_dir"))
        return await call_next(context)

    async def check_resource_access(self, context: MiddlewareContext[Any]) -> None:
        self._validate_header()


def health_payload(project_dir: Path, process_identity: str) -> dict[str, str]:
    """Return the public readiness identity for one bound service."""
    return {
        "project_digest": project_digest(project_dir),
        "process_identity": process_identity,
        "service_identity": os.environ.get("MAGNOLIA_MEMORY_SERVICE_IDENTITY", ""),
    }


def build_server(token: str) -> FastMCP:
    """Wrap the existing memory app with bearer authentication and health."""
    if not token:
        raise RuntimeError("memory HTTP service requires a bearer token")
    from compchem_memory.server import mcp as memory_app

    verifier = StaticTokenVerifier(
        {token: {"client_id": "magnolia-memory", "scopes": []}}
    )
    app = FastMCP("compchem-memory-http", auth=verifier)
    if os.environ.get("MAGNOLIA_PROJECT_BOUND") == "1":
        project = os.environ.get("MAGNOLIA_PROJECT_DIR")
        if not project:
            raise RuntimeError("memory HTTP service requires a bound project directory")
        app.add_middleware(ProjectBindingMiddleware(Path(project)))
    app.mount(memory_app)

    @app.custom_route("/health", methods=["GET"])
    async def health(request: Request) -> JSONResponse:
        supplied = request.headers.get("authorization", "")
        if not hmac.compare_digest(supplied, f"Bearer {token}"):
            return JSONResponse({"error": "unauthorized"}, status_code=401)
        identity = _process_identity(os.getpid())
        if not identity:
            return JSONResponse({"error": "identity unavailable"}, status_code=503)
        project = Path(os.environ["MAGNOLIA_PROJECT_DIR"]).resolve()
        return JSONResponse(health_payload(project, identity))

    return app


def main(argv: list[str] | None = None) -> None:
    """Run one authenticated streamable-HTTP service on IPv4 loopback only."""
    parser = argparse.ArgumentParser(prog="python -m compchem_memory.memory_http")
    parser.add_argument("--project-dir", required=True, type=Path)
    args = parser.parse_args(argv)
    if os.environ.get("MAGNOLIA_PROJECT_BOUND") != "1":
        raise RuntimeError("memory HTTP service requires strict project binding")
    bound = Path(os.environ.get("MAGNOLIA_PROJECT_DIR", "")).expanduser().resolve()
    if args.project_dir.expanduser().resolve() != bound:
        raise RuntimeError("memory HTTP service project argument differs from its binding")
    token = os.environ.get(TOKEN_ENV, "")
    port = int(os.environ.get("MAGNOLIA_MEMORY_SERVICE_PORT", "0"))
    if port <= 0:
        raise RuntimeError("memory HTTP service requires a configured port")
    build_server(token).run(
        transport="streamable-http", host="127.0.0.1", port=port, show_banner=False
    )


if __name__ == "__main__":
    main()
