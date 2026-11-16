"""Authenticated loopback HTTP entry point for the project-bound memory app."""

from __future__ import annotations

import hmac
import os
from pathlib import Path

from fastmcp import FastMCP
from fastmcp.server.auth import StaticTokenVerifier
from starlette.requests import Request
from starlette.responses import JSONResponse

from compchem_memory.harness.memory_service import (
    TOKEN_ENV,
    _process_identity,
    project_digest,
)


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


def main() -> None:
    """Run one authenticated streamable-HTTP service on IPv4 loopback only."""
    if os.environ.get("MAGNOLIA_PROJECT_BOUND") != "1":
        raise RuntimeError("memory HTTP service requires strict project binding")
    token = os.environ.get(TOKEN_ENV, "")
    port = int(os.environ.get("MAGNOLIA_MEMORY_SERVICE_PORT", "0"))
    if port <= 0:
        raise RuntimeError("memory HTTP service requires a configured port")
    build_server(token).run(
        transport="streamable-http", host="127.0.0.1", port=port, show_banner=False
    )


if __name__ == "__main__":
    main()
