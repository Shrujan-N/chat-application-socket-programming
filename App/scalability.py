"""Horizontal scalability primitives and 100k-user load benchmark for NFR-02 (Issue #42)."""

from __future__ import annotations

import argparse
import asyncio
from datetime import datetime, timezone
import json
import math
import os
from pathlib import Path
import sqlite3
import threading
import time
from typing import Any, Awaitable, Callable, Iterable, Optional
import zlib


DEFAULT_TARGET_USERS = 100_000
DEFAULT_MAX_CONNECTIONS = 150_000
DEFAULT_SHARD_COUNT = 64
DEFAULT_LOCK_STRIPES = 4_096
DEFAULT_BROADCAST_BATCH_SIZE = 256
DEFAULT_SEND_TIMEOUT_SECONDS = 2.0
DEFAULT_P95_TARGET_MS = 200.0
DEFAULT_DELIVERY_RATE_TARGET_PERCENT = 99.9


def _int_env(name: str, default: int, minimum: int, maximum: int) -> int:
    try:
        value = int(os.environ.get(name, str(default)))
    except (TypeError, ValueError):
        return default
    return value if minimum <= value <= maximum else default


def _float_env(name: str, default: float, minimum: float, maximum: float) -> float:
    try:
        value = float(os.environ.get(name, str(default)))
    except (TypeError, ValueError):
        return default
    return value if minimum < value <= maximum else default


TARGET_CONCURRENT_USERS = _int_env(
    "CHAT_SCALABILITY_TARGET_USERS", DEFAULT_TARGET_USERS, 1, 10_000_000
)
MAX_CONCURRENT_CONNECTIONS = _int_env(
    "CHAT_MAX_CONNECTIONS", DEFAULT_MAX_CONNECTIONS, 10, 10_000_000
)
REGISTRY_SHARD_COUNT = _int_env(
    "CHAT_REGISTRY_SHARDS", DEFAULT_SHARD_COUNT, 1, 4_096
)
LOCK_STRIPE_COUNT = _int_env(
    "CHAT_LOCK_STRIPES", DEFAULT_LOCK_STRIPES, 16, 65_536
)
BROADCAST_BATCH_SIZE = _int_env(
    "CHAT_BROADCAST_BATCH_SIZE", DEFAULT_BROADCAST_BATCH_SIZE, 1, 10_000
)
SEND_TIMEOUT_SECONDS = _float_env(
    "CHAT_WS_SEND_TIMEOUT_SECONDS", DEFAULT_SEND_TIMEOUT_SECONDS, 0.01, 60.0
)
SCALABILITY_P95_TARGET_MS = _float_env(
    "CHAT_SCALABILITY_P95_TARGET_MS", DEFAULT_P95_TARGET_MS, 0.1, 60_000.0
)
DB_PATH = Path(
    os.environ.get(
        "CHAT_SCALABILITY_DB_PATH",
        str(Path(__file__).resolve().parent / "logs" / "scalability_metrics.sqlite3"),
    )
).expanduser().resolve()


class CapacityExceededError(RuntimeError):
    """Raised when accepting a new WebSocket would exceed node capacity."""


def deterministic_slot(key: str, bucket_count: int) -> int:
    """Return a cross-process deterministic shard/stripe index using CRC32."""
    return zlib.crc32(str(key).encode("utf-8")) % bucket_count


class StripedLockPool:
    """Bounded pool of asyncio.Locks preserving per-recipient message ordering.

    Mapping any recipient/group key to a fixed stripe guarantees two properties:
    1. Every message targeting the same recipient acquires the exact same lock,
       preserving strict persistence + live-delivery order (Issue #45).
    2. Total allocated locks never exceed `stripe_count` (default 4,096), even
       when 100,000+ distinct users and groups are active concurrently.
    """

    def __init__(self, stripe_count: int = LOCK_STRIPE_COUNT):
        if stripe_count < 1:
            raise ValueError("stripe_count must be >= 1")
        self.stripe_count = int(stripe_count)
        self._locks: dict[int, asyncio.Lock] = {}
        self._guard = threading.RLock()

    def stripe_for(self, key: str) -> int:
        return deterministic_slot(key, self.stripe_count)

    def get(self, key: str) -> asyncio.Lock:
        idx = self.stripe_for(key)
        lock = self._locks.get(idx)
        if lock is not None:
            return lock
        with self._guard:
            lock = self._locks.get(idx)
            if lock is None:
                lock = asyncio.Lock()
                self._locks[idx] = lock
            return lock

    @property
    def allocated_locks(self) -> int:
        return len(self._locks)

    def __len__(self) -> int:
        return len(self._locks)

    def clear(self) -> None:
        with self._guard:
            self._locks.clear()

    def snapshot(self) -> dict[str, Any]:
        allocated = len(self._locks)
        return {
            "stripe_count": self.stripe_count,
            "allocated_locks": allocated,
            "utilization_percent": round(allocated / self.stripe_count * 100.0, 2),
        }


