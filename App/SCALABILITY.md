# Scalability Architecture & 100k Load Benchmark (NFR-02 — Issue #42)

## Requirement (`NFR-02`)
> *"Architecture must support 100,000 concurrent users with no degradation in performance."*
> - **Pass:** Load testing at `100,000` concurrent connections shows response times and delivery rates within defined targets (`P95 <= 200 ms`, `delivery_rate >= 99.9%`, `0` dropped connections, `0` errors).
> - **Fail:** A noticeable increase in latency, dropped connections, or errors occurring below the `100,000`-user threshold.

---

## Architectural Improvements Implemented

1. **64-Shard `O(1)` Connection Registry (`ShardedConnectionRegistry` in `App/scalability.py`)**
   - Connected users and multi-device WebSockets are partitioned across `64` deterministic `CRC32` hash shards (`CHAT_REGISTRY_SHARDS`, default `64`).
   - Eliminates linear list scans during high-churn connect/disconnect bursts and tracks live shard distribution, peak concurrency, routed frames, and admission capacity (`CHAT_MAX_CONNECTIONS`, default `150,000`).
   - Excess connections beyond `CHAT_MAX_CONNECTIONS` are gracefully rejected before handshake acceptance with RFC 6455 WebSocket status `1013` (*Try Again Later*) rather than exhausting server memory.

2. **Bounded 4,096-Stripe Delivery Lock Pool (`StripedLockPool` in `App/scalability.py` & `App/main.py`)**
   - Replaces unbounded per-recipient `asyncio.Lock` dictionary growth with `4,096` deterministic `CRC32` lock stripes (`CHAT_LOCK_STRIPES`, default `4096`).
   - Preserves strict per-recipient persistence and live-delivery ordering (`Issue #45`) while capping lock memory to `<= 4,096` locks even at `100,000+` active users and groups.

3. **Non-Blocking Batched Fan-Out & Per-Socket Timeout Isolation (`send_json_with_timeout`, `gather_bounded`)**
   - Multi-device sync, group message fan-out, mention notifications, and profile broadcasts dispatch concurrently in bounded batches (`CHAT_BROADCAST_BATCH_SIZE`, default `256`) with per-socket send timeouts (`CHAT_WS_SEND_TIMEOUT_SECONDS`, default `2.0s`).
   - A slow or stalled client socket times out and is cleaned up independently without blocking delivery to any other connected user.

4. **Horizontal Gateway Pub/Sub Event Broker (`InMemoryPubSubBroker`)**
   - Implements the **Message Broker (Pub/Sub)** tier from `documents/Architecture_diagram.pdf`.
   - Allows multiple gateway worker nodes (`SimulatedGatewayNode` / `ConnectionManager`) to subscribe and route 1-to-1 and group frames across worker boundaries, enabling horizontal scale-out across multiple processes/interfaces beyond single-IP TCP ephemeral port limits (~64k ports per IPv4 address).

5. **SQLite Write-Ahead Logging (`WAL`) & Hot-Path Composite Indexes (`App/database.py`, `App/web_database.py`)**
   - Enables `PRAGMA journal_mode=WAL`, `PRAGMA synchronous=NORMAL`, and `PRAGMA busy_timeout=10000` (`timeout=30s`) so concurrent readers and writers do not block each other.
   - Adds composite indexes (`idx_messages_recipient_status`, `idx_messages_sender_recipient`, `idx_messages_group_id`, `idx_group_members_username`, `idx_contacts_user_status`, `idx_contacts_contact_status`) to keep reconnect sync, history, and group queries `O(log N)`.

---

## Running the 100,000-Concurrent-User Load Benchmark (Windows PowerShell)

From the repository root:

```powershell
python App/scalability.py --users 100000 --messages 2000 --nodes 4
```

The benchmark ramps concurrency across `1,000` -> `25,000` -> `50,000` -> `100,000` simultaneous connected users across `4` gateway nodes (`256` total shards), routes concurrent 1-to-1 and group message waves through `InMemoryPubSubBroker` and `StripedLockPool`, persists results to `App/logs/scalability_metrics.sqlite3`, and exits `0` only if all `NFR-02` pass criteria hold.

## Inspecting Live & Benchmark Telemetry via API

Authenticated clients can query `GET /api/scalability`:

```powershell
$login = Invoke-RestMethod -Method Post -Uri http://127.0.0.1:8000/api/login `
  -ContentType "application/json" `
  -Body '{"username":"testuser01","password":"Test@1234"}'

Invoke-RestMethod -Uri http://127.0.0.1:8000/api/scalability `
  -Headers @{ Authorization = "Bearer $($login.token)" } | ConvertTo-Json -Depth 6
```

## Running the Automated Test Suite

```powershell
python -m pytest -q App/test_scalability.py
```
