"""Regression and acceptance tests for FR-23: Message Reactions/Emojis (Issue #34).

Verifies:
1. Adding a reaction attaches it to the message.
2. Selecting the same reaction a second time toggles it off (FR-23 fail criteria).
3. Multiple users reacting with the same emoji aggregates count and user lists.
4. User reacting with different emojis stores each reaction independently.
5. Batch query returns reactions for multiple messages efficiently.
6. Chat history (/api/messages/history/...) enriches messages with reactions.
7. REST API /api/messages/{msg_id}/react toggles reactions and enforces authorization.
8. WebSocket live propagation: reacting over WebSocket delivers 'reaction_update' frame in real time.
"""

import asyncio
import os
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

_tmp = tempfile.mkdtemp(prefix="chat_reactions_test_")
os.environ["CHAT_DB_PATH"] = os.path.join(_tmp, "reactions_test.db")
os.environ.pop("CHAT_WEB_DB_PATH", None)
os.environ["JWT_SECRET"] = "test-reactions-secret"
os.environ["CHAT_UPLOADS_DIR"] = os.path.join(_tmp, "uploads")

import main
from reactions_service import (
    get_reactions_for_message,
    get_reactions_for_messages,
    toggle_message_reaction,
)
from fastapi.testclient import TestClient

client = TestClient(main.app)


def _make_user(username: str):
    client.post(
        "/api/register",
        json={
            "username": username,
            "email": f"{username}@example.com",
            "password": "ValidPass123",
        },
    )
    login = client.post(
        "/api/login",
        json={"username": username, "password": "ValidPass123"},
    )
    token = login.json()["token"]
    return username, {"Authorization": f"Bearer {token}"}, token


def _setup_message(msg_id, sender, recipient):
    main.persist_websocket_message(msg_id, sender, recipient, "Hello reaction test", "10:00")
    return msg_id


def test_add_reaction():
    u1, _, _ = _make_user("rx_user1")
    u2, _, _ = _make_user("rx_user2")
    msg_id = _setup_message("rx-add-1", u1, u2)
    conn = main.get_db()
    try:
        result = toggle_message_reaction(conn, msg_id, u1, "👍")
        assert result["success"] is True
        assert result["action"] == "added"
        assert result["emoji"] == "👍"
        assert result["username"] == u1

        reactions = result["reactions"]
        assert len(reactions) == 1
        assert reactions[0]["emoji"] == "👍"
        assert reactions[0]["count"] == 1
        assert reactions[0]["users"] == [u1]
    finally:
        conn.close()


def test_toggle_off_reaction():
    """FR-23 acceptance criteria: selecting the same reaction a second time toggles it off."""
    u1, _, _ = _make_user("rx_tog1")
    u2, _, _ = _make_user("rx_tog2")
    msg_id = _setup_message("rx-toggle-1", u1, u2)
    conn = main.get_db()
    try:
        # First click: added
        r1 = toggle_message_reaction(conn, msg_id, u1, "❤️")
        assert r1["action"] == "added"
        assert len(r1["reactions"]) == 1
        assert r1["reactions"][0]["count"] == 1

        # Second click with same emoji: removed (toggled off)
        r2 = toggle_message_reaction(conn, msg_id, u1, "❤️")
        assert r2["action"] == "removed"
        assert len(r2["reactions"]) == 0
    finally:
        conn.close()


def test_multiple_users_reaction():
    """Multiple users reacting with the same emoji increments count and lists all reactors."""
    u1, _, _ = _make_user("rx_mult1")
    u2, _, _ = _make_user("rx_mult2")
    msg_id = _setup_message("rx-multi-1", u1, u2)
    conn = main.get_db()
    try:
        toggle_message_reaction(conn, msg_id, u1, "😂")
        res = toggle_message_reaction(conn, msg_id, u2, "😂")

        assert len(res["reactions"]) == 1
        rx = res["reactions"][0]
        assert rx["emoji"] == "😂"
        assert rx["count"] == 2
        assert set(rx["users"]) == {u1, u2}
    finally:
        conn.close()


def test_multiple_different_emojis():
    """A user or different users can react with distinct emojis."""
    u1, _, _ = _make_user("rx_diff1")
    u2, _, _ = _make_user("rx_diff2")
    msg_id = _setup_message("rx-diff-1", u1, u2)
    conn = main.get_db()
    try:
        toggle_message_reaction(conn, msg_id, u1, "👍")
        toggle_message_reaction(conn, msg_id, u1, "🔥")
        toggle_message_reaction(conn, msg_id, u2, "👍")

        reactions = get_reactions_for_message(conn, msg_id)
        rx_map = {r["emoji"]: r for r in reactions}

        assert "👍" in rx_map
        assert rx_map["👍"]["count"] == 2
        assert set(rx_map["👍"]["users"]) == {u1, u2}

        assert "🔥" in rx_map
        assert rx_map["🔥"]["count"] == 1
        assert rx_map["🔥"]["users"] == [u1]
    finally:
        conn.close()