class ShardedConnectionRegistry:
    """Hash-sharded O(1) connection registry with capacity and health telemetry."""

    def __init__(
        self,
        shard_count: int = REGISTRY_SHARD_COUNT,
        max_connections: int = MAX_CONCURRENT_CONNECTIONS,
    ):
        if shard_count < 1:
            raise ValueError("shard_count must be >= 1")
        if max_connections < 1:
            raise ValueError("max_connections must be >= 1")
        self.shard_count = int(shard_count)
        self.max_connections = int(max_connections)
        self._shards: list[dict[str, set[Any]]] = [
            {} for _ in range(self.shard_count)
        ]
        self._shard_locks: list[threading.RLock] = [
            threading.RLock() for _ in range(self.shard_count)
        ]
        self._metrics_lock = threading.RLock()
        self.active_users = 0
        self.active_connections = 0
        self.peak_concurrent_users = 0
        self.peak_concurrent_connections = 0
        self.total_accepted_connections = 0
        self.rejected_over_capacity = 0
        self.dropped_connections = 0
        self.broadcast_errors = 0
        self.total_frames_routed = 0

    def shard_index(self, username: str) -> int:
        return deterministic_slot(username, self.shard_count)

    def can_accept(self) -> bool:
        return self.active_connections < self.max_connections

    def register(self, username: str, websocket: Any, *, enforce_capacity: bool = True) -> bool:
        """Register a socket in its user shard. Returns True if newly added."""
        idx = self.shard_index(username)
        with self._shard_locks[idx]:
            shard = self._shards[idx]
            existing = shard.get(username)
            if existing is not None and websocket in existing:
                return False

            with self._metrics_lock:
                if enforce_capacity and self.active_connections >= self.max_connections:
                    self.rejected_over_capacity += 1
                    raise CapacityExceededError(
                        f"Max concurrent connections ({self.max_connections}) reached"
                    )
                new_user = existing is None or len(existing) == 0
                if existing is None:
                    existing = set()
                    shard[username] = existing
                existing.add(websocket)

                if new_user:
                    self.active_users += 1
                    if self.active_users > self.peak_concurrent_users:
                        self.peak_concurrent_users = self.active_users

                self.active_connections += 1
                self.total_accepted_connections += 1
                if self.active_connections > self.peak_concurrent_connections:
                    self.peak_concurrent_connections = self.active_connections
                return True

    def unregister(self, username: str, websocket: Optional[Any] = None) -> int:
        """Remove one socket (or all sockets when websocket is None) for username."""
        idx = self.shard_index(username)
        with self._shard_locks[idx]:
            shard = self._shards[idx]
            existing = shard.get(username)
            if not existing:
                return 0

            if websocket is None:
                removed = len(existing)
                shard.pop(username, None)
                user_emptied = True
            elif websocket in existing:
                existing.remove(websocket)
                removed = 1
                if not existing:
                    shard.pop(username, None)
                    user_emptied = True
                else:
                    user_emptied = False
            else:
                return 0

            with self._metrics_lock:
                self.active_connections = max(0, self.active_connections - removed)
                if user_emptied:
                    self.active_users = max(0, self.active_users - 1)
            return removed

    def sockets_for(self, username: str) -> list[Any]:
        idx = self.shard_index(username)
        with self._shard_locks[idx]:
            existing = self._shards[idx].get(username)
            return list(existing) if existing else []

    def record_routed_frames(self, count: int) -> None:
        if count <= 0:
            return
        with self._metrics_lock:
            self.total_frames_routed += int(count)

    def record_broadcast_failure(self, count: int = 1) -> None:
        if count <= 0:
            return
        with self._metrics_lock:
            self.broadcast_errors += int(count)
            self.dropped_connections += int(count)

    def snapshot(self) -> dict[str, Any]:
        shard_user_counts = [len(shard) for shard in self._shards]
        non_empty = sum(1 for c in shard_user_counts if c > 0)
        min_users = min(shard_user_counts) if shard_user_counts else 0
        max_users = max(shard_user_counts) if shard_user_counts else 0
        avg_users = (
            round(sum(shard_user_counts) / len(shard_user_counts), 2)
            if shard_user_counts
            else 0.0
        )
        with self._metrics_lock:
            utilization = round(
                self.active_connections / self.max_connections * 100.0, 3
            )
            return {
                "shard_count": self.shard_count,
                "non_empty_shards": non_empty,
                "min_shard_users": min_users,
                "max_shard_users": max_users,
                "avg_shard_users": avg_users,
                "max_connections": self.max_connections,
                "active_users": self.active_users,
                "active_connections": self.active_connections,
                "peak_concurrent_users": self.peak_concurrent_users,
                "peak_concurrent_connections": self.peak_concurrent_connections,
                "capacity_utilization_percent": utilization,
                "total_accepted_connections": self.total_accepted_connections,
                "rejected_over_capacity": self.rejected_over_capacity,
                "dropped_connections": self.dropped_connections,
                "broadcast_errors": self.broadcast_errors,
                "total_frames_routed": self.total_frames_routed,
            }


