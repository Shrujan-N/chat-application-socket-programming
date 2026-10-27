"""Regression tests for FR-21 (reconnection handling) - server side.

Run from the App directory:  python -m pytest test_reconnection.py -v
Uses a temporary database so a developer's chat_app.db is never touched.
"""
import os
import tempfile

_tmp = tempfile.mkdtemp()
os.environ["CHAT_DB_PATH"] = os.path.join(_tmp, "test.db")
os.environ.pop("CHAT_WEB_DB_PATH", None)
os.environ["JWT_SECRET"] = "test-secret"
os.environ["CHAT_UPLOADS_DIR"] = os.path.join(_tmp, "uploads")

import pytest
from fastapi.testclient import TestClient

import main
from auth_service import register_account, authenticate


@pytest.fixture(scope="module")
def client():
    with TestClient(main.app) as c:
        yield c


def _login(name):
    try:
        register_account(name, f"{name}@example.com", "Passw0rdX")
    except Exception:
        pass
    return authenticate(name, "Passw0rdX")["token"]


@pytest.fixture(scope="module")
def tokens():
    return {"alice": _login("alice"), "bob": _login("bob")}


def auth(token):
    return {"Authorization": f"Bearer {token}"}


def ws_url(user, token):
    return f"/ws/{user}?token={token}"


def send(ws, msg_id, recipient, content="hi"):
    ws.send_json({"type": "send_message", "msg_id": msg_id, "recipient": recipient,
                  "content": content, "timestamp": "10:00"})


def test_ping_gets_pong(client, tokens):
    with client.websocket_connect(ws_url("alice", tokens["alice"])) as ws:
        ws.send_json({"type": "ping"})
        assert ws.receive_json() == {"type": "pong"}


def test_live_message_carries_row_id_and_is_marked_delivered(client, tokens):
    with client.websocket_connect(ws_url("bob", tokens["bob"])) as bob:
        with client.websocket_connect(ws_url("alice", tokens["alice"])) as alice:
            send(alice, "live-1", "bob")
            assert alice.receive_json() == {"type": "status_update", "msg_id": "live-1", "status": "sent"}
            frame = bob.receive_json()
            assert frame["type"] == "receive_message" and frame["msg_id"] == "live-1"
            assert isinstance(frame["id"], int) and frame["id"] > 0
            assert alice.receive_json()["status"] == "delivered"
    row = main.get_db().execute("SELECT status FROM messages WHERE msg_id='live-1'").fetchone()
    assert row["status"] == "delivered"


def test_sync_first_call_returns_only_cursor(client, tokens):
    r = client.get("/api/messages/sync?since=-1", headers=auth(tokens["bob"]))
    assert r.status_code == 200
    body = r.json()
    assert body["messages"] == [] and body["has_more"] is False
    assert body["latest_id"] >= 1


def test_sync_requires_login(client):
    assert client.get("/api/messages/sync?since=0").status_code == 401


def test_offline_messages_are_synced_and_sender_is_told_delivered(client, tokens):
    cursor = client.get("/api/messages/sync?since=-1", headers=auth(tokens["bob"])).json()["latest_id"]
    # bob is offline; alice sends two messages
    with client.websocket_connect(ws_url("alice", tokens["alice"])) as alice:
        send(alice, "off-1", "bob", "one")
        send(alice, "off-2", "bob", "two")
        assert alice.receive_json()["status"] == "sent"
        assert alice.receive_json()["status"] == "sent"
        # bob comes back and syncs while alice is online
        r = client.get(f"/api/messages/sync?since={cursor}", headers=auth(tokens["bob"]))
        body = r.json()
        assert [m["msg_id"] for m in body["messages"]] == ["off-1", "off-2"]
        assert all(m["status"] == "delivered" for m in body["messages"])
        assert body["latest_id"] == body["messages"][-1]["id"]
        # alice sees the delivered ticks
        got = {(f["msg_id"], f["status"]) for f in (alice.receive_json(), alice.receive_json())}
        assert got == {("off-1", "delivered"), ("off-2", "delivered")}
    # syncing again from the new cursor returns nothing (no duplicates)
    again = client.get(f"/api/messages/sync?since={body['latest_id']}", headers=auth(tokens["bob"])).json()
    assert again["messages"] == [] and again["latest_id"] == body["latest_id"]


