"""Regression tests for FR-25 web group @mentions."""
import os
import tempfile
import uuid
from pathlib import Path

_tmp = tempfile.mkdtemp(prefix="chat_user_mentions_tests_")
os.environ["CHAT_DB_PATH"] = os.path.join(_tmp, "mentions_test.db")
os.environ.pop("CHAT_WEB_DB_PATH", None)
os.environ["JWT_SECRET"] = "test-user-mentions-secret"
os.environ["CHAT_UPLOADS_DIR"] = os.path.join(_tmp, "uploads")

from fastapi.testclient import TestClient

import main
from message_integrity import content_sha256
from web_user_mentions import extract_group_mentions

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


def test_extraction_resolves_only_group_members_and_deduplicates():
    members = ["Soumya_01", "Shila_02", "Sameer_03"]
    assert extract_group_mentions(
        "Hello @shila_02, @outsider_99 and @SHILA_02; @soumya_01", members, sender="Soumya_01"
    ) == ["Shila_02"]
    assert extract_group_mentions("Contact me at mail@shila_02.example", members, sender="Sameer_03") == []
    assert extract_group_mentions("No member mentioned", members, sender="Sameer_03") == []


def test_group_mention_is_persisted_and_notifies_only_the_mentioned_member():
    sender, sender_headers, sender_token = make_user("mention_sender")
    target, target_headers, target_token = make_user("mention_target")
    outsider, outsider_headers, _ = make_user("mention_outside")

    created = client.post(
        "/api/groups",
        headers=sender_headers,
        json={"name": "Mention Test", "icon": "👥", "members": [target]},
    )
    assert created.status_code == 200, created.text
    group_id = created.json()["group"]["group_id"]
    content = f"Hi @{target}, please check this. @{outsider} is not a group member."
    msg_id = "mention-test-" + uuid.uuid4().hex

    with client.websocket_connect(f"/ws/{sender}?token={sender_token}") as sender_ws:
        with client.websocket_connect(f"/ws/{target}?token={target_token}") as target_ws:
            sender_ws.send_json({
                "type": "send_group_message",
                "msg_id": msg_id,
                "group_id": group_id,
                "content": content,
                "timestamp": "10:15 AM",
                "integrity_hash": content_sha256(content),
            })
            sent_status = sender_ws.receive_json()
            live_message = target_ws.receive_json()
            live_notification = target_ws.receive_json()

    assert sent_status["type"] == "status_update"
    assert sent_status["msg_id"] == msg_id
    assert sent_status["status"] == "sent"
    assert live_message["type"] == "receive_message"
    assert live_message["msg_id"] == msg_id
    assert live_message["group_id"] == group_id
    assert live_message["mentioned_users"] == [target]
    assert live_notification["type"] == "mention_notification"
    assert live_notification["notification_id"]
    assert live_notification["group_name"] == "Mention Test"

    history = client.get(f"/api/groups/{group_id}/history", headers=target_headers)
    assert history.status_code == 200, history.text
    stored = next(item for item in history.json()["history"] if item["msg_id"] == msg_id)
    assert stored["content"] == content
    assert stored["mentioned_users"] == [target]

    pending = client.get("/api/mentions", headers=target_headers)
    assert pending.status_code == 200, pending.text
    notification = next(item for item in pending.json()["notifications"] if item["msg_id"] == msg_id)
    assert notification["sender"] == sender
    assert notification["group_id"] == group_id
    assert notification["group_name"] == "Mention Test"

    outsider_pending = client.get("/api/mentions", headers=outsider_headers)
    assert outsider_pending.status_code == 200
    assert all(item["msg_id"] != msg_id for item in outsider_pending.json()["notifications"])

    marked = client.post(
        f"/api/mentions/{notification['notification_id']}/read", headers=target_headers
    )
    assert marked.status_code == 200
    assert marked.json()["success"] is True
    after_read = client.get("/api/mentions", headers=target_headers)
    assert all(item["msg_id"] != msg_id for item in after_read.json()["notifications"])


def test_mentions_api_requires_authentication():
    assert client.get("/api/mentions").status_code == 401


def test_web_ui_exposes_group_mention_suggestions_and_notifications():
    html = (Path(__file__).parent / "static" / "index.html").read_text(encoding="utf-8")
    assert 'id="mention-suggestions"' in html
    assert "handleMentionInput(event)" in html
    assert "handleMessageInputKeydown(event)" in html
    assert "handleMentionNotification(data)" in html
    assert "mentioned_users" in html