async def send_json_with_timeout(
    websocket: Any,
    message: dict[str, Any],
    timeout_seconds: Optional[float] = None,
) -> None:
    """Send a JSON frame bounded by a per-socket timeout to prevent head-of-line stalls."""
    limit = SEND_TIMEOUT_SECONDS if timeout_seconds is None else float(timeout_seconds)
    await asyncio.wait_for(websocket.send_json(message), timeout=limit)


async def gather_bounded(
    factories: Iterable[Callable[[], Awaitable[Any]]],
    batch_size: int = BROADCAST_BATCH_SIZE,
) -> list[Any]:
    """Execute coroutine factories concurrently in bounded batches."""
    results: list[Any] = []
    batch: list[Awaitable[Any]] = []
    limit = max(1, int(batch_size))
    for factory in factories:
        batch.append(factory())
        if len(batch) >= limit:
            results.extend(await asyncio.gather(*batch, return_exceptions=True))
            batch.clear()
    if batch:
        results.extend(await asyncio.gather(*batch, return_exceptions=True))
    return results


UserDeliverCallback = Callable[[str, dict[str, Any], Any], Awaitable[int]]


class InMemoryPubSubBroker:
    """Async horizontal Pub/Sub event broker for multi-worker gateway fan-out.

    Matches the Message Broker (Pub/Sub) component in Architecture_diagram.pdf:
    each gateway worker registers its local delivery callback (`node_id`), and
    published user/group frames fan out concurrently across subscribed workers.
    """

    def __init__(self):
        self._subscribers: dict[str, UserDeliverCallback] = {}
        self._lock = threading.RLock()
        self.published_events = 0

    def subscribe_node(self, node_id: str, callback: UserDeliverCallback) -> None:
        with self._lock:
            self._subscribers[str(node_id)] = callback

    def unsubscribe_node(self, node_id: str) -> None:
        with self._lock:
            self._subscribers.pop(str(node_id), None)

    @property
    def subscriber_count(self) -> int:
        return len(self._subscribers)

    async def publish_user(
        self,
        username: str,
        message: dict[str, Any],
        *,
        exclude: Any = None,
    ) -> int:
        with self._lock:
            callbacks = list(self._subscribers.values())
            self.published_events += 1

        if not callbacks:
            return 0
        if len(callbacks) == 1:
            return await callbacks[0](username, message, exclude)

        counts = await asyncio.gather(
            *(cb(username, message, exclude) for cb in callbacks),
            return_exceptions=True,
        )
        return sum(c for c in counts if isinstance(c, int))

    async def publish_many_users(
        self,
        usernames: Iterable[str],
        message: dict[str, Any],
        *,
        exclude_username: Optional[str] = None,
        batch_size: int = BROADCAST_BATCH_SIZE,
    ) -> int:
        targets = [u for u in usernames if u and u != exclude_username]
        if not targets:
            return 0
        factories = [
            (lambda target=user: self.publish_user(target, message))
            for user in targets
        ]
        results = await gather_bounded(factories, batch_size=batch_size)
        return sum(r for r in results if isinstance(r, int))

    def snapshot(self) -> dict[str, Any]:
        with self._lock:
            return {
                "broker_type": "async_pubsub",
                "subscribed_gateway_nodes": len(self._subscribers),
                "node_ids": sorted(self._subscribers.keys()),
                "published_events": self.published_events,
            }


