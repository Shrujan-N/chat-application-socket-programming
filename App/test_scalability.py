"""Automated scalability and 100,000-concurrent-user load tests for NFR-02 (Issue #42)."""

from __future__ import annotations

import asyncio
import os
from pathlib import Path
import sys
import tempfile
import time
import uuid

_TMP_DIR = tempfile.mkdtemp(prefix="chat_scalability_test_")
os.environ.setdefault("CHAT_DB_PATH", os.path.join(_TMP_DIR, "test_scalability.db"))
os.environ.setdefault("JWT_SECRET", "test-scalability-secret")

APP_DIR = Path(__file__).resolve().parent
if str(APP_DIR) not in sys.path:
    sys.path.insert(0, str(APP_DIR))

from fastapi.testclient import TestClient

import database
import main
import scalability
import web_database


client = TestClient(main.app)


def _register_and_login(prefix: str = "scale_user") -> tuple[str, dict[str, str], str]:
    username = f"{prefix}_{uuid.uuid4().hex[:8]}"
    reg = client.post(
        "/api/register",
        json={
            "username": username,
            "email": f"{username}@example.com",
            "password": "ValidPass123",
        },
    )
    assert reg.status_code == 200, reg.text
    login = client.post(
        "/api/login",
        json={"username": username, "password": "ValidPass123"},
    )
    assert login.status_code == 200, login.text
    token = login.json()["token"]
    return username, {"Authorization": f"Bearer {token}"}, token


def test_sharded_registry_supports_100k_concurrent_users_and_clean_teardown():
    registry = scalability.ShardedConnectionRegistry(
        shard_count=64,
        max_connections=150_000,
    )
    sockets = []
    for idx in range(100_000):
        username = f"u_{idx:06d}"
        sock = scalability.VirtualClientSocket(username, "gateway-1")
        assert registry.register(username, sock) is True
        sockets.append((username, sock))

    snap = registry.snapshot()
    assert snap["active_users"] == 100_000
    assert snap["active_connections"] == 100_000
    assert snap["peak_concurrent_users"] == 100_000
    assert snap["peak_concurrent_connections"] == 100_000
    assert snap["non_empty_shards"] == 64
    assert snap["rejected_over_capacity"] == 0
    # Verify balanced CRC32 sharding (~1,562.5 users/shard)
    assert 1_300 <= snap["min_shard_users"] <= 1_850
    assert 1_300 <= snap["max_shard_users"] <= 1_850

    for username, sock in sockets:
        assert registry.unregister(username, sock) == 1

    after = registry.snapshot()
    assert after["active_users"] == 0
    assert after["active_connections"] == 0
    assert after["peak_concurrent_connections"] == 100_000


def test_striped_lock_pool_bounds_memory_across_100k_recipients():
    pool = scalability.StripedLockPool(stripe_count=4_096)
    first_alice = pool.get("alice")
    assert pool.get("alice") is first_alice

    for idx in range(100_000):
        lock = pool.get(f"recipient_{idx:06d}")
        assert isinstance(lock, asyncio.Lock)

    assert pool.allocated_locks <= 4_096
    # Verify main.get_recipient_delivery_lock is backed by the bounded striped pool
    assert main.get_recipient_delivery_lock("bob") is main.get_recipient_delivery_lock("bob")


def test_slow_or_stalled_socket_never_stalls_healthy_broadcast_recipients():
    class HealthySocket:
        def __init__(self):
            self.accepted = False
            self.messages = []

        async def accept(self):
            self.accepted = True

        async def send_json(self, payload):
            self.messages.append(payload)

    class StalledSocket:
        def __init__(self):
            self.accepted = False

        async def accept(self):
            self.accepted = True

        async def send_json(self, payload):
            await asyncio.sleep(2.0)

    async def scenario():
        mgr = main.ConnectionManager(shard_count=16, max_connections=1_000)
        healthy_tab = HealthySocket()
        stalled_tab = StalledSocket()
        other_member = HealthySocket()

        await mgr.connect("alice", healthy_tab)
        await mgr.connect("alice", stalled_tab)
        await mgr.connect("charlie", other_member)

        original_timeout = scalability.SEND_TIMEOUT_SECONDS
        scalability.SEND_TIMEOUT_SECONDS = 0.05
        try:
            started = time.perf_counter()
            delivered_alice = await mgr.broadcast_user(
                "alice", {"type": "receive_message", "msg_id": "m-alice"}
            )
            alice_elapsed = time.perf_counter() - started
            assert delivered_alice == 1
            assert len(healthy_tab.messages) == 1
            assert mgr.connections_for("alice") == [healthy_tab]
            assert alice_elapsed < 0.5

            class FailingSocket(HealthySocket):
                async def send_json(self, payload):
                    raise RuntimeError("broken pipe")

            broken_sock = FailingSocket()
            await mgr.connect("dave", broken_sock)
            start_broadcast = time.perf_counter()
            total_sent = await mgr.broadcast_many_users(
                ["charlie", "dave"],
                {"type": "receive_message", "msg_id": "m-1", "content": "hi"},
            )
            elapsed = time.perf_counter() - start_broadcast
            assert total_sent == 1
            assert len(other_member.messages) == 1
            assert mgr.connections_for("dave") == []
            assert mgr.registry.dropped_connections >= 2
            assert elapsed < 0.5
        finally:
            scalability.SEND_TIMEOUT_SECONDS = original_timeout

    asyncio.run(scenario())


