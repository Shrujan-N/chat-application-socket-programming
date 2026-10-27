"""Regression tests for NFR-01 Performance / Latency."""
import performance
import time
from fastapi.testclient import TestClient
import main

def test_no_samples_reports_insufficient_data(tmp_path, monkeypatch):
    monkeypatch.setattr(performance, "DB_PATH", tmp_path / "latency.sqlite3")
    performance.clear_pending_measurements()
    summary = performance.get_latency_summary(now=1_800_000_000, window_hours=24)
    assert summary["sample_count"] == 0
    assert summary["p95_latency_ms"] is None
    assert summary["acceptance_met"] is None
    assert summary["status"] == "insufficient_data"

def test_server_ack_and_client_report_are_persisted(tmp_path, monkeypatch):
    monkeypatch.setattr(performance, "DB_PATH", tmp_path / "latency.sqlite3")
    performance.clear_pending_measurements()
    started = time.perf_counter()
    performance.begin_delivery("m-1", "sender", "recipient", started_at=started)
    result = performance.complete_delivery_ack("m-1", "recipient", completed_at=started + 0.05)
    assert result["sender"] == "sender"
    assert result["server_latency_ms"] == 50.0
    assert performance.record_client_latency_report("m-1", "sender", 125.5) is True
    assert performance.record_client_latency_report("m-1", "sender", 126.0) is False
    summary = performance.get_latency_summary(now=time.time(), window_hours=24)
    assert summary["sample_count"] == 1
    assert summary["server_ack_sample_count"] == 1
    assert summary["average_latency_ms"] == 125.5
    assert summary["within_target_percent"] == 100.0
    assert summary["status"] == "insufficient_data"

def test_delivery_ack_is_bound_to_the_actual_recipient(tmp_path, monkeypatch):
    monkeypatch.setattr(performance, "DB_PATH", tmp_path / "latency.sqlite3")
    performance.clear_pending_measurements()
    performance.begin_delivery("m-2", "sender", "recipient", started_at=100.0)
    assert performance.complete_delivery_ack("m-2", "wrong-user", completed_at=100.1) is None
    assert performance.complete_delivery_ack("m-2", "recipient", completed_at=100.1) is not None

def test_missing_ack_is_recorded_as_failed_sample(tmp_path, monkeypatch):
    monkeypatch.setattr(performance, "DB_PATH", tmp_path / "latency.sqlite3")
    monkeypatch.setattr(performance, "DELIVERY_ACK_TIMEOUT_SECONDS", 0.5)
    performance.clear_pending_measurements()
    performance.begin_delivery("m-timeout", "sender", "recipient", started_at=10.0)
    assert performance.expire_pending_deliveries(now_perf=10.6, now_wall=1_800_000_000) == 1
    summary = performance.get_latency_summary(now=1_800_000_000, window_hours=24)
    assert summary["sample_count"] == 1
    assert summary["timed_out_deliveries"] == 1
    assert summary["within_target_percent"] == 0.0
    assert summary["status"] == "insufficient_data"

def test_95_percent_target_is_met_after_minimum_sample_count(tmp_path):
    db_path, now = tmp_path / "latency.sqlite3", 1_800_000_000.0
    for i in range(20):
        performance._persist_sample(f"pass-{i}", "sender", "recipient", 190, "client_e2e", measured_at=now, db_path=db_path)
    summary = performance.get_latency_summary(now=now+1, window_hours=24, target_ms=200, minimum_samples=20, db_path=db_path)
    assert summary["sample_count"] == 20
    assert summary["within_target_percent"] == 100.0
    assert summary["p95_latency_ms"] == 190.0
    assert summary["acceptance_met"] is True
    assert summary["status"] == "meets_target"

def test_acceptance_fails_when_less_than_95_percent_meet_target(tmp_path):
    db_path, now = tmp_path / "latency.sqlite3", 1_800_000_000.0
    for i in range(18):
        performance._persist_sample(f"fast-{i}", "sender", "recipient", 150, "client_e2e", measured_at=now, db_path=db_path)
    for i in range(2):
        performance._persist_sample(f"slow-{i}", "sender", "recipient", 350, "client_e2e", measured_at=now, db_path=db_path)
    summary = performance.get_latency_summary(now=now+1, window_hours=24, target_ms=200, minimum_samples=20, db_path=db_path)
    assert summary["within_target_percent"] == 90.0
    assert summary["acceptance_met"] is False
    assert summary["status"] == "below_target"
    assert summary["p95_latency_ms"] == 350.0

def test_old_samples_are_excluded_from_window(tmp_path):
    db_path, now = tmp_path / "latency.sqlite3", 1_800_000_000.0
    performance._persist_sample("old", "sender", "recipient", 25, "client_e2e", measured_at=now-90000, db_path=db_path)
    performance._persist_sample("new", "sender", "recipient", 45, "client_e2e", measured_at=now-1, db_path=db_path)
    summary = performance.get_latency_summary(now=now, window_hours=24, db_path=db_path)
    assert summary["sample_count"] == 1
    assert summary["average_latency_ms"] == 45.0

def test_latency_report_rejects_invalid_values(tmp_path, monkeypatch):
    monkeypatch.setattr(performance, "DB_PATH", tmp_path / "latency.sqlite3")
    performance.clear_pending_measurements()
    started = time.perf_counter()
    performance.begin_delivery("m-bad", "sender", "recipient", started_at=started)
    performance.complete_delivery_ack("m-bad", "recipient", completed_at=started + 0.1)
    assert performance.record_client_latency_report("m-bad", "sender", -1) is False
    assert performance.record_client_latency_report("m-bad", "sender", float("nan")) is False
    assert performance.record_client_latency_report("m-bad", "sender", "bad") is False

def test_latency_endpoint_requires_authentication():
    assert TestClient(main.app).get("/api/performance/latency").status_code == 401

def test_application_and_browser_wire_latency_measurements():
    from pathlib import Path
    app_dir = Path(__file__).resolve().parent
    main_source = (app_dir / "main.py").read_text(encoding="utf-8")
    browser = (app_dir / "static" / "index.html").read_text(encoding="utf-8")
    assert "performance_metrics.begin_delivery(" in main_source
    assert 'elif message_type == "delivery_ack":' in main_source
    assert 'elif message_type == "latency_report":' in main_source
    assert '@app.get("/api/performance/latency")' in main_source
    assert "performance.now()" in browser
    assert 'type: "delivery_ack"' in browser
    assert "type: 'latency_report'" in browser
    assert "handlePerformanceMetric(data)" in browser