def _percentile(sorted_values: list[float], percentile: float) -> Optional[float]:
    if not sorted_values:
        return None
    rank = max(
        0,
        min(
            len(sorted_values) - 1,
            math.ceil((percentile / 100.0) * len(sorted_values)) - 1,
        ),
    )
    return round(sorted_values[rank], 3)


def _open_metrics_db(db_path: Optional[os.PathLike | str] = None) -> sqlite3.Connection:
    path = Path(db_path).expanduser().resolve() if db_path is not None else DB_PATH
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(path), timeout=10.0)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA busy_timeout = 5000")
    conn.execute("PRAGMA journal_mode = WAL")
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS scalability_benchmark_runs (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            measured_at REAL NOT NULL,
            concurrent_users INTEGER NOT NULL,
            concurrent_connections INTEGER NOT NULL,
            gateway_nodes INTEGER NOT NULL,
            messages_sent INTEGER NOT NULL,
            messages_delivered INTEGER NOT NULL,
            delivery_rate_percent REAL NOT NULL,
            dropped_connections INTEGER NOT NULL,
            error_count INTEGER NOT NULL,
            connect_duration_ms REAL NOT NULL,
            connections_per_second REAL NOT NULL,
            routing_duration_ms REAL NOT NULL,
            messages_per_second REAL NOT NULL,
            avg_latency_ms REAL NOT NULL,
            p50_latency_ms REAL NOT NULL,
            p95_latency_ms REAL NOT NULL,
            p99_latency_ms REAL NOT NULL,
            max_latency_ms REAL NOT NULL,
            lock_stripes_used INTEGER NOT NULL,
            nfr02_passed INTEGER NOT NULL CHECK (nfr02_passed IN (0, 1)),
            stages_json TEXT NOT NULL
        )
        """
    )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_scalability_runs_measured_at "
        "ON scalability_benchmark_runs(measured_at DESC)"
    )
    return conn


def persist_benchmark_run(
    result: dict[str, Any],
    *,
    db_path: Optional[os.PathLike | str] = None,
) -> None:
    conn = None
    try:
        conn = _open_metrics_db(db_path)
        conn.execute(
            """
            INSERT INTO scalability_benchmark_runs (
                measured_at, concurrent_users, concurrent_connections, gateway_nodes,
                messages_sent, messages_delivered, delivery_rate_percent,
                dropped_connections, error_count, connect_duration_ms,
                connections_per_second, routing_duration_ms, messages_per_second,
                avg_latency_ms, p50_latency_ms, p95_latency_ms, p99_latency_ms,
                max_latency_ms, lock_stripes_used, nfr02_passed, stages_json
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                float(result["measured_at_epoch"]),
                int(result["concurrent_users"]),
                int(result["concurrent_connections"]),
                int(result["gateway_nodes"]),
                int(result["messages_sent"]),
                int(result["messages_delivered"]),
                float(result["delivery_rate_percent"]),
                int(result["dropped_connections"]),
                int(result["error_count"]),
                float(result["connect_duration_ms"]),
                float(result["connections_per_second"]),
                float(result["routing_duration_ms"]),
                float(result["messages_per_second"]),
                float(result["latency_ms"]["avg"]),
                float(result["latency_ms"]["p50"]),
                float(result["latency_ms"]["p95"]),
                float(result["latency_ms"]["p99"]),
                float(result["latency_ms"]["max"]),
                int(result["lock_stripes_used"]),
                int(bool(result["nfr02_passed"])),
                json.dumps(result.get("ramp_stages", [])),
            ),
        )
        conn.execute(
            """
            DELETE FROM scalability_benchmark_runs
            WHERE id NOT IN (
                SELECT id FROM scalability_benchmark_runs
                ORDER BY measured_at DESC, id DESC LIMIT 50
            )
            """
        )
        conn.commit()
    except (sqlite3.Error, OSError):
        return
    finally:
        if conn is not None:
            conn.close()


