"""Regression tests for NFR-03 Availability / Uptime."""
import sqlite3
from urllib.error import URLError

from fastapi.testclient import TestClient

import availability
from availability_monitor import probe_url
import main


class FakeResponse:
    status = 200

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, traceback):
        return False

    def read(self, limit=-1):
        return b'{"status":"ready"}'


class FakeConnection:
    def __init__(self, fails=False):
        self.fails = fails
        self.closed = False

    def execute(self, query):
        assert query == "SELECT 1"
        if self.fails:
            raise sqlite3.OperationalError("database unavailable")
        return self

    def close(self):
        self.closed = True


def test_summary_does_not_claim_uptime_without_probe_data(tmp_path):
    now = 1_800_000_000.0
    summary = availability.get_availability_summary(
        server_started_at=now - 60,
        now=now,
        window_days=30,
        sla_target_percent=99.9,
        probe_interval_seconds=30,
        db_path=tmp_path / "empty.sqlite3",
    )
    assert summary["monitor_status"] == "not_started"
    assert summary["observed_probes"] == 0
    assert summary["observed_availability_percent"] is None
    assert summary["slo_status"] == "insufficient_data"
    assert summary["error_budget_seconds"] == 2592.0


def test_rolling_summary_estimates_downtime_and_error_budget(tmp_path):
    now = 1_800_000_000.0
    db_path = tmp_path / "probes.sqlite3"
    interval = 43_200.0
    availability.record_probe(200, 12.5, checked_at=now - interval, db_path=db_path)
    availability.record_probe(503, 21.0, "not ready", checked_at=now, db_path=db_path)

    summary = availability.get_availability_summary(
        server_started_at=now - 120,
        now=now,
        window_days=1,
        sla_target_percent=99.9,
        probe_interval_seconds=interval,
        db_path=db_path,
    )
    assert summary["observed_probes"] == 2
    assert summary["successful_probes"] == 1
    assert summary["failed_probes"] == 1
    assert summary["observed_availability_percent"] == 50.0
    assert summary["window_coverage_percent"] == 50.0
    assert summary["estimated_downtime_seconds"] == interval
    assert summary["error_budget_seconds"] == 86.4
    assert summary["slo_status"] == "insufficient_data"


def test_full_window_coverage_flags_exceeded_error_budget(tmp_path):
    now = 1_800_000_000.0
    db_path = tmp_path / "probes.sqlite3"
    interval = 43_200.0
    availability.record_probe(200, 1, checked_at=now - 86_400, db_path=db_path)
    availability.record_probe(503, 1, "outage", checked_at=now - interval, db_path=db_path)
    availability.record_probe(200, 1, checked_at=now, db_path=db_path)

    summary = availability.get_availability_summary(
        server_started_at=now - 90,
        now=now,
        window_days=1,
        sla_target_percent=99.9,
        probe_interval_seconds=interval,
        db_path=db_path,
    )
    assert summary["window_coverage_percent"] == 100.0
    assert summary["slo_status"] == "exceeded_error_budget"
    assert summary["within_error_budget"] is False


def test_full_window_coverage_with_healthy_probes_is_within_budget(tmp_path):
    now = 1_800_000_000.0
    db_path = tmp_path / "healthy-probes.sqlite3"
    interval = 43_200.0
    availability.record_probe(200, 1, checked_at=now - 86_400, db_path=db_path)
    availability.record_probe(200, 1, checked_at=now - interval, db_path=db_path)
    availability.record_probe(200, 1, checked_at=now, db_path=db_path)

    summary = availability.get_availability_summary(
        server_started_at=now - 90,
        now=now,
        window_days=1,
        sla_target_percent=99.9,
        probe_interval_seconds=interval,
        db_path=db_path,
    )
    assert summary["window_coverage_percent"] == 100.0
    assert summary["observed_availability_percent"] == 100.0
    assert summary["slo_status"] == "within_error_budget"
    assert summary["within_error_budget"] is True
    assert summary["error_budget_remaining_seconds"] == 86.4


def test_old_probes_are_not_included_in_rolling_window(tmp_path):
    now = 1_800_000_000.0
    db_path = tmp_path / "probes.sqlite3"
    availability.record_probe(200, 1, checked_at=now - 2 * 86400, db_path=db_path)
    summary = availability.get_availability_summary(
        server_started_at=now - 10,
        now=now,
        window_days=1,
        sla_target_percent=99.9,
        probe_interval_seconds=30,
        db_path=db_path,
    )
    assert summary["observed_probes"] == 0
    assert summary["slo_status"] == "insufficient_data"


def test_probe_url_records_successful_response():
    status, latency, error = probe_url(
        "http://localhost/health/ready",
        opener=lambda request, timeout: FakeResponse(),
    )
    assert status == 200
    assert latency >= 0
    assert error is None


def test_probe_url_records_connection_failure():
    def offline(request, timeout):
        raise URLError("server offline")

    status, latency, error = probe_url("http://localhost/health/ready", opener=offline)
    assert status is None
    assert latency >= 0
    assert "URLError" in error


def test_liveness_endpoint_is_public():
    response = TestClient(main.app).get("/health/live")
    assert response.status_code == 200
    assert response.json()["status"] == "alive"


def test_readiness_endpoint_reports_both_database_dependencies(monkeypatch):
    connections = []

    def factory():
        connection = FakeConnection()
        connections.append(connection)
        return connection

    monkeypatch.setattr(main, "core_get_db", factory)
    monkeypatch.setattr(main, "get_db", factory)
    response = TestClient(main.app).get("/health/ready")

    assert response.status_code == 200
    assert response.json() == {
        "status": "ready",
        "checks": {"core_database": "ok", "web_database": "ok"},
    }
    assert len(connections) == 2
    assert all(connection.closed for connection in connections)


def test_readiness_endpoint_returns_503_when_database_is_unavailable(monkeypatch):
    monkeypatch.setattr(main, "core_get_db", lambda: FakeConnection(fails=True))
    monkeypatch.setattr(main, "get_db", lambda: FakeConnection())
    response = TestClient(main.app).get("/health/ready")

    assert response.status_code == 503
    assert response.json()["status"] == "not_ready"
    assert response.json()["checks"] == {
        "core_database": "failed",
        "web_database": "ok",
    }


def test_availability_api_requires_authentication():
    response = TestClient(main.app).get("/api/availability")
    assert response.status_code == 401
