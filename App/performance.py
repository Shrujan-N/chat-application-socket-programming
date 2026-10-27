"""Message delivery latency measurement helpers for NFR-01."""
from __future__ import annotations
from datetime import datetime, timezone
import math, os, sqlite3, threading, time
from pathlib import Path
from typing import Optional

DEFAULT_TARGET_MS = 200.0
DEFAULT_WINDOW_HOURS = 24
DEFAULT_MIN_SAMPLES = 20
DEFAULT_ACK_TIMEOUT_SECONDS = 5.0
MAX_REASONABLE_REPORTED_LATENCY_MS = 24 * 60 * 60 * 1000.0

def _float_env(name, default, minimum, maximum):
    try:
        value = float(os.environ.get(name, str(default)))
    except (TypeError, ValueError):
        return default
    return value if minimum < value <= maximum else default

def _int_env(name, default, minimum, maximum):
    try:
        value = int(os.environ.get(name, str(default)))
    except (TypeError, ValueError):
        return default
    return value if minimum <= value <= maximum else default

LATENCY_TARGET_MS = _float_env("CHAT_LATENCY_TARGET_MS", DEFAULT_TARGET_MS, 0.0, 60000.0)
LATENCY_WINDOW_HOURS = _int_env("CHAT_LATENCY_WINDOW_HOURS", DEFAULT_WINDOW_HOURS, 1, 720)
LATENCY_MIN_SAMPLES = _int_env("CHAT_LATENCY_MIN_SAMPLES", DEFAULT_MIN_SAMPLES, 1, 100000)
DELIVERY_ACK_TIMEOUT_SECONDS = _float_env(
    "CHAT_DELIVERY_ACK_TIMEOUT_SECONDS", DEFAULT_ACK_TIMEOUT_SECONDS, 0.0, 3600.0
)
DB_PATH = Path(os.environ.get(
    "CHAT_LATENCY_DB_PATH",
    str(Path(__file__).resolve().parent / "logs" / "message_latency.sqlite3")
)).expanduser().resolve()

_pending_lock = threading.RLock()
_pending_deliveries: dict[tuple[str, str], dict] = {}
_pending_client_reports: dict[tuple[str, str], dict] = {}

def _open_database(db_path: Optional[os.PathLike | str] = None):
    path = Path(db_path).expanduser().resolve() if db_path is not None else DB_PATH
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(path), timeout=5.0)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA busy_timeout = 5000")
    conn.execute("""
        CREATE TABLE IF NOT EXISTS message_latency_samples (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            message_id TEXT NOT NULL,
            sender TEXT NOT NULL,
            recipient TEXT NOT NULL,
            measured_at REAL NOT NULL,
            latency_ms REAL NOT NULL CHECK (latency_ms >= 0),
            source TEXT NOT NULL CHECK (source IN ('client_e2e', 'server_ack', 'ack_timeout')),
            within_target INTEGER NOT NULL CHECK (within_target IN (0, 1)),
            UNIQUE(message_id, sender, source)
        )
    """)
    conn.execute("CREATE INDEX IF NOT EXISTS idx_message_latency_measured_at ON message_latency_samples(measured_at)")
    return conn

def _persist_sample(message_id, sender, recipient, latency_ms, source, measured_at=None, db_path=None):
    if not message_id or not sender or not recipient:
        return
    value = max(0.0, float(latency_ms))
    timestamp = time.time() if measured_at is None else float(measured_at)
    conn = None
    try:
        conn = _open_database(db_path)
        conn.execute("""
            INSERT INTO message_latency_samples
                (message_id, sender, recipient, measured_at, latency_ms, source, within_target)
            VALUES (?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(message_id, sender, source) DO UPDATE SET
                measured_at=excluded.measured_at, latency_ms=excluded.latency_ms,
                within_target=excluded.within_target
        """, (message_id, sender, recipient, timestamp, value, source,
              int(value <= LATENCY_TARGET_MS)))
        conn.execute("DELETE FROM message_latency_samples WHERE measured_at < ?", (timestamp - 30 * 86400,))
        conn.commit()
    except (sqlite3.Error, OSError):
        # Metrics persistence must never interrupt chat delivery.
        return
    finally:
        if conn is not None:
            conn.close()

def _expire_pending_locked(now_perf, now_wall):
    expired = []
    for key, item in list(_pending_deliveries.items()):
        if now_perf - item["started_at"] >= DELIVERY_ACK_TIMEOUT_SECONDS:
            expired.append((key, item))
            _pending_deliveries.pop(key, None)
    for _, item in expired:
        _persist_sample(item["message_id"], item["sender"], item["recipient"],
                        DELIVERY_ACK_TIMEOUT_SECONDS * 1000.0, "ack_timeout", measured_at=now_wall)
    report_expiry = max(30.0, DELIVERY_ACK_TIMEOUT_SECONDS * 6)
    for key, item in list(_pending_client_reports.items()):
        if now_perf - item["completed_at"] > report_expiry:
            _pending_client_reports.pop(key, None)
    return len(expired)

def expire_pending_deliveries(*, now_perf=None, now_wall=None):
    with _pending_lock:
        return _expire_pending_locked(
            time.perf_counter() if now_perf is None else float(now_perf),
            time.time() if now_wall is None else float(now_wall))

def begin_delivery(message_id, sender, recipient, *, started_at=None):
    now_perf = time.perf_counter() if started_at is None else float(started_at)
    with _pending_lock:
        _expire_pending_locked(now_perf, time.time())
        _pending_deliveries[(recipient, message_id)] = {
            "message_id": message_id, "sender": sender, "recipient": recipient,
            "started_at": now_perf}