def test_batch_get_reactions():
    u1, _, _ = _make_user("rx_b1")
    u2, _, _ = _make_user("rx_b2")
    m1 = _setup_message("batch-m1", u1, u2)
    m2 = _setup_message("batch-m2", u1, u2)
    conn = main.get_db()
    try:
        toggle_message_reaction(conn, m1, u1, "👍")
        toggle_message_reaction(conn, m2, u2, "🎉")

        batch = get_reactions_for_messages(conn, [m1, m2, "batch-empty"])
        assert len(batch[m1]) == 1
        assert batch[m1][0]["emoji"] == "👍"
        assert len(batch[m2]) == 1
        assert batch[m2][0]["emoji"] == "🎉"
        assert batch["batch-empty"] == []
    finally:
        conn.close()


def test_chat_history_includes_reactions():
    u1, _, _ = _make_user("rx_h1")
    u2, _, _ = _make_user("rx_h2")
    msg_id = _setup_message("rx-hist-1", u1, u2)
    conn = main.get_db()
    try:
        toggle_message_reaction(conn, msg_id, u1, "😮")
    finally:
        conn.close()

    result = asyncio.run(main.get_chat_history(u1, u2))
    msg = next(m for m in result["history"] if m["msg_id"] == msg_id)
    assert "reactions" in msg
    assert len(msg["reactions"]) == 1
    assert msg["reactions"][0]["emoji"] == "😮"
    assert msg["reactions"][0]["count"] == 1
    assert msg["reactions"][0]["users"] == [u1]


def test_rest_react_endpoint():
    u1, u1_headers, _ = _make_user("rx_rest1")
    u2, _, _ = _make_user("rx_rest2")
    msg_id = _setup_message("rx-rest-1", u1, u2)

    # 1. Add reaction via REST
    resp = client.post(
        f"/api/messages/{msg_id}/react",
        headers=u1_headers,
        json={"emoji": "🙏"},
    )
    assert resp.status_code == 200
    data = resp.json()
    assert data["action"] == "added"
    assert len(data["reactions"]) == 1
    assert data["reactions"][0]["emoji"] == "🙏"

    # 2. Get reactions via REST
    resp_get = client.get(
        f"/api/messages/{msg_id}/reactions",
        headers=u1_headers,
    )
    assert resp_get.status_code == 200
    assert len(resp_get.json()["reactions"]) == 1

    # 3. Toggle off via REST
    resp_off = client.post(
        f"/api/messages/{msg_id}/react",
        headers=u1_headers,
        json={"emoji": "🙏"},
    )
    assert resp_off.status_code == 200
    assert resp_off.json()["action"] == "removed"
    assert len(resp_off.json()["reactions"]) == 0


def test_unauthorized_user_cannot_react():
    u1, _, _ = _make_user("rx_auth1")
    u2, _, _ = _make_user("rx_auth2")
    outsider, outsider_headers, _ = _make_user("rx_outsider")
    msg_id = _setup_message("rx-auth-1", u1, u2)

    resp = client.post(
        f"/api/messages/{msg_id}/react",
        headers=outsider_headers,
        json={"emoji": "👍"},
    )
    assert resp.status_code == 403


def test_react_nonexistent_message():
    u1, u1_headers, _ = _make_user("rx_nonexist")
    resp = client.post(
        "/api/messages/does-not-exist/react",
        headers=u1_headers,
        json={"emoji": "👍"},
    )
    assert resp.status_code == 404


def test_websocket_reaction_realtime_broadcast():
    """Verify live propagation of reactions over WebSocket."""
    alice, _, alice_token = _make_user("ws_react_a")
    bob, _, bob_token = _make_user("ws_react_b")
    msg_id = _setup_message("rx-ws-1", alice, bob)

    with client.websocket_connect(f"/ws/{alice}?token={alice_token}") as ws_a, \
         client.websocket_connect(f"/ws/{bob}?token={bob_token}") as ws_b:

        # Alice reacts with 👍
        ws_a.send_json({
            "type": "toggle_reaction",
            "msg_id": msg_id,
            "emoji": "👍"
        })

        # Both Alice and Bob receive the reaction_update frame
        event_a = ws_a.receive_json()
        assert event_a["type"] == "reaction_update"
        assert event_a["msg_id"] == msg_id
        assert event_a["emoji"] == "👍"
        assert event_a["action"] == "added"
        assert event_a["username"] == alice

        event_b = ws_b.receive_json()
        assert event_b["type"] == "reaction_update"
        assert event_b["msg_id"] == msg_id
        assert event_b["emoji"] == "👍"
        assert event_b["action"] == "added"
        assert event_b["username"] == alice
        assert len(event_b["reactions"]) == 1
        assert event_b["reactions"][0]["count"] == 1


if __name__ == "__main__":
    for name, func in list(globals().items()):
        if name.startswith("test_") and callable(func):
            print(f"Running {name}...", end=" ")
            func()
            print("PASSED")
    print("\nAll 10 message reaction tests PASSED successfully!")
