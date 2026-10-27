# Performance / Latency (NFR-01)

## What is measured

- The sender browser starts a monotonic timer when it first transmits a live WebSocket message.
- The recipient verifies the message checksum, then sends a delivery acknowledgement.
- When the sender browser receives the acknowledgement result, it stops the timer and reports its measured time to the server.
- Measurements are stored in `App/logs/message_latency.sqlite3`. Server-receive-to-recipient-ACK samples are also recorded separately. A missing ACK becomes a failed sample after the configured timeout.

The main measurement is the application-level sender-browser to recipient-verified-ACK round trip. It includes sender/recipient network time, server processing and the ACK path. It is a practical indicator for the 200 ms goal, not a guarantee under every network condition.

## API and acceptance criterion

`GET /api/performance/latency` requires a valid bearer token. It returns rolling-window sample count, percentage at/below target, average, P50/P95/max latency and acceptance status.

Defaults: 200 ms target, 24-hour window, minimum 20 samples before evaluating acceptance and a 5-second ACK timeout. The status remains `insufficient_data` until 20 end-to-end samples exist; then it is `meets_target` if at least 95% are at or below 200 ms, otherwise `below_target`.

## Run locally on Windows PowerShell

At repository root, run the app in one terminal:

```powershell
$env:PYTHONPATH = "$PWD\App"
python -m uvicorn App.main:app --reload
```

Log in and send messages between two online users. The sender's message-status tooltip shows the latency result after the recipient verifies and acknowledges the message.

Run the dedicated tests and full regression suite:

```powershell
python -m pytest App/test_performance.py -v
python -m pytest -v
```

## Configuration

Set before starting the server:
- `CHAT_LATENCY_TARGET_MS` (default `200`)
- `CHAT_LATENCY_WINDOW_HOURS` (default `24`)
- `CHAT_LATENCY_MIN_SAMPLES` (default `20`)
- `CHAT_DELIVERY_ACK_TIMEOUT_SECONDS` (default `5`)
- `CHAT_LATENCY_DB_PATH` (optional; default `App/logs/message_latency.sqlite3`)

## Limitations

Only live messages with an online recipient produce measured delivery samples; offline queued messages remain governed by existing retry/sync behavior and are not considered stable-network latency samples. The system measures latency but cannot guarantee future messages will meet the target.
