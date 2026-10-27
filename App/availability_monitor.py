"""External health probe loop for rolling availability measurements.

Run this in a terminal separate from the FastAPI server:
    python ./App/availability_monitor.py
"""
from __future__ import annotations

from datetime import datetime, timezone
import logging
import os
import time
from urllib.error import HTTPError, URLError
from urllib.parse import urlparse
from urllib.request import Request, urlopen

from availability import PROBE_INTERVAL_SECONDS, record_probe

DEFAULT_HEALTH_URL = "http://127.0.0.1:8000/health/ready"
DEFAULT_TIMEOUT_SECONDS = 5.0


def _positive_float(name: str, default: float) -> float:
    try:
        value = float(os.environ.get(name, str(default)))
    except (TypeError, ValueError):
        return default
    return value if value > 0 else default


def probe_url(url: str, *, timeout_seconds: float = DEFAULT_TIMEOUT_SECONDS, opener=None):
    """Return (HTTP status or None, latency_ms, safe error text or None)."""
    request = Request(url, headers={"User-Agent": "chat-availability-monitor/1.0"})
    open_request = opener or urlopen
    started = time.perf_counter()
    status_code = None
    error = None
    try:
        with open_request(request, timeout=timeout_seconds) as response:
            status_code = getattr(response, "status", None)
            if status_code is None:
                status_code = response.getcode()
            response.read(4096)
            if not 200 <= int(status_code) < 300:
                error = f"Health endpoint returned HTTP {status_code}"
    except HTTPError as exc:
        status_code = exc.code
        error = f"Health endpoint returned HTTP {exc.code}"
    except (URLError, TimeoutError, OSError, ValueError) as exc:
        error = f"{type(exc).__name__}: {str(exc)[:200]}"
    latency_ms = max(0.0, (time.perf_counter() - started) * 1000.0)
    return status_code, round(latency_ms, 2), error


def main() -> None:
    url = os.environ.get("CHAT_HEALTH_URL", DEFAULT_HEALTH_URL).strip()
    parsed = urlparse(url)
    if parsed.scheme not in {"http", "https"} or not parsed.netloc:
        raise SystemExit("CHAT_HEALTH_URL must be a valid HTTP or HTTPS URL.")

    interval = _positive_float("CHAT_HEALTH_CHECK_INTERVAL_SECONDS", PROBE_INTERVAL_SECONDS)
    timeout = _positive_float("CHAT_HEALTH_CHECK_TIMEOUT_SECONDS", DEFAULT_TIMEOUT_SECONDS)
    print(f"Availability monitor started: {url}")
    print(f"Probe interval: {interval:g}s; Ctrl+C stops monitoring.")

    try:
        while True:
            loop_started = time.monotonic()
            status_code, latency_ms, error = probe_url(url, timeout_seconds=timeout)
            try:
                record_probe(status_code, latency_ms, error)
            except Exception:
                logging.exception("Could not persist availability probe")

            timestamp = datetime.now(timezone.utc).isoformat(timespec="seconds")
            if status_code is not None and 200 <= status_code < 300 and error is None:
                print(f"{timestamp} UP   status={status_code} latency_ms={latency_ms:.2f}")
            else:
                print(
                    f"{timestamp} DOWN status={status_code or 'no response'} "
                    f"latency_ms={latency_ms:.2f} error={error or 'unavailable'}"
                )
            time.sleep(max(0.0, interval - (time.monotonic() - loop_started)))
    except KeyboardInterrupt:
        print("\nAvailability monitor stopped.")


if __name__ == "__main__":
    main()
