"""Regression tests for Issue #33: block, mute, and report users.

Run from the App directory with: python -m pytest -q test_user_safety.py
"""
import os
import tempfile
import uuid

_tmp = tempfile.mkdtemp()
os.environ.setdefault("CHAT_DB_PATH", os.path.join(_tmp, "test_user_safety.db"))
os.environ.setdefault("JWT_SECRET", "test-secret")

from fastapi.testclient import TestClient
import main
from web_database import get_db

client = TestClient(main.app)


def make_user(prefix):
    name = f"{prefix}_{uuid.uuid4().hex[:8]}"
    registered = client.post(
        "/api/register",
        json={"username": name, "email": f"{name}@example.com", "password": "Valid1234"},
    )
    assert registered.status_code == 200, registered.text
    logged_in = client.post("/api/login", json={"username": name, "password": "Valid1234"})
    assert logged_in.status_code == 200, logged_in.text
    return name, {"Authorization": f"Bearer {logged_in.json()['token']}"}, logged_in.json()["token"]


def test_block_list_and_unblock():
    alice, alice_headers, _ = make_user("blk_a")
    bob, _, _ = make_user("blk_b")

    blocked = client.post("/api/blocks", json={"username": bob}, headers=alice_headers)
    assert blocked.status_code == 200 and blocked.json()["success"] is True
    assert bob in blocked.json()["blocked"]
    assert client.get("/api/blocks", headers=alice_headers).json()["blocked"] == [bob]

    unblocked = client.delete(f"/api/blocks/{bob}", headers=alice_headers)
    assert unblocked.status_code == 200 and bob not in unblocked.json()["blocked"]


def test_user_cannot_block_or_report_self():
    alice, headers, _ = make_user("blk_self")
    assert client.post("/api/blocks", json={"username": alice}, headers=headers).status_code == 400
    assert client.post(
        "/api/reports", json={"username": alice, "reason": "test report"}, headers=headers
    ).status_code == 400


def test_report_is_saved_for_moderation():
    alice, headers, _ = make_user("rpt_a")
    bob, _, _ = make_user("rpt_b")
    response = client.post(
        "/api/reports",
        json={"username": bob, "reason": "Repeated harassment in chat"},
        headers=headers,
    )
    assert response.status_code == 200 and response.json()["success"] is True

    conn = get_db()
    try:
        row = conn.execute(
            "SELECT reporter_username, reported_username, reason, status FROM user_reports ORDER BY id DESC LIMIT 1"
        ).fetchone()
        assert row is not None
        assert row["reporter_username"] == alice
        assert row["reported_username"] == bob
        assert row["reason"] == "Repeated harassment in chat"
        assert row["status"] == "open"
    finally:
        conn.close()


def test_blocked_user_cannot_send_websocket_message_or_persist_it():
    alice, alice_headers, alice_token = make_user("msg_a")
    bob, _, bob_token = make_user("msg_b")
    assert client.post("/api/blocks", json={"username": bob}, headers=alice_headers).status_code == 200
    msg_id = "blocked-" + uuid.uuid4().hex

    with client.websocket_connect(f"/ws/{alice}?token={alice_token}") as socket:
        socket.send_json({
            "type": "send_message", "msg_id": msg_id,
            "sender": alice, "recipient": bob,
            "content": "This should not be sent", "timestamp": "12:00",
        })
        event = socket.receive_json()
        assert event["type"] == "error" and event["msg_id"] == msg_id
        assert "block" in event["message"].lower()

    conn = get_db()
    try:
        row = conn.execute("SELECT 1 FROM messages WHERE msg_id = ?", (msg_id,)).fetchone()
        assert row is None
    finally:
        conn.close()


def test_blocked_users_cannot_create_a_contact_request():
    alice, alice_headers, _ = make_user("req_a")
    bob, bob_headers, _ = make_user("req_b")
    assert client.post("/api/blocks", json={"username": bob}, headers=alice_headers).status_code == 200
    response = client.post(
        "/api/contacts/add",
        json={"user_id": bob, "contact_username": alice},
        headers=bob_headers,
    )
    assert response.status_code == 200
    assert response.json()["success"] is False
    assert "block" in response.json()["error"].lower()