def test_capacity_admission_control_rejects_excess_sockets_cleanly():
    class DummySocket:
        async def accept(self):
            return None

    async def scenario():
        mgr = main.ConnectionManager(shard_count=8, max_connections=2)
        await mgr.connect("u1", DummySocket())
        await mgr.connect("u2", DummySocket())
        try:
            await mgr.connect("u3", DummySocket())
            raise AssertionError("Expected CapacityExceededError at max_connections=2")
        except scalability.CapacityExceededError:
            pass
        assert mgr.registry.active_connections == 2
        assert mgr.registry.rejected_over_capacity == 1

    asyncio.run(scenario())


def test_horizontal_multi_node_pubsub_broker_routes_across_workers():
    class DummySocket:
        def __init__(self):
            self.inbox = []

        async def accept(self):
            return None

        async def send_json(self, message):
            self.inbox.append(message)

    async def scenario():
        shared_broker = scalability.InMemoryPubSubBroker()
        worker_a = main.ConnectionManager(
            shard_count=16, broker=shared_broker, node_id="worker-a"
        )
        worker_b = main.ConnectionManager(
            shard_count=16, broker=shared_broker, node_id="worker-b"
        )
        alice_sock = DummySocket()
        bob_sock = DummySocket()
        await worker_a.connect("alice", alice_sock)
        await worker_b.connect("bob", bob_sock)

        # Publishing from worker_a reaches bob on worker_b through shared Pub/Sub
        delivered = await worker_a.broadcast_user(
            "bob", {"type": "receive_message", "msg_id": "cross-node-1"}
        )
        assert delivered == 1
        assert len(bob_sock.inbox) == 1
        assert bob_sock.inbox[0]["msg_id"] == "cross-node-1"

    asyncio.run(scenario())


def test_sqlite_wal_and_scalability_indexes_are_enabled(tmp_path, monkeypatch):
    core_db_file = tmp_path / "core_wal.db"
    web_db_file = tmp_path / "web_wal.db"
    monkeypatch.setattr(database, "DB_PATH", core_db_file)
    monkeypatch.setattr(web_database, "DB_PATH", web_db_file)

    database.init_db()
    web_database.init_db()

    conn = web_database.get_db()
    try:
        journal_mode = conn.execute("PRAGMA journal_mode").fetchone()[0].lower()
        assert journal_mode == "wal"
        assert conn.execute("PRAGMA foreign_keys").fetchone()[0] == 1
        indexes = {
            row["name"]
            for row in conn.execute(
                "SELECT name FROM sqlite_master WHERE type = 'index'"
            ).fetchall()
        }
        expected_indexes = {
            "idx_messages_recipient_status",
            "idx_messages_sender_recipient",
            "idx_messages_group_id",
            "idx_group_members_username",
            "idx_contacts_user_status",
            "idx_contacts_contact_status",
        }
        assert expected_indexes <= indexes
    finally:
        conn.close()