def cancel_pending_delivery(message_id, recipient):
    with _pending_lock:
        _pending_deliveries.pop((recipient, message_id), None)

def complete_delivery_ack(message_id, recipient, *, completed_at=None):
    now_perf = time.perf_counter() if completed_at is None else float(completed_at)
    with _pending_lock:
        _expire_pending_locked(now_perf, time.time())
        item = _pending_deliveries.pop((recipient, message_id), None)
        if item is None:
            return None
        latency_ms = max(0.0, (now_perf - item["started_at"]) * 1000.0)
        _persist_sample(message_id, item["sender"], recipient, latency_ms, "server_ack")
        _pending_client_reports[(item["sender"], message_id)] = {
            "message_id": message_id, "sender": item["sender"], "recipient": recipient,
            "completed_at": now_perf}
        return {"message_id": message_id, "sender": item["sender"],
                "recipient": recipient, "server_latency_ms": round(latency_ms, 3)}

def record_client_latency_report(message_id, sender, latency_ms):
    if isinstance(latency_ms, bool) or not isinstance(latency_ms, (int, float)):
        return False
    value = float(latency_ms)
    if not math.isfinite(value) or value < 0 or value > MAX_REASONABLE_REPORTED_LATENCY_MS:
        return False
    with _pending_lock:
        _expire_pending_locked(time.perf_counter(), time.time())
        item = _pending_client_reports.pop((sender, message_id), None)
        if item is None:
            return False
        _persist_sample(message_id, sender, item["recipient"], value, "client_e2e")
    return True

def clear_pending_measurements():
    """Test helper; clear in-memory timers without deleting persisted samples."""
    with _pending_lock:
        _pending_deliveries.clear()
        _pending_client_reports.clear()

def _percentile(values, percentile):
    if not values:
        return None
    ordered = sorted(values)
    return ordered[max(1, math.ceil(percentile / 100.0 * len(ordered))) - 1]

def _iso_utc(timestamp):
    return None if timestamp is None else datetime.fromtimestamp(float(timestamp), timezone.utc).isoformat()

def get_latency_summary(*, now=None, window_hours=None, target_ms=None, minimum_samples=None, db_path=None):
    """Summarize sender-browser end-to-end latency over a rolling window."""
    expire_pending_deliveries()
    current_time = time.time() if now is None else float(now)
    hours = int(window_hours if window_hours is not None else LATENCY_WINDOW_HOURS)
    target = float(target_ms if target_ms is not None else LATENCY_TARGET_MS)
    minimum = int(minimum_samples if minimum_samples is not None else LATENCY_MIN_SAMPLES)
    if not 1 <= hours <= 720:
        raise ValueError("window_hours must be between 1 and 720")
    if not math.isfinite(target) or not 0 < target <= 60000:
        raise ValueError("target_ms must be greater than 0 and at most 60000")
    if minimum < 1:
        raise ValueError("minimum_samples must be positive")
    cutoff = current_time - hours * 3600.0
    base = {
        "measurement": "sender_browser_to_recipient_verified_ack",
        "window_hours": hours, "window_started_at": _iso_utc(cutoff),
        "latency_target_ms": round(target, 3), "required_within_target_percent": 95.0,
        "minimum_samples_for_assessment": minimum, "sample_count": 0,
        "server_ack_sample_count": 0, "timed_out_deliveries": 0,
        "within_target_count": 0, "within_target_percent": None,
        "average_latency_ms": None, "p50_latency_ms": None, "p95_latency_ms": None,
        "max_latency_ms": None, "acceptance_met": None,
        "status": "insufficient_data", "last_sample_at": None}
    conn = None
    try:
        conn = _open_database(db_path)
        rows = conn.execute("""
            SELECT measured_at, latency_ms
            FROM message_latency_samples
            WHERE measured_at >= ? AND measured_at <= ?
              AND source IN ('client_e2e', 'ack_timeout')
            ORDER BY measured_at ASC, id ASC
        """, (cutoff, current_time)).fetchall()
        server_ack_count = conn.execute("""
            SELECT COUNT(*) FROM message_latency_samples
            WHERE measured_at >= ? AND measured_at <= ? AND source='server_ack'
        """, (cutoff, current_time)).fetchone()[0]
        timeout_count = conn.execute("""
            SELECT COUNT(*) FROM message_latency_samples
            WHERE measured_at >= ? AND measured_at <= ? AND source='ack_timeout'
        """, (cutoff, current_time)).fetchone()[0]
    except (sqlite3.Error, OSError):
        base["status"] = "metrics_unavailable"
        return base
    finally:
        if conn is not None:
            conn.close()
    values = [float(row["latency_ms"]) for row in rows]
    count = len(values)
    within = sum(1 for value in values if value <= target)
    percent = within / count * 100.0 if count else None
    if count >= minimum:
        acceptance = bool(percent is not None and percent >= 95.0)
        status = "meets_target" if acceptance else "below_target"
    else:
        acceptance, status = None, "insufficient_data"
    base.update({
        "sample_count": count, "server_ack_sample_count": int(server_ack_count),
        "timed_out_deliveries": int(timeout_count), "within_target_count": within,
        "within_target_percent": round(percent, 3) if percent is not None else None,
        "average_latency_ms": round(sum(values) / count, 3) if count else None,
        "p50_latency_ms": round(_percentile(values, 50), 3) if count else None,
        "p95_latency_ms": round(_percentile(values, 95), 3) if count else None,
        "max_latency_ms": round(max(values), 3) if count else None,
        "acceptance_met": acceptance, "status": status,
        "last_sample_at": _iso_utc(rows[-1]["measured_at"]) if count else None})
    return base
