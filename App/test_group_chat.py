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


def test_browser_group_admins_can_manage_group_and_roles():
    owner, owner_headers, _ = make_user("grp_admin_owner")
    member, member_headers, _ = make_user("grp_admin_member")
    added, _, _ = make_user("grp_admin_added")
    created = client.post(
        "/api/groups", headers=owner_headers,
        json={"name": "Original Name", "icon": "👥", "members": [member]},
    )
    assert created.status_code == 200, created.text
    group_id = created.json()["group"]["group_id"]

    denied = client.patch(
        f"/api/groups/{group_id}", headers=member_headers,
        json={"name": "Not allowed", "icon": "🚫"},
    )
    assert denied.status_code == 403

    updated = client.patch(
        f"/api/groups/{group_id}", headers=owner_headers,
        json={"name": "Updated Team", "icon": "🧪"},
    )
    assert updated.status_code == 200, updated.text
    assert updated.json()["group"] == {
        "group_id": group_id, "name": "Updated Team", "icon": "🧪"
    }

    added_response = client.post(
        f"/api/groups/{group_id}/members", headers=owner_headers,
        json={"username": added},
    )
    assert added_response.status_code == 200, added_response.text
    assert added in added_response.json()["members"]

    promoted = client.post(
        f"/api/groups/{group_id}/admins", headers=owner_headers,
        json={"username": added},
    )
    assert promoted.status_code == 200, promoted.text
    roles = client.get(f"/api/groups/{group_id}/members", headers=owner_headers).json()["members"]
    assert next(item for item in roles if item["username"] == added)["role"] == "admin"

    demoted = client.delete(
        f"/api/groups/{group_id}/admins/{added}", headers=owner_headers,
    )
    assert demoted.status_code == 200, demoted.text
    removed = client.delete(
        f"/api/groups/{group_id}/members/{added}", headers=owner_headers,
    )
    assert removed.status_code == 200, removed.text
    assert added not in removed.json()["members"]
    assert client.get(f"/api/groups/{group_id}/history", headers=member_headers).status_code == 200


def test_browser_group_message_edit_delete_for_me_and_delete_everyone():
    owner, owner_headers, _ = make_user("grp_msg_owner")
    member, member_headers, _ = make_user("grp_msg_member")
    created = client.post(
        "/api/groups", headers=owner_headers,
        json={"name": "Message Actions", "members": [member]},
    )
    assert created.status_code == 200, created.text
    group_id = created.json()["group"]["group_id"]

    edit_id = "browser-edit-" + uuid.uuid4().hex
    main.persist_websocket_group_message(edit_id, group_id, owner, "before edit", "10:20")
    denied = client.patch(
        f"/api/messages/{edit_id}", headers=member_headers,
        json={"content": "unauthorized edit"},
    )
    assert denied.status_code == 403
    edited = client.patch(
        f"/api/messages/{edit_id}", headers=owner_headers,
        json={"content": "after edit"},
    )
    assert edited.status_code == 200, edited.text
    owner_history = client.get(f"/api/groups/{group_id}/history", headers=owner_headers).json()["history"]
    edited_message = next(item for item in owner_history if item["msg_id"] == edit_id)
    assert edited_message["content"] == "after edit"
    assert edited_message["edited"] == 1

    me_id = "browser-delete-me-" + uuid.uuid4().hex
    main.persist_websocket_group_message(me_id, group_id, owner, "member can hide this", "10:21")
    hidden = client.delete(
        f"/api/messages/{me_id}?scope=me", headers=member_headers,
    )
    assert hidden.status_code == 200, hidden.text
    member_history = client.get(f"/api/groups/{group_id}/history", headers=member_headers).json()["history"]
    owner_history = client.get(f"/api/groups/{group_id}/history", headers=owner_headers).json()["history"]
    assert all(item["msg_id"] != me_id for item in member_history)
    assert any(item["msg_id"] == me_id for item in owner_history)

    everyone_id = "browser-delete-everyone-" + uuid.uuid4().hex
    main.persist_websocket_group_message(everyone_id, group_id, owner, "remove for all", "10:22")
    deleted = client.delete(
        f"/api/messages/{everyone_id}?scope=everyone", headers=owner_headers,
    )
    assert deleted.status_code == 200, deleted.text
    member_history = client.get(f"/api/groups/{group_id}/history", headers=member_headers).json()["history"]
    tombstone = next(item for item in member_history if item["msg_id"] == everyone_id)
    assert tombstone["deleted_everyone"] == 1
    assert tombstone["content"] == "[This message was deleted]"


