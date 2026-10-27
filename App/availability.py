"""Availability measurement helpers for the chat application.

The HTTP process cannot record its own outage while it is stopped. Run
availability_monitor.py as a separate process to collect rolling probe data.
"""
from __future__ import annotations

from datetime import datetime, timezone
import os
from pathlib import Path
import sqlite3
import time
from typing import Callable, Optional

DEFAULT_SLA_TARGET_PERCENT = 99.9
DEFAULT_WINDOW_DAYS = 30
DEFAULT_PROBE_INTERVAL_SECONDS = 30.0
MIN_SLO_COVERAGE_PERCENT = 99.9


def _env_float(name: str, default: float, *, minimum: float, maximum: float) -> float:
    try:
        value = float(os.environ.get(name, str(default)))
    except (TypeError, ValueError):
        return default
    return value if minimum < value <= maximum else default


def _env_int(name: str, default: int, *, minimum: int, maximum: int) -> int:
    try:
        value = int(os.environ.get(name, str(default)))
    except (TypeError, ValueError):
        return default
    return value if minimum <= value <= maximum else default


SLA_TARGET_PERCENT = _env_float(
    "CHAT_SLA_TARGET_PERCENT", DEFAULT_SLA_TARGET_PERCENT, minimum=0.0, maximum=100.0
)
SLA_WINDOW_DAYS = _env_int("CHAT_SLA_WINDOW_DAYS", DEFAULT_WINDOW_DAYS, minimum=1, maximum=365)
PROBE_INTERVAL_SECONDS = _env_float(
    "CHAT_HEALTH_CHECK_INTERVAL_SECONDS",
    DEFAULT_PROBE_INTERVAL_SECONDS,
    minimum=0.0,
    maximum=86400.0,
)
DB_PATH = Path(
    os.environ.get(
        "CHAT_AVAILABILITY_DB_PATH",
        str(Path(__file__).resolve().parent / "logs" / "availability_history.sqlite3"),
    )
).expanduser().resolve()


def _open_database(db_path: Optional[os.PathLike | str] = None) -> sqlite3.Connection:
    path = Path(db_path).expanduser().resolve() if db_path is not None else DB_PATH
    path.parent.mkdir(parents=True, exist_ok=True)
    connection = sqlite3.connect(str(path), timeout=5.0)
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA busy_timeout = 5000")
    connection.execute(
        """
        CREATE TABLE IF NOT EXISTS availability_probes (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            checked_at REAL NOT NULL,
            success INTEGER NOT NULL CHECK (success IN (0, 1)),
            status_code INTEGER,
            latency_ms REAL NOT NULL CHECK (latency_ms >= 0),
            error TEXT
        )
        """
    )
    connection.execute(
        "CREATE INDEX IF NOT EXISTS idx_availability_probes_checked_at "
        "ON availability_probes(checked_at)"
    )
    return connection


def record_probe(
    status_code: Optional[int],
    latency_ms: float,
    error: Optional[str] = None,
    *,
    checked_at: Optional[float] = None,
    db_path: Optional[os.PathLike | str] = None,
) -> None:
    """Persist one health probe result; status 2xx means the service was ready."""
    timestamp = time.time() if checked_at is None else float(checked_at)
    status = int(status_code) if status_code is not None else None
    success = int(status is not None and 200 <= status < 300)
    safe_latency = max(0.0, float(latency_ms))
    safe_error = str(error)[:300] if error else None
    connection = _open_database(db_path)
    try:
        connection.execute(
            """
            INSERT INTO availability_probes
                (checked_at, success, status_code, latency_ms, error)
            VALUES (?, ?, ?, ?, ?)
            """,
            (timestamp, success, status, safe_latency, safe_error),
        )
        retention_days = max(90, SLA_WINDOW_DAYS * 2)
        connection.execute(
            "DELETE FROM availability_probes WHERE checked_at < ?",
            (timestamp - retention_days * 86400,),
        )
        connection.commit()
    finally:
        connection.close()


def check_database_connection(connection_factory: Callable[[], object]) -> bool:
    """Return False rather than raising if a required database cannot be queried."""
    connection = None
    try:
        connection = connection_factory()
        connection.execute("SELECT 1")
        return True
    except Exception:
        return False
    finally:
        if connection is not None:
            try:
                connection.close()
            except Exception:
                pass


def _iso_utc(timestamp: Optional[float]) -> Optional[str]:
    if timestamp is None:
        return None
    return datetime.fromtimestamp(float(timestamp), timezone.utc).isoformat()