def test_sync_only_returns_messages_addressed_to_caller(client, tokens):
    r = client.get("/api/messages/sync?since=0", headers=auth(tokens["alice"])).json()
    assert all(m["recipient"] == "alice" for m in r["messages"])


def test_sync_pages_when_more_than_limit(client, tokens, monkeypatch):
    monkeypatch.setattr(main, "SYNC_PAGE_LIMIT", 2)
    cursor = client.get("/api/messages/sync?since=-1", headers=auth(tokens["bob"])).json()["latest_id"]
    with client.websocket_connect(ws_url("alice", tokens["alice"])) as alice:
        for i in range(5):
            send(alice, f"page-{i}", "bob")
        for _ in range(5):
            alice.receive_json()
    seen, since, pages = [], cursor, 0
    while True:
        body = client.get(f"/api/messages/sync?since={since}", headers=auth(tokens["bob"])).json()
        seen += [m["msg_id"] for m in body["messages"]]
        since, pages = body["latest_id"], pages + 1
        if not body["has_more"]:
            break
    assert seen == [f"page-{i}" for i in range(5)] and pages == 3


def test_resending_same_msg_id_after_reconnect_is_not_duplicated(client, tokens):
    with client.websocket_connect(ws_url("bob", tokens["bob"])) as bob:
        with client.websocket_connect(ws_url("alice", tokens["alice"])) as alice:
            send(alice, "dup-1", "bob", "same")
            assert alice.receive_json()["status"] == "sent"
            assert bob.receive_json()["msg_id"] == "dup-1"
            alice.receive_json()   # delivered
        # connection dropped before the ack reached the browser: it re-sends the same msg_id
        with client.websocket_connect(ws_url("alice", tokens["alice"])) as alice:
            send(alice, "dup-1", "bob", "same")
            assert alice.receive_json() == {"type": "status_update", "msg_id": "dup-1", "status": "sent"}
            # nothing is forwarded to bob a second time
            alice.send_json({"type": "ping"})
            assert alice.receive_json() == {"type": "pong"}
    n = main.get_db().execute("SELECT COUNT(*) FROM messages WHERE msg_id='dup-1'").fetchone()[0]
    assert n == 1


def test_transient_failure_is_flagged_retryable(client, tokens, monkeypatch):
    import sqlite3
    def boom(*a, **k):
        raise sqlite3.OperationalError("database is locked")
    monkeypatch.setattr(main, "persist_websocket_message", boom)
    with client.websocket_connect(ws_url("alice", tokens["alice"])) as alice:
        send(alice, "fail-1", "bob")
        err = alice.receive_json()
        assert err["type"] == "error" and err["msg_id"] == "fail-1" and err["retryable"] is True


def test_blocked_error_is_not_retryable(client, tokens):
    main.get_db().execute("INSERT OR IGNORE INTO user_blocks(blocker_username, blocked_username) VALUES ('bob','alice')").connection.commit()
    with client.websocket_connect(ws_url("alice", tokens["alice"])) as alice:
        send(alice, "blk-1", "bob")
        err = alice.receive_json()
        assert err["type"] == "error" and not err.get("retryable")
    c = main.get_db(); c.execute("DELETE FROM user_blocks"); c.commit()


def test_new_connection_replaces_old_with_4001(client, tokens):
    from starlette.websockets import WebSocketDisconnect
    with client.websocket_connect(ws_url("alice", tokens["alice"])) as first:
        with client.websocket_connect(ws_url("alice", tokens["alice"])) as second:
            with pytest.raises(WebSocketDisconnect) as exc:
                first.receive_json()
            assert exc.value.code == 4001
            second.send_json({"type": "ping"})
            assert second.receive_json() == {"type": "pong"}