def test_browser_group_replies_are_saved_and_return_preview():
    owner, owner_headers, owner_token = make_user("grp_reply_owner")
    member, member_headers, member_token = make_user("grp_reply_member")
    created = client.post(
        "/api/groups", headers=owner_headers,
        json={"name": "Reply Thread", "members": [member]},
    )
    group_id = created.json()["group"]["group_id"]
    parent_id = "browser-reply-parent-" + uuid.uuid4().hex
    main.persist_websocket_group_message(parent_id, group_id, owner, "Original group question", "10:30")
    reply_id = "browser-reply-child-" + uuid.uuid4().hex

    with client.websocket_connect(f"/ws/{owner}?token={owner_token}") as owner_socket:
        with client.websocket_connect(f"/ws/{member}?token={member_token}") as member_socket:
            owner_socket.send_json({
                "type": "send_group_message", "msg_id": reply_id,
                "group_id": group_id, "content": "Answer in the group",
                "timestamp": "10:31", "reply_to_msg_id": parent_id,
                "integrity_hash": content_sha256("Answer in the group"),
            })
            status = owner_socket.receive_json()
            received = member_socket.receive_json()

    assert status["type"] == "status_update"
    assert received["type"] == "receive_message"
    assert received["reply_to_msg_id"] == parent_id
    assert received["reply_preview"]["content"] == "Original group question"
    history = client.get(f"/api/groups/{group_id}/history", headers=member_headers).json()["history"]
    reply = next(item for item in history if item["msg_id"] == reply_id)
    assert reply["reply_preview"]["msg_id"] == parent_id


def test_browser_typing_indicator_events_are_group_scoped():
    owner, owner_headers, _ = make_user("grp_typing_owner")
    member, member_headers, _ = make_user("grp_typing_member")
    created = client.post(
        "/api/groups", headers=owner_headers,
        json={"name": "Typing Test", "members": [member]},
    )
    group_id = created.json()["group"]["group_id"]

    class CaptureManager:
        def __init__(self):
            self.events = []

        async def broadcast_many_users(self, usernames, event, *, exclude_username=None):
            self.events.append((list(usernames), event, exclude_username))

    class DummySocket:
        async def send_json(self, payload):
            raise AssertionError(f"Unexpected sender-side response: {payload}")

    manager = CaptureManager()
    handler = main.handle_browser_websocket_event
    import asyncio
    assert asyncio.run(handler("typing", {"group_id": group_id}, owner, DummySocket(), manager))
    assert asyncio.run(handler("typing_stop", {"group_id": group_id}, owner, DummySocket(), manager))
    assert len(manager.events) == 2
    recipients, started, excluded = manager.events[0]
    assert set(recipients) == {owner, member}
    assert excluded == owner
    assert started == {"type": "typing", "username": owner, "group_id": group_id, "active": True}
    _, stopped, excluded = manager.events[1]
    assert excluded == owner
    assert stopped == {"type": "typing", "username": owner, "group_id": group_id, "active": False}


def test_browser_complete_search_finds_group_names_and_group_message_candidates():
    owner, owner_headers, _ = make_user("grp_search_owner")
    member, member_headers, _ = make_user("grp_search_member")
    created = client.post(
        "/api/groups", headers=owner_headers,
        json={"name": "Quantum Research Lab", "icon": "🔬", "members": [member]},
    )
    group_id = created.json()["group"]["group_id"]
    message_id = "browser-search-" + uuid.uuid4().hex
    main.persist_websocket_group_message(
        message_id, group_id, owner, "spectrometer calibration notes", "10:40"
    )

    group_results = client.get(
        "/api/search/complete?q=Quantum", headers=member_headers,
    )
    assert group_results.status_code == 200, group_results.text
    assert any(item["group_id"] == group_id for item in group_results.json()["groups"])

    message_results = client.get(
        "/api/search/complete?q=calibration", headers=member_headers,
    )
    assert message_results.status_code == 200, message_results.text
    assert any(item["msg_id"] == message_id for item in message_results.json()["messages"])
    assert client.get(
        "/api/search/complete?q=calibration", headers={},
    ).status_code == 401


def test_browser_features_script_is_loaded_and_exposes_srs_controls():
    html = (Path(__file__).parent / "static" / "index.html").read_text(encoding="utf-8")
    script = (Path(__file__).parent / "static" / "browser_features.js").read_text(encoding="utf-8")
    assert '<script src="/static/browser_features.js"></script>' in html
    assert "typing_stop" in script and "is typing" in script
    assert "Save group details" in script and "Make admin" in script
    assert "Delete for me" in script and "Delete for everyone" in script
    assert "startReplyToMessageInAnyChat" in script
    assert "/api/search/complete" in script


def test_private_history_and_legacy_search_use_authenticated_identity():
    sender, sender_headers, _ = make_user("privsend")
    recipient, recipient_headers, _ = make_user("privrecv")
    outsider, outsider_headers, _ = make_user("privout")
    own_id = "private-scope-own-" + uuid.uuid4().hex
    outsider_id = "private-scope-outsider-" + uuid.uuid4().hex
    main.persist_websocket_message(own_id, sender, recipient, "private scope needle", "10:50")
    main.persist_websocket_message(outsider_id, outsider, recipient, "outsider secret needle", "10:51")

    own_history = client.get(
        f"/api/messages/history/{sender}/{recipient}", headers=sender_headers,
    )
    assert own_history.status_code == 200, own_history.text
    assert any(item["msg_id"] == own_id for item in own_history.json()["history"])
    assert client.get(
        f"/api/messages/history/{sender}/{recipient}", headers=outsider_headers,
    ).status_code == 403

    # The legacy username query parameter is ignored; it cannot be used to search
    # another account's private conversation history.
    legacy_search = client.get(
        "/api/search", params={"username": outsider, "q": "private scope needle"},
        headers=sender_headers,
    )
    assert legacy_search.status_code == 200, legacy_search.text
    assert any(item["msg_id"] == own_id for item in legacy_search.json()["messages"])
    assert all(item["msg_id"] != outsider_id for item in legacy_search.json()["messages"])