def get_availability_summary(
    *,
    server_started_at: float,
    now: Optional[float] = None,
    window_days: Optional[int] = None,
    sla_target_percent: Optional[float] = None,
    probe_interval_seconds: Optional[float] = None,
    db_path: Optional[os.PathLike | str] = None,
) -> dict:
    """Summarize probe results and SLO error budget over a rolling time window."""
    current_time = time.time() if now is None else float(now)
    days = int(window_days if window_days is not None else SLA_WINDOW_DAYS)
    target = float(sla_target_percent if sla_target_percent is not None else SLA_TARGET_PERCENT)
    interval = float(
        probe_interval_seconds
        if probe_interval_seconds is not None
        else PROBE_INTERVAL_SECONDS
    )
    if days < 1 or days > 365:
        raise ValueError("window_days must be between 1 and 365")
    if not 0.0 < target <= 100.0:
        raise ValueError("sla_target_percent must be greater than 0 and at most 100")
    if interval <= 0:
        raise ValueError("probe_interval_seconds must be positive")

    window_seconds = days * 86400.0
    cutoff = current_time - window_seconds
    error_budget_seconds = window_seconds * (100.0 - target) / 100.0
    base = {
        "service": "chat-application",
        "process_started_at": _iso_utc(server_started_at),
        "process_uptime_seconds": round(max(0.0, current_time - server_started_at), 1),
        "window_days": days,
        "window_started_at": _iso_utc(cutoff),
        "sla_target_percent": round(target, 4),
        "probe_interval_seconds": round(interval, 3),
        "minimum_coverage_for_slo_percent": MIN_SLO_COVERAGE_PERCENT,
        "error_budget_seconds": round(error_budget_seconds, 1),
        "monitor_status": "unavailable",
        "observed_probes": 0,
        "successful_probes": 0,
        "failed_probes": 0,
        "observed_availability_percent": None,
        "window_coverage_percent": 0.0,
        "estimated_downtime_seconds": None,
        "error_budget_remaining_seconds": None,
        "within_error_budget": None,
        "slo_status": "insufficient_data",
        "first_probe_at": None,
        "last_probe_at": None,
        "last_probe_status_code": None,
    }

    connection = None
    try:
        connection = _open_database(db_path)
        rows = connection.execute(
            """
            SELECT checked_at, success, status_code
            FROM availability_probes
            WHERE checked_at >= ? AND checked_at <= ?
            ORDER BY checked_at ASC, id ASC
            """,
            (cutoff, current_time),
        ).fetchall()
    except (sqlite3.Error, OSError):
        return base
    finally:
        if connection is not None:
            connection.close()

    count = len(rows)
    if count == 0:
        base["monitor_status"] = "not_started"
        return base

    successful = sum(1 for row in rows if row["success"])
    failed = count - successful
    # Each probe covers at most one interval. Longer gaps are unobserved.
    covered_seconds = 0.0
    for index, row in enumerate(rows):
        checked_at = float(row["checked_at"])
        next_at = float(rows[index + 1]["checked_at"]) if index + 1 < count else current_time
        covered_seconds += min(interval, max(0.0, next_at - checked_at))
    coverage = min(100.0, covered_seconds / window_seconds * 100.0)

    observed_availability = successful / count * 100.0
    estimated_downtime = min(window_seconds, failed * interval)
    if coverage < MIN_SLO_COVERAGE_PERCENT:
        slo_status = "insufficient_data"
        within_budget = None
        remaining_budget = None
    elif estimated_downtime <= error_budget_seconds:
        slo_status = "within_error_budget"
        within_budget = True
        remaining_budget = max(0.0, error_budget_seconds - estimated_downtime)
    else:
        slo_status = "exceeded_error_budget"
        within_budget = False
        remaining_budget = 0.0

    base.update({
        "monitor_status": "recording",
        "observed_probes": count,
        "successful_probes": successful,
        "failed_probes": failed,
        "observed_availability_percent": round(observed_availability, 5),
        "window_coverage_percent": round(coverage, 5),
        "estimated_downtime_seconds": round(estimated_downtime, 1),
        "error_budget_remaining_seconds": (
            round(remaining_budget, 1) if remaining_budget is not None else None
        ),
        "within_error_budget": within_budget,
        "slo_status": slo_status,
        "first_probe_at": _iso_utc(rows[0]["checked_at"]),
        "last_probe_at": _iso_utc(rows[-1]["checked_at"]),
        "last_probe_status_code": rows[-1]["status_code"],
    })
    return base
