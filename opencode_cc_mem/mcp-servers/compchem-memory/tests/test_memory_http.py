import asyncio
from pathlib import Path

import pytest

from compchem_memory import memory_http, server


def test_memory_http_refuses_missing_or_wrong_bearer():
    app = memory_http.build_server("service-token")

    assert asyncio.run(app.auth.verify_token("wrong-token")) is None
    assert asyncio.run(app.auth.verify_token("service-token")) is not None


def test_memory_http_two_projects_are_isolated(tmp_path):
    first = tmp_path / "first"
    second = tmp_path / "second"
    first.mkdir()
    second.mkdir()

    assert memory_http.health_payload(first, "proc:1:first") != memory_http.health_payload(
        second, "proc:2:second"
    )


def test_memory_http_rejects_cross_project_request(tmp_path, monkeypatch):
    bound = tmp_path / "bound"
    other = tmp_path / "other"
    bound.mkdir()
    other.mkdir()
    monkeypatch.setattr(server, "PROJECT_DIR", str(bound))
    monkeypatch.setenv("MAGNOLIA_PROJECT_BOUND", "1")

    assert server._resolve_project_store(str(bound)) == str(bound.resolve())
    with pytest.raises(server.ProjectBindingError, match="bound to a different project"):
        server._resolve_project_store(str(other))

    monkeypatch.setattr(
        server.project_context,
        "effective_project_dir",
        lambda explicit=None: explicit or str(other),
    )
    with pytest.raises(server.ProjectBindingError, match="bound to a different project"):
        server._resolve_project_store()
