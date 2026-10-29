"""Regression tests for web group creation, membership, history, and live delivery."""
import os
import tempfile
import uuid
from pathlib import Path

_tmp = tempfile.mkdtemp(prefix="chat_group_tests_")
os.environ["CHAT_DB_PATH"] = os.path.join(_tmp, "group_test.db")
os.environ.pop("CHAT_WEB_DB_PATH", None)
os.environ["JWT_SECRET"] = "test-group-chat-secret"
os.environ["CHAT_UPLOADS_DIR"] = os.path.join(_tmp, "uploads")

from fastapi.testclient import TestClient

import main
from message_integrity import content_sha256

client = TestClient(main.app)


def make_user(prefix):
    username = f"{prefix}_{uuid.uuid4().hex[:8]}"
    password = "Valid1234"
    registered = client.post(
        "/api/register",
        json={"username": username, "email": f"{username}@example.com", "password": password},
    )
    assert registered.status_code == 200, registered.text
    logged_in = client.post(
        "/api/login", json={"username": username, "password": password}
    )
    assert logged_in.status_code == 200, logged_in.text
    return username, {"Authorization": f"Bearer {logged_in.json()['token']}"}, logged_in.json()["token"]


def test_group_creation_listing_and_membership_authorization():
    creator, creator_headers, _ = make_user("grp_owner")
    member, member_headers, _ = make_user("grp_member")
    outsider, outsider_headers, _ = make_user("grp_outside")

    created = client.post(
        "/api/groups",
        headers=creator_headers,
        json={"name": "Project Team", "icon": "👥", "members": [member]},
    )
    assert created.status_code == 200, created.text
    group = created.json()["group"]
    assert group["name"] == "Project Team"
    assert group["member_count"] == 2

    listed = client.get("/api/groups", headers=member_headers)
    assert listed.status_code == 200
    assert [item["group_id"] for item in listed.json()["groups"]] == [group["group_id"]]

    members = client.get(
        f"/api/groups/{group['group_id']}/members", headers=creator_headers
    )
    assert members.status_code == 200
    assert {item["username"] for item in members.json()["members"]} == {creator, member}
    assert next(item for item in members.json()["members"] if item["username"] == creator)["role"] == "admin"

    assert client.get(
        f"/api/groups/{group['group_id']}/members", headers=outsider_headers
    ).status_code == 403
    assert client.get(
        f"/api/groups/{group['group_id']}/history", headers=outsider_headers
    ).status_code == 403


def test_group_creation_validates_name_and_members():
    creator, headers, _ = make_user("grp_validation")
    response = client.post(
        "/api/groups", headers=headers,
        json={"name": "   ", "members": [creator]},
    )
    assert response.status_code == 400

    response = client.post(
        "/api/groups", headers=headers,
        json={"name": "No other members", "members": [creator]},
    )
    assert response.status_code == 400


def test_group_messages_are_persisted_and_delivered_live():
    creator, creator_headers, creator_token = make_user("grp_sender")
    member, member_headers, member_token = make_user("grp_receiver")
    created = client.post(
        "/api/groups", headers=creator_headers,
        json={"name": "Live Test", "members": [member]},
    )
    assert created.status_code == 200, created.text
    group_id = created.json()["group"]["group_id"]

    content = "Hello group"
    msg_id = "group-test-" + uuid.uuid4().hex
    payload = {
        "type": "send_group_message",
        "msg_id": msg_id,
        "group_id": group_id,
        "content": content,
        "timestamp": "10:15",
        "integrity_hash": content_sha256(content),
    }

    with client.websocket_connect(f"/ws/{creator}?token={creator_token}") as sender_socket:
        with client.websocket_connect(f"/ws/{member}?token={member_token}") as member_socket:
            sender_socket.send_json(payload)
            status = sender_socket.receive_json()
            incoming = member_socket.receive_json()

    assert status["type"] == "status_update"
    assert status["msg_id"] == msg_id
    assert status["status"] == "sent"
    assert incoming["type"] == "receive_message"
    assert incoming["group_id"] == group_id
    assert incoming["sender"] == creator
    assert incoming["content"] == content
    assert incoming["integrity_hash"] == content_sha256(content)

    history = client.get(
        f"/api/groups/{group_id}/history", headers=member_headers
    )
    assert history.status_code == 200, history.text
    stored = next(item for item in history.json()["history"] if item["msg_id"] == msg_id)
    assert stored["content"] == content
    assert stored["conversation_type"] == "group"


def test_non_member_cannot_send_group_messages():
    creator, creator_headers, creator_token = make_user("grp_secure_owner")
    member, _, _ = make_user("grp_secure_member")
    outsider, _, outsider_token = make_user("grp_secure_outside")
    created = client.post(
        "/api/groups", headers=creator_headers,
        json={"name": "Private Group", "members": [member]},
    )
    assert created.status_code == 200, created.text
    group_id = created.json()["group"]["group_id"]
    msg_id = "group-unauthorized-" + uuid.uuid4().hex
    content = "Should not be persisted"

    with client.websocket_connect(f"/ws/{outsider}?token={outsider_token}") as socket:
        socket.send_json({
            "type": "send_group_message",
            "msg_id": msg_id,
            "group_id": group_id,
            "content": content,
            "timestamp": "10:16",
            "integrity_hash": content_sha256(content),
        })
        response = socket.receive_json()

    assert response["type"] == "error"
    connection = main.get_db()
    try:
        assert connection.execute(
            "SELECT 1 FROM messages WHERE msg_id = ?", (msg_id,)
        ).fetchone() is None
    finally:
        connection.close()


def test_group_messages_are_included_in_offline_sync_for_members():
    creator, creator_headers, _ = make_user("grp_sync_owner")
    member, member_headers, _ = make_user("grp_sync_member")
    created = client.post(
        "/api/groups", headers=creator_headers,
        json={"name": "Offline Group", "members": [member]},
    )
    assert created.status_code == 200, created.text
    group_id = created.json()["group"]["group_id"]

    cursor = client.get("/api/messages/sync?since=-1", headers=member_headers)
    assert cursor.status_code == 200, cursor.text
    since = cursor.json()["latest_id"]
    msg_id = "group-sync-" + uuid.uuid4().hex
    main.persist_websocket_group_message(
        msg_id, group_id, creator, "Offline group message", "10:17",
        content_sha256("Offline group message"),
    )

    synchronized = client.get(
        f"/api/messages/sync?since={since}", headers=member_headers
    )
    assert synchronized.status_code == 200, synchronized.text
    messages = synchronized.json()["messages"]
    restored = next(item for item in messages if item["msg_id"] == msg_id)
    assert restored["group_id"] == group_id
    assert restored["conversation_type"] == "group"
    assert restored["content"] == "Offline group message"


def test_group_chat_ui_has_creation_selection_and_group_message_hooks():
    html = (Path(__file__).parent / "static" / "index.html").read_text(encoding="utf-8")
    assert 'onclick="openCreateGroupDialog()"' in html
    assert 'id="create-group-dialog"' in html
    assert "async function createGroup(event)" in html
    assert "function selectGroup(group)" in html
    assert "`/api/groups/${encodeURIComponent(groupIdAtRequest)}/history`" in html
    assert "type: groupIdAtSend ? 'send_group_message' : 'send_message'" in html
