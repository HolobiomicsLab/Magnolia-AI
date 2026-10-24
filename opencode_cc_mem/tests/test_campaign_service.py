import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from campaign_runner.service import (  # noqa: E402
    ServiceError,
    ensure_service,
    read_service,
    service_status,
    stop_service,
)


def test_missing_service_is_not_running(tmp_path):
    assert service_status(tmp_path)["running"] is False
    assert stop_service(tmp_path) is False


def test_running_service_must_match_database(tmp_path, monkeypatch):
    project = tmp_path / "project"
    path = project / ".magnolia" / "campaign-service.json"
    path.parent.mkdir(parents=True)
    path.write_text(
        json.dumps(
            {
                "project": "project",
                "port": 4112,
                "db": str(tmp_path / "old.db"),
                "pid": 1234,
                "pgid": 1234,
                "password": "secret",
                "started_at": 0,
                "log": "log",
            }
        )
    )
    monkeypatch.setattr(
        "campaign_runner.service.service_status",
        lambda project_dir: {"running": True},
    )
    try:
        ensure_service(project, "opencode", str(tmp_path / "new.db"))
    except ServiceError as exc:
        assert "different database" in str(exc)
    else:
        raise AssertionError("expected database mismatch to be rejected")


def test_service_status_hides_password_and_dead_pid(tmp_path):
    path = tmp_path / ".magnolia" / "campaign-service.json"
    path.parent.mkdir(parents=True)
    path.write_text(
        json.dumps(
            {
                "project": "project",
                "port": 4999,
                "db": "db",
                "pid": 99999999,
                "pgid": 99999999,
                "password": "secret-token",
                "started_at": 0,
                "log": "log",
            }
        )
    )
    record = read_service(tmp_path)
    assert record["password"] == "secret-token"
    status = service_status(tmp_path)
    assert status["running"] is False
    assert status["password"] == "<hidden>"
