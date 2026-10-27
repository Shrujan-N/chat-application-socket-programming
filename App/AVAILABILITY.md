# Availability / Uptime (NFR-03)

## What is implemented

- `GET /health/live` is a public liveness probe. It confirms the web process can answer requests.
- `GET /health/ready` is a public readiness probe. It checks both SQLite database connections used by the application and returns HTTP 503 if either one fails.
- `GET /api/availability` returns authenticated process uptime, rolling probe measurements, coverage, the configured SLA target, estimated downtime, and error-budget status.
- `availability_monitor.py` is a separate polling process. It records successful and failed readiness probes to a persistent SQLite history, including probes that fail while the web server is down.

Defaults are a 30-day rolling window, a 99.9% availability target, and one probe every 30 seconds. Configure these before starting the web server and monitor:

- `CHAT_SLA_TARGET_PERCENT` (default `99.9`)
- `CHAT_SLA_WINDOW_DAYS` (default `30`, allowed range 1–365)
- `CHAT_HEALTH_CHECK_INTERVAL_SECONDS` (default `30`)
- `CHAT_HEALTH_CHECK_TIMEOUT_SECONDS` (default `5`, monitor process)
- `CHAT_HEALTH_URL` (default `http://127.0.0.1:8000/health/ready`)
- `CHAT_AVAILABILITY_DB_PATH` (defaults to `App/logs/availability_history.sqlite3`; both processes must use the same path)

## Run locally on Windows PowerShell

Open **Terminal 1** at the repository root and start the web application:

```powershell
$env:PYTHONPATH = "$PWD\App"
python -m uvicorn App.main:app --reload
```

Open **Terminal 2** at the same repository root and start the monitor as a separate process:

```powershell
$env:CHAT_HEALTH_URL = "http://127.0.0.1:8000/health/ready"
$env:CHAT_HEALTH_CHECK_INTERVAL_SECONDS = "30"
python .\App\availability_monitor.py
```

Leave the monitor running. It prints each probe as UP or DOWN and persists the result. Stop it with Ctrl+C. After some samples have accumulated, inspect:

- [Liveness](http://127.0.0.1:8000/health/live)
- [Readiness](http://127.0.0.1:8000/health/ready)
- `GET /api/availability` with a valid bearer token (the `/api/` endpoints require login).

For a local outage demonstration, stop the web server while leaving the monitor running, wait for a few DOWN results, restart the web server, and then inspect the authenticated availability endpoint.

## How the SLO is reported

Each recorded health probe contributes at most one configured interval to the estimated window; longer gaps between checks count as unobserved coverage. The rolling error budget is:

`window_seconds × (1 - SLA_target_percent / 100)`

For a 30-day window and a 99.9% target, the budget is 2,592 seconds (43 minutes 12 seconds). The endpoint reports `slo_status = insufficient_data` until coverage reaches at least 99.9% of the rolling window. Once coverage is sufficient, it reports `within_error_budget` or `exceeded_error_budget`.

## Operational limitation

This repository implementation measures availability; it does not by itself guarantee 99.9% service uptime or provide automatic failover. For a production SLO, run the monitor on infrastructure independent of the chat server and use durable shared monitoring storage. The local SQLite history is intended for this project/demo and must be backed up if needed for long-term reporting.