class VirtualClientSocket:
    """Memory-efficient virtual WebSocket session (`__slots__`) for 100k load tests."""

    __slots__ = ("username", "node_id", "accepted", "received_frames", "last_msg_id")

    def __init__(self, username: str, node_id: str):
        self.username = username
        self.node_id = node_id
        self.accepted = False
        self.received_frames = 0
        self.last_msg_id: Optional[str] = None

    async def accept(self) -> None:
        self.accepted = True

    async def send_json(self, message: dict[str, Any]) -> None:
        if not self.accepted:
            raise RuntimeError("Socket is not open")
        self.received_frames += 1
        self.last_msg_id = message.get("msg_id")


class SimulatedGatewayNode:
    """Horizontal gateway worker backed by ShardedConnectionRegistry + PubSubBroker."""

    def __init__(
        self,
        node_id: str,
        broker: InMemoryPubSubBroker,
        *,
        shard_count: int = REGISTRY_SHARD_COUNT,
        max_connections: int = MAX_CONCURRENT_CONNECTIONS,
    ):
        self.node_id = node_id
        self.registry = ShardedConnectionRegistry(
            shard_count=shard_count,
            max_connections=max_connections,
        )
        self.broker = broker
        self.broker.subscribe_node(node_id, self.deliver_local)

    async def connect(self, username: str, socket: VirtualClientSocket) -> None:
        await socket.accept()
        self.registry.register(username, socket)

    def disconnect(self, username: str, socket: Optional[VirtualClientSocket] = None) -> None:
        self.registry.unregister(username, socket)

    async def deliver_local(
        self,
        username: str,
        message: dict[str, Any],
        exclude: Any = None,
    ) -> int:
        sockets = [
            sock for sock in self.registry.sockets_for(username) if sock is not exclude
        ]
        if not sockets:
            return 0
        if len(sockets) == 1:
            try:
                await send_json_with_timeout(sockets[0], message)
                self.registry.record_routed_frames(1)
                return 1
            except Exception:
                self.registry.record_broadcast_failure(1)
                self.registry.unregister(username, sockets[0])
                return 0

        outcomes = await asyncio.gather(
            *(send_json_with_timeout(sock, message) for sock in sockets),
            return_exceptions=True,
        )
        delivered = 0
        for sock, outcome in zip(sockets, outcomes):
            if isinstance(outcome, Exception):
                self.registry.record_broadcast_failure(1)
                self.registry.unregister(username, sock)
            else:
                delivered += 1
        self.registry.record_routed_frames(delivered)
        return delivered