def test_100k_concurrent_users_load_benchmark_meets_nfr02_targets(tmp_path):
    metrics_db = tmp_path / "scalability_metrics.sqlite3"
    report = scalability.run_scalability_benchmark(
        concurrent_users=100_000,
        messages_at_peak=1_000,
        gateway_nodes=4,
        persist=True,
        db_path=metrics_db,
    )

    assert report["requirement"] == "NFR-02"
    assert report["concurrent_users"] == 100_000
    assert report["concurrent_connections"] == 100_000
    assert report["meets_100k_threshold"] is True
    assert report["dropped_connections"] == 0
    assert report["rejected_connections"] == 0
    assert report["error_count"] == 0
    assert report["delivery_rate_percent"] == 100.0
    assert report["latency_ms"]["p95"] <= scalability.SCALABILITY_P95_TARGET_MS
    assert report["lock_stripes_used"] <= scalability.LOCK_STRIPE_COUNT
    assert [stage["concurrent_users"] for stage in report["ramp_stages"]] == [
        1_000,
        25_000,
        50_000,
        100_000,
    ]
    assert report["nfr02_passed"] is True

    summary = scalability.get_scalability_summary(db_path=metrics_db)
    assert summary["nfr02_status"] == "passing"
    assert summary["latest_benchmark"]["concurrent_connections"] == 100_000
    assert summary["latest_benchmark"]["nfr02_passed"] is True


def test_scalability_api_endpoint_enforces_auth_and_reports_telemetry(tmp_path, monkeypatch):
    metrics_db = tmp_path / "api_scalability.sqlite3"
    monkeypatch.setattr(scalability, "DB_PATH", metrics_db)

    unauthenticated = client.get("/api/scalability")
    assert unauthenticated.status_code == 401

    _, headers, _ = _register_and_login("scale_api")
    initial = client.get("/api/scalability", headers=headers)
    assert initial.status_code == 200, initial.text
    payload = initial.json()
    assert payload["requirement"] == "NFR-02"
    assert payload["target_concurrent_users"] == 100_000
    assert payload["nfr02_status"] == "not_benchmarked"
    assert payload["live_registry"]["shard_count"] == scalability.REGISTRY_SHARD_COUNT
    assert payload["lock_pool"]["stripe_count"] == scalability.LOCK_STRIPE_COUNT
    assert payload["pubsub_broker"]["subscribed_gateway_nodes"] >= 1

    scalability.run_scalability_benchmark(
        concurrent_users=100_000,
        messages_at_peak=500,
        gateway_nodes=4,
        persist=True,
        db_path=metrics_db,
    )
    after_bench = client.get("/api/scalability", headers=headers)
    assert after_bench.status_code == 200
    bench_payload = after_bench.json()
    assert bench_payload["nfr02_status"] == "passing"
    assert bench_payload["latest_benchmark"]["concurrent_users"] == 100_000
    assert bench_payload["latest_benchmark"]["nfr02_passed"] is True


if __name__ == "__main__":
    class _MockMonkeyPatch:
        def __init__(self):
            self.undo = []
        def setattr(self, target, name, val):
            old = getattr(target, name)
            setattr(target, name, val)
            self.undo.append((target, name, old))
        def revert(self):
            for t, n, v in reversed(self.undo):
                setattr(t, n, v)

    print("Running App/test_scalability.py tests...")
    test_sharded_registry_supports_100k_concurrent_users_and_clean_teardown()
    print("  [PASS] test_sharded_registry_supports_100k_concurrent_users_and_clean_teardown")
    test_striped_lock_pool_bounds_memory_across_100k_recipients()
    print("  [PASS] test_striped_lock_pool_bounds_memory_across_100k_recipients")
    test_slow_or_stalled_socket_never_stalls_healthy_broadcast_recipients()
    print("  [PASS] test_slow_or_stalled_socket_never_stalls_healthy_broadcast_recipients")
    test_capacity_admission_control_rejects_excess_sockets_cleanly()
    print("  [PASS] test_capacity_admission_control_rejects_excess_sockets_cleanly")
    test_horizontal_multi_node_pubsub_broker_routes_across_workers()
    print("  [PASS] test_horizontal_multi_node_pubsub_broker_routes_across_workers")

    _mp = _MockMonkeyPatch()
    _tmp_p = Path(tempfile.mkdtemp(prefix="scale_direct_"))
    try:
        test_sqlite_wal_and_scalability_indexes_are_enabled(_tmp_p, _mp)
        print("  [PASS] test_sqlite_wal_and_scalability_indexes_are_enabled")
    finally:
        _mp.revert()

    test_100k_concurrent_users_load_benchmark_meets_nfr02_targets(_tmp_p)
    print("  [PASS] test_100k_concurrent_users_load_benchmark_meets_nfr02_targets")

    _mp = _MockMonkeyPatch()
    try:
        test_scalability_api_endpoint_enforces_auth_and_reports_telemetry(_tmp_p, _mp)
        print("  [PASS] test_scalability_api_endpoint_enforces_auth_and_reports_telemetry")
    finally:
        _mp.revert()

    print("\nALL 8 SCALABILITY TESTS PASSED!")