async def _measure_routing_wave(
    broker: InMemoryPubSubBroker,
    lock_pool: StripedLockPool,
    active_count: int,
    wave_messages: int,
) -> dict[str, Any]:
    """Route a concurrent wave of private + group messages across connected users."""
    latencies_ms: list[float] = []
    delivered_total = 0
    errors = 0

    direct_messages = max(1, int(wave_messages * 0.8))
    group_broadcasts = max(1, wave_messages - direct_messages)
    group_size = min(20, max(2, active_count // 10))
    expected_deliveries = direct_messages + (group_broadcasts * (group_size - 1))

    async def send_direct(index: int) -> None:
        nonlocal delivered_total, errors
        sender_idx = (index * 7919) % active_count
        recipient_idx = (sender_idx + 1 + (index % max(1, active_count - 1))) % active_count
        sender = f"user_{sender_idx:06d}"
        recipient = f"user_{recipient_idx:06d}"
        frame = {
            "type": "receive_message",
            "msg_id": f"bench-dm-{active_count}-{index}",
            "sender": sender,
            "recipient": recipient,
            "content": "scalability-check",
        }
        started = time.perf_counter()
        try:
            async with lock_pool.get(recipient):
                count = await broker.publish_user(recipient, frame)
            elapsed_ms = (time.perf_counter() - started) * 1000.0
            latencies_ms.append(elapsed_ms)
            if count >= 1:
                delivered_total += 1
            else:
                errors += 1
        except Exception:
            errors += 1

    async def send_group(index: int) -> None:
        nonlocal delivered_total, errors
        base = (index * 104729) % active_count
        members = [f"user_{(base + offset) % active_count:06d}" for offset in range(group_size)]
        sender = members[0]
        group_id = f"grp-bench-{index % 64}"
        frame = {
            "type": "receive_message",
            "conversation_type": "group",
            "group_id": group_id,
            "msg_id": f"bench-grp-{active_count}-{index}",
            "sender": sender,
            "content": "group-scalability-check",
        }
        started = time.perf_counter()
        try:
            async with lock_pool.get(f"group:{group_id}"):
                count = await broker.publish_many_users(
                    members, frame, exclude_username=sender
                )
            elapsed_ms = (time.perf_counter() - started) * 1000.0
            latencies_ms.append(elapsed_ms)
            delivered_total += count
            if count < len(members) - 1:
                errors += (len(members) - 1) - count
        except Exception:
            errors += len(members) - 1

    wave_started = time.perf_counter()
    factories: list[Callable[[], Awaitable[Any]]] = [
        (lambda idx=i: send_direct(idx)) for i in range(direct_messages)
    ] + [
        (lambda idx=i: send_group(idx)) for i in range(group_broadcasts)
    ]
    await gather_bounded(factories, batch_size=BROADCAST_BATCH_SIZE)
    wave_duration_ms = (time.perf_counter() - wave_started) * 1000.0

    sorted_latencies = sorted(latencies_ms)
    avg_ms = (
        round(sum(sorted_latencies) / len(sorted_latencies), 3)
        if sorted_latencies
        else 0.0
    )
    delivery_rate = (
        round(delivered_total / expected_deliveries * 100.0, 4)
        if expected_deliveries
        else 100.0
    )
    throughput = (
        round(expected_deliveries / (wave_duration_ms / 1000.0), 1)
        if wave_duration_ms > 0
        else 0.0
    )
    return {
        "concurrent_users": active_count,
        "messages_sent": expected_deliveries,
        "messages_delivered": delivered_total,
        "delivery_rate_percent": delivery_rate,
        "errors": errors,
        "routing_duration_ms": round(wave_duration_ms, 2),
        "messages_per_second": throughput,
        "avg_latency_ms": avg_ms,
        "p50_latency_ms": _percentile(sorted_latencies, 50) or 0.0,
        "p95_latency_ms": _percentile(sorted_latencies, 95) or 0.0,
        "p99_latency_ms": _percentile(sorted_latencies, 99) or 0.0,
        "max_latency_ms": round(sorted_latencies[-1], 3) if sorted_latencies else 0.0,
    }


async def run_scalability_benchmark_async(
    *,
    concurrent_users: int = TARGET_CONCURRENT_USERS,
    messages_at_peak: int = 2_000,
    gateway_nodes: int = 4,
    persist: bool = True,
    db_path: Optional[os.PathLike | str] = None,
) -> dict[str, Any]:
    """Verify NFR-02 across ramp stages up to `concurrent_users` (default 100,000)."""
    if concurrent_users < 2:
        raise ValueError("concurrent_users must be >= 2")
    node_count = max(1, int(gateway_nodes))
    per_node_capacity = max(
        MAX_CONCURRENT_CONNECTIONS,
        math.ceil(concurrent_users / node_count) + 1_024,
    )

    broker = InMemoryPubSubBroker()
    lock_pool = StripedLockPool(stripe_count=LOCK_STRIPE_COUNT)
    nodes = [
        SimulatedGatewayNode(
            f"gateway-{idx + 1}",
            broker,
            shard_count=REGISTRY_SHARD_COUNT,
            max_connections=per_node_capacity,
        )
        for idx in range(node_count)
    ]

    # Build progressive ramp stages ending at `concurrent_users` (e.g. 1k -> 25k -> 50k -> 100k).
    candidate_stages = [
        stage
        for stage in (1_000, 25_000, 50_000)
        if stage < concurrent_users
    ]
    candidate_stages.append(concurrent_users)

    sockets: list[VirtualClientSocket] = []
    ramp_stages: list[dict[str, Any]] = []
    connect_elapsed_s = 0.0
    connected_so_far = 0
    stage_wave_size = max(100, min(500, messages_at_peak // 4))

    try:
        for stage_target in candidate_stages:
            connect_started = time.perf_counter()
            for user_idx in range(connected_so_far, stage_target):
                username = f"user_{user_idx:06d}"
                node = nodes[user_idx % node_count]
                sock = VirtualClientSocket(username, node.node_id)
                sock.accepted = True
                node.registry.register(username, sock)
                sockets.append(sock)
            connect_elapsed_s += time.perf_counter() - connect_started
            connected_so_far = stage_target

            wave_count = (
                messages_at_peak if stage_target == concurrent_users else stage_wave_size
            )
            stage_metrics = await _measure_routing_wave(
                broker, lock_pool, stage_target, wave_count
            )
            ramp_stages.append(stage_metrics)

        total_active_connections = sum(n.registry.active_connections for n in nodes)
        total_active_users = sum(n.registry.active_users for n in nodes)
        dropped_connections = sum(n.registry.dropped_connections for n in nodes)
        rejected_connections = sum(n.registry.rejected_over_capacity for n in nodes)
        peak_stage = ramp_stages[-1]
        baseline_stage = ramp_stages[0]

        connect_duration_ms = round(connect_elapsed_s * 1000.0, 2)
        connections_per_sec = (
            round(concurrent_users / connect_elapsed_s, 1)
            if connect_elapsed_s > 0
            else float(concurrent_users)
        )
        total_errors = (
            sum(stage["errors"] for stage in ramp_stages)
            + dropped_connections
            + rejected_connections
        )

        # NFR-02 Acceptance Criteria evaluation:
        # 1. Reached target threshold (100,000 concurrent connections when testing NFR-02)
        # 2. Zero dropped connections & zero errors
        # 3. Delivery rate >= 99.9% and P95 latency within target (<= 200ms)
        meets_scale_threshold = total_active_connections >= min(
            concurrent_users, TARGET_CONCURRENT_USERS
        )
        nfr02_passed = (
            meets_scale_threshold
            and total_active_connections == concurrent_users
            and dropped_connections == 0
            and total_errors == 0
            and peak_stage["delivery_rate_percent"] >= DEFAULT_DELIVERY_RATE_TARGET_PERCENT
            and peak_stage["p95_latency_ms"] <= SCALABILITY_P95_TARGET_MS
        )

        measured_at = time.time()
        result = {
            "requirement": "NFR-02",
            "measured_at": datetime.fromtimestamp(
                measured_at, tz=timezone.utc
            ).isoformat(),
            "measured_at_epoch": measured_at,
            "target_concurrent_users": TARGET_CONCURRENT_USERS,
            "concurrent_users": total_active_users,
            "concurrent_connections": total_active_connections,
            "gateway_nodes": node_count,
            "shards_per_node": REGISTRY_SHARD_COUNT,
            "total_cluster_shards": node_count * REGISTRY_SHARD_COUNT,
            "lock_stripes_configured": LOCK_STRIPE_COUNT,
            "lock_stripes_used": lock_pool.allocated_locks,
            "connect_duration_ms": connect_duration_ms,
            "connections_per_second": connections_per_sec,
            "messages_sent": peak_stage["messages_sent"],
            "messages_delivered": peak_stage["messages_delivered"],
            "delivery_rate_percent": peak_stage["delivery_rate_percent"],
            "dropped_connections": dropped_connections,
            "rejected_connections": rejected_connections,
            "error_count": total_errors,
            "routing_duration_ms": peak_stage["routing_duration_ms"],
            "messages_per_second": peak_stage["messages_per_second"],
            "latency_ms": {
                "baseline_p95": baseline_stage["p95_latency_ms"],
                "avg": peak_stage["avg_latency_ms"],
                "p50": peak_stage["p50_latency_ms"],
                "p95": peak_stage["p95_latency_ms"],
                "p99": peak_stage["p99_latency_ms"],
                "max": peak_stage["max_latency_ms"],
                "target_p95": SCALABILITY_P95_TARGET_MS,
            },
            "ramp_stages": ramp_stages,
            "meets_100k_threshold": total_active_connections >= TARGET_CONCURRENT_USERS,
            "nfr02_passed": nfr02_passed,
        }
    finally:
        # Cleanly tear down all 100k sessions so active counts return to 0.
        for user_idx, sock in enumerate(sockets):
            nodes[user_idx % node_count].disconnect(sock.username, sock)
        for node in nodes:
            broker.unsubscribe_node(node.node_id)

    if persist:
        persist_benchmark_run(result, db_path=db_path)
    return result


def run_scalability_benchmark(
    *,
    concurrent_users: int = TARGET_CONCURRENT_USERS,
    messages_at_peak: int = 2_000,
    gateway_nodes: int = 4,
    persist: bool = True,
    db_path: Optional[os.PathLike | str] = None,
) -> dict[str, Any]:
    """Synchronous entry point for CLI and test harnesses."""
    return asyncio.run(
        run_scalability_benchmark_async(
            concurrent_users=concurrent_users,
            messages_at_peak=messages_at_peak,
            gateway_nodes=gateway_nodes,
            persist=persist,
            db_path=db_path,
        )
    )


def get_scalability_summary(
    *,
    registry: Optional[ShardedConnectionRegistry] = None,
    lock_pool: Optional[StripedLockPool] = None,
    broker: Optional[InMemoryPubSubBroker] = None,
    db_path: Optional[os.PathLike | str] = None,
) -> dict[str, Any]:
    """Return live gateway scalability telemetry and latest persisted NFR-02 benchmark."""
    latest_benchmark: Optional[dict[str, Any]] = None
    conn = None
    try:
        conn = _open_metrics_db(db_path)
        row = conn.execute(
            """
            SELECT * FROM scalability_benchmark_runs
            ORDER BY measured_at DESC, id DESC LIMIT 1
            """
        ).fetchone()
        if row is not None:
            latest_benchmark = {
                "measured_at": datetime.fromtimestamp(
                    row["measured_at"], tz=timezone.utc
                ).isoformat(),
                "concurrent_users": row["concurrent_users"],
                "concurrent_connections": row["concurrent_connections"],
                "gateway_nodes": row["gateway_nodes"],
                "messages_sent": row["messages_sent"],
                "messages_delivered": row["messages_delivered"],
                "delivery_rate_percent": row["delivery_rate_percent"],
                "dropped_connections": row["dropped_connections"],
                "error_count": row["error_count"],
                "connect_duration_ms": row["connect_duration_ms"],
                "connections_per_second": row["connections_per_second"],
                "routing_duration_ms": row["routing_duration_ms"],
                "messages_per_second": row["messages_per_second"],
                "latency_ms": {
                    "avg": row["avg_latency_ms"],
                    "p50": row["p50_latency_ms"],
                    "p95": row["p95_latency_ms"],
                    "p99": row["p99_latency_ms"],
                    "max": row["max_latency_ms"],
                    "target_p95": SCALABILITY_P95_TARGET_MS,
                },
                "lock_stripes_used": row["lock_stripes_used"],
                "nfr02_passed": bool(row["nfr02_passed"]),
                "ramp_stages": json.loads(row["stages_json"] or "[]"),
            }
    except (sqlite3.Error, OSError, ValueError):
        latest_benchmark = None
    finally:
        if conn is not None:
            conn.close()

    if latest_benchmark is None:
        nfr02_status = "not_benchmarked"
    elif latest_benchmark["nfr02_passed"]:
        nfr02_status = "passing"
    else:
        nfr02_status = "failing"

    return {
        "requirement": "NFR-02",
        "target_concurrent_users": TARGET_CONCURRENT_USERS,
        "p95_latency_target_ms": SCALABILITY_P95_TARGET_MS,
        "nfr02_status": nfr02_status,
        "live_registry": registry.snapshot() if registry is not None else None,
        "lock_pool": lock_pool.snapshot() if lock_pool is not None else None,
        "pubsub_broker": broker.snapshot() if broker is not None else None,
        "latest_benchmark": latest_benchmark,
    }


def _main() -> int:
    parser = argparse.ArgumentParser(
        description="Run NFR-02 100,000-concurrent-user scalability load benchmark."
    )
    parser.add_argument(
        "--users",
        type=int,
        default=TARGET_CONCURRENT_USERS,
        help="Target concurrent connected users (default: 100000).",
    )
    parser.add_argument(
        "--messages",
        type=int,
        default=2_000,
        help="Concurrent messages routed at peak load (default: 2000).",
    )
    parser.add_argument(
        "--nodes",
        type=int,
        default=4,
        help="Simulated horizontal gateway worker nodes (default: 4).",
    )
    parser.add_argument(
        "--no-persist",
        action="store_true",
        help="Do not write benchmark run to scalability_metrics.sqlite3.",
    )
    args = parser.parse_args()

    report = run_scalability_benchmark(
        concurrent_users=args.users,
        messages_at_peak=args.messages,
        gateway_nodes=args.nodes,
        persist=not args.no_persist,
    )
    print(json.dumps(report, indent=2))
    return 0 if report["nfr02_passed"] else 1


if __name__ == "__main__":
    raise SystemExit(_main())
