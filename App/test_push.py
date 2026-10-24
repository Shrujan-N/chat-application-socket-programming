"""Tests for FR-19 Push Notifications (web API + push logic).

Real push services are never contacted: push_service._send is replaced by a
function that records what would have been sent.

Run from the project root:     pytest -v
"""
import os
import sqlite3
import tempfile
import time

_tmp = tempfile.mkdtemp()
os.environ["CHAT_DB_PATH"] = os.path.join(_tmp, "test_push.db")
os.environ["JWT_SECRET"] = "test-secret"
os.environ["VAPID_KEY_FILE"] = os.path.join(_tmp, "vapid.pem")

import pytest
from fastapi.testclient import TestClient

import database
import main
import push_service
from database import init_db

init_db()
web = TestClient(main.app)

ENDPOINT = "https://fcm.googleapis.com/fcm/send/"
KEYS = {"p256dh": "BPk3Tn_dummyP256dhKeyForTests-0123456789abcdefghijklmnopqrstuvwxyzABCDEFGHIJ", "auth": "dummyAuthSecret_12"}


# ------------------------------------------------------------------ helpers
@pytest.fixture()
def sent(monkeypatch):
    """Capture pushes instead of sending them (and pretend pywebpush is installed)."""
    calls = []
    monkeypatch.setattr(push_service, "is_enabled", lambda: True)
    monkeypatch.setattr(push_service, "_send", lambda sub, payload: calls.append((sub["endpoint"], payload)))
    return calls


def make_user(name, password="Valid1234"):
    r = web.post("/api/register", json={"username": name, "email": f"{name}@example.com", "password": password})
    assert r.status_code == 200
    token = web.post("/api/login", json={"username": name, "password": password}).json()["token"]
    return {"Authorization": f"Bearer {token}"}


def token_of(headers):
    return headers["Authorization"][len("Bearer "):]


def befriend(a_headers, a, b_headers, b):
    web.post("/api/contacts/add", json={"user_id": a, "contact_username": b}, headers=a_headers)
    web.post("/api/contacts/respond", json={"user_id": b, "requester_id": a, "action": "accept"}, headers=b_headers)


def subscribe(headers, device="one"):
    return web.post("/api/push/subscribe", json={"endpoint": ENDPOINT + device, "keys": KEYS}, headers=headers)


def db(sql, params=()):
    conn = sqlite3.connect(database.DB_PATH)
    conn.row_factory = sqlite3.Row
    try:
        rows = conn.execute(sql, params).fetchall()
        conn.commit()
        return rows
    finally:
        conn.close()


def wait_for(condition, seconds=3.0):
    end = time.time() + seconds
    while time.time() < end:
        if condition():
            return True
        time.sleep(0.05)
    return condition()


def pair(a_name, b_name):
    """Two users who are contacts, with b subscribed on one device."""
    a, b = make_user(a_name), make_user(b_name)
    befriend(a, a_name, b, b_name)
    assert subscribe(b, b_name).status_code == 200
    return a, b


# ----------------------------------------------------------- service worker
def test_service_worker_is_served_from_the_site_root():
    r = web.get("/sw.js")
    assert r.status_code == 200
    assert "javascript" in r.headers["content-type"]
    assert r.headers["service-worker-allowed"] == "/"
    assert "addEventListener('push'" in r.text and "showNotification" in r.text


# ----------------------------------------------------------------- endpoints
def test_push_and_mute_endpoints_require_a_session():
    assert web.get("/api/push/public-key").status_code == 401
    assert web.post("/api/push/subscribe", json={"endpoint": ENDPOINT + "x", "keys": KEYS}).status_code == 401
    assert web.post("/api/push/unsubscribe", json={"endpoint": ENDPOINT + "x"}).status_code == 401
    assert web.post("/api/push/test").status_code == 401
    assert web.get("/api/mute").status_code == 401
    assert web.post("/api/mute", json={"username": "x"}).status_code == 401


def test_public_key_is_a_valid_vapid_key(sent):
    h = make_user("pn_key")
    r = web.get("/api/push/public-key", headers=h).json()
    assert r["enabled"] is True and len(r["public_key"]) == 87          # 65-byte uncompressed P-256 point
    assert r["public_key"] == web.get("/api/push/public-key", headers=h).json()["public_key"]   # stable


def test_public_key_reports_disabled_when_push_is_unavailable(monkeypatch):
    monkeypatch.setattr(push_service, "is_enabled", lambda: False)
    h = make_user("pn_off")
    assert web.get("/api/push/public-key", headers=h).json() == {"enabled": False, "public_key": None}


# ------------------------------------------------------------- subscriptions
def test_subscribe_stores_one_row_per_device_and_resubscribe_does_not_duplicate():
    h = make_user("pn_sub")
    assert subscribe(h, "dev1").status_code == 200
    assert subscribe(h, "dev1").status_code == 200
    assert subscribe(h, "dev2").status_code == 200
    rows = db("SELECT endpoint FROM push_subscriptions WHERE username = 'pn_sub'")
    assert sorted(r["endpoint"] for r in rows) == [ENDPOINT + "dev1", ENDPOINT + "dev2"]


def test_a_device_moves_to_the_user_who_logs_in_on_it():
    a, b = make_user("pn_own_a"), make_user("pn_own_b")
    subscribe(a, "shared")
    subscribe(b, "shared")                                               # same browser, other account
    rows = db("SELECT username FROM push_subscriptions WHERE endpoint = ?", (ENDPOINT + "shared",))
    assert [r["username"] for r in rows] == ["pn_own_b"]


@pytest.mark.parametrize("endpoint,keys", [
    ("http://fcm.googleapis.com/fcm/send/x", KEYS),             # not https
    ("https://evil.example.com/collect", KEYS),                 # not a browser push service
    ("https://fcm.googleapis.com.evil.com/x", KEYS),            # look-alike host
    ("", KEYS),
    (ENDPOINT + "x", {}),
    (ENDPOINT + "x", {"p256dh": KEYS["p256dh"]}),
    (ENDPOINT + "x", {"p256dh": "not base64 !!", "auth": "abc"}),
])
def test_invalid_subscriptions_are_rejected(endpoint, keys):
    h = make_user("pn_bad_" + str(abs(hash((endpoint, str(keys)))) % 10**8))
    r = web.post("/api/push/subscribe", json={"endpoint": endpoint, "keys": keys}, headers=h)
    assert r.status_code == 400
    assert db("SELECT COUNT(*) AS n FROM push_subscriptions WHERE endpoint = ?", (endpoint,))[0]["n"] == 0


def test_unsubscribe_removes_only_your_own_device():
    a, b = make_user("pn_un_a"), make_user("pn_un_b")
    subscribe(a, "mine")
    assert web.post("/api/push/unsubscribe", json={"endpoint": ENDPOINT + "mine"}, headers=b).status_code == 200
    assert len(db("SELECT 1 FROM push_subscriptions WHERE endpoint = ?", (ENDPOINT + "mine",))) == 1   # b cannot remove a's
    assert web.post("/api/push/unsubscribe", json={"endpoint": ENDPOINT + "mine"}, headers=a).status_code == 200
    assert db("SELECT 1 FROM push_subscriptions WHERE endpoint = ?", (ENDPOINT + "mine",)) == []


def test_test_notification_goes_to_the_users_devices(sent):
    h = make_user("pn_test")
    subscribe(h, "t1")
    r = web.post("/api/push/test", headers=h).json()
    assert r["sent"] == 1 and sent[0][0] == ENDPOINT + "t1" and sent[0][1]["type"] == "test"


def test_subscription_changes_are_audited():
    h = make_user("pn_audit")
    subscribe(h, "audit")
    web.post("/api/push/unsubscribe", json={"endpoint": ENDPOINT + "audit"}, headers=h)
    events = {r["event"] for r in db("SELECT event FROM login_logs WHERE username = 'pn_audit'")}
    assert {"push_subscribed", "push_unsubscribed"} <= events


# ---------------------------------------------------------------------- mute
def test_mute_and_unmute_a_contact():
    a, b = make_user("pn_mu_a"), make_user("pn_mu_b")
    assert web.get("/api/mute", headers=a).json() == {"muted": []}
    r = web.post("/api/mute", json={"username": "pn_mu_b", "muted": True}, headers=a)
    assert r.status_code == 200 and r.json()["muted"] == ["pn_mu_b"]
    web.post("/api/mute", json={"username": "pn_mu_b", "muted": True}, headers=a)      # twice is harmless
    assert web.get("/api/mute", headers=a).json()["muted"] == ["pn_mu_b"]
    assert web.get("/api/mute", headers=b).json()["muted"] == []                         # per user
    assert web.post("/api/mute", json={"username": "pn_mu_b", "muted": False}, headers=a).json()["muted"] == []


def test_cannot_mute_yourself_or_an_unknown_user():
    a = make_user("pn_mu_self")
    assert web.post("/api/mute", json={"username": "pn_mu_self"}, headers=a).status_code == 400
    assert web.post("/api/mute", json={"username": "nobody_here_x"}, headers=a).status_code == 404
    assert web.post("/api/mute", json={"username": ""}, headers=a).status_code == 400


# ------------------------------------------------------ what gets notified
def test_new_message_notifies_the_recipients_device_with_a_preview(sent):
    a, b = pair("pn_msg_a", "pn_msg_b")
    web.put("/api/profile", json={"display_name": "Alice A"}, headers=a)
    assert push_service.notify_new_message("m-preview-1", "pn_msg_a", "pn_msg_b", "Hello <b>there</b> &amp; welcome") == 1
    endpoint, payload = sent[0]
    assert endpoint == ENDPOINT + "pn_msg_b"
    assert payload["title"] == "Alice A" and payload["body"] == "Hello there & welcome"
    assert payload["tag"] == "msg-m-preview-1" and payload["sender"] == "pn_msg_a"


def test_long_messages_are_cut_and_attachments_get_a_label(sent):
    a, b = pair("pn_prev_a", "pn_prev_b")
    push_service.notify_new_message("m-long", "pn_prev_a", "pn_prev_b", "word " * 100)
    assert len(sent[-1][1]["body"]) <= push_service.PREVIEW_MAX and sent[-1][1]["body"].endswith("…")
    push_service.notify_new_message("m-img", "pn_prev_a", "pn_prev_b", '<div><img src="/uploads/a.png" alt="a.png"></div>')
    assert sent[-1][1]["body"] == "📷 Photo"
    push_service.notify_new_message("m-file", "pn_prev_a", "pn_prev_b", '<a href="/uploads/r.pdf" download="r.pdf">📎 Download r.pdf</a>')
    assert sent[-1][1]["body"] == "📎 r.pdf"


def test_muted_chat_gets_no_notification_until_unmuted(sent):
    a, b = pair("pn_muted_a", "pn_muted_b")
    web.post("/api/mute", json={"username": "pn_muted_a", "muted": True}, headers=b)
    assert push_service.notify_new_message("m-muted-1", "pn_muted_a", "pn_muted_b", "hi") == 0
    assert sent == []
    web.post("/api/mute", json={"username": "pn_muted_a", "muted": False}, headers=b)
    assert push_service.notify_new_message("m-muted-2", "pn_muted_a", "pn_muted_b", "hi again") == 1


def test_muting_one_contact_does_not_silence_others(sent):
    a, b = pair("pn_some_a", "pn_some_b")
    c = make_user("pn_some_c")
    befriend(c, "pn_some_c", b, "pn_some_b")
    web.post("/api/mute", json={"username": "pn_some_a", "muted": True}, headers=b)
    assert push_service.notify_new_message("m-some-1", "pn_some_a", "pn_some_b", "x") == 0
    assert push_service.notify_new_message("m-some-2", "pn_some_c", "pn_some_b", "x") == 1


def test_the_same_message_never_notifies_a_device_twice(sent):
    a, b = pair("pn_dup_a", "pn_dup_b")
    subscribe(b, "pn_dup_second_device")
    assert push_service.notify_new_message("m-dup", "pn_dup_a", "pn_dup_b", "once") == 2      # two devices, one each
    assert push_service.notify_new_message("m-dup", "pn_dup_a", "pn_dup_b", "once") == 0      # replay: nothing
    assert len(sent) == 2 and len({e for e, _ in sent}) == 2
    assert push_service.notify_new_message("m-dup-2", "pn_dup_a", "pn_dup_b", "next") == 2    # next message is new


def test_only_the_recipients_devices_are_notified(sent):
    a, b = pair("pn_only_a", "pn_only_b")
    subscribe(a, "pn_only_senders_phone")
    push_service.notify_new_message("m-only", "pn_only_a", "pn_only_b", "hi")
    assert [e for e, _ in sent] == [ENDPOINT + "pn_only_b"]


def test_no_notification_from_someone_who_is_not_a_contact(sent):
    a, b = make_user("pn_stranger_a"), make_user("pn_stranger_b")
    subscribe(b, "pn_stranger_b")
    assert push_service.notify_new_message("m-stranger", "pn_stranger_a", "pn_stranger_b", "spam") == 0
    assert sent == []


def test_expired_subscription_is_removed(monkeypatch, sent):
    a, b = pair("pn_gone_a", "pn_gone_b")

    def gone(sub, payload):
        raise push_service.PushGone()
    monkeypatch.setattr(push_service, "_send", gone)
    assert push_service.notify_new_message("m-gone", "pn_gone_a", "pn_gone_b", "hi") == 0
    assert db("SELECT 1 FROM push_subscriptions WHERE username = 'pn_gone_b'") == []


def test_a_failing_device_does_not_stop_the_others(monkeypatch, sent):
    a, b = pair("pn_fail_a", "pn_fail_b")
    subscribe(b, "pn_fail_b_second")
    delivered = []

    def flaky(sub, payload):
        if sub["endpoint"].endswith("pn_fail_b"):
            raise RuntimeError("push service unreachable")
        delivered.append(sub["endpoint"])
    monkeypatch.setattr(push_service, "_send", flaky)
    assert push_service.notify_new_message("m-fail", "pn_fail_a", "pn_fail_b", "hi") == 1
    assert delivered == [ENDPOINT + "pn_fail_b_second"]


def test_nothing_is_sent_when_push_is_switched_off(monkeypatch):
    a, b = make_user("pn_dis_a"), make_user("pn_dis_b")
    befriend(a, "pn_dis_a", b, "pn_dis_b")
    subscribe(b, "pn_dis_b")
    calls = []
    monkeypatch.setattr(push_service, "is_enabled", lambda: False)
    monkeypatch.setattr(push_service, "_send", lambda s, p: calls.append(1))
    assert push_service.notify_new_message("m-dis", "pn_dis_a", "pn_dis_b", "hi") == 0 and calls == []


# --------------------------------------------- end to end over the WebSocket
def test_offline_recipient_gets_a_push_when_a_message_is_sent(sent):
    a, b = pair("pn_ws_a", "pn_ws_b")
    with web.websocket_connect(f"/ws/pn_ws_a?token={token_of(a)}") as ws:
        ws.send_json({"type": "send_message", "msg_id": "ws-offline-1", "sender": "pn_ws_a",
                      "recipient": "pn_ws_b", "content": "are you there?", "timestamp": "10:00"})
        assert wait_for(lambda: len(sent) == 1)
    assert sent[0][0] == ENDPOINT + "pn_ws_b" and sent[0][1]["body"] == "are you there?"


def test_muted_recipient_gets_no_push_over_the_websocket(sent):
    a, b = pair("pn_wsm_a", "pn_wsm_b")
    web.post("/api/mute", json={"username": "pn_wsm_a", "muted": True}, headers=b)
    with web.websocket_connect(f"/ws/pn_wsm_a?token={token_of(a)}") as ws:
        ws.send_json({"type": "send_message", "msg_id": "ws-muted-1", "sender": "pn_wsm_a",
                      "recipient": "pn_wsm_b", "content": "hello?", "timestamp": "10:00"})
        time.sleep(0.6)
    assert sent == []
    assert db("SELECT content FROM messages WHERE msg_id = 'ws-muted-1'")[0]["content"] == "hello?"   # still stored


def test_online_recipient_in_front_gets_no_push_but_hidden_page_does(sent):
    a, b = pair("pn_wsv_a", "pn_wsv_b")
    # Using the client as a context manager gives both sockets ONE shared event loop,
    # like the real server (separate loops per socket can freeze the test).
    with TestClient(main.app) as live:
        with live.websocket_connect(f"/ws/pn_wsv_a?token={token_of(a)}") as wa, \
                live.websocket_connect(f"/ws/pn_wsv_b?token={token_of(b)}") as wb:
            # b's page is in front: the message goes straight to b, no push
            wa.send_json({"type": "send_message", "msg_id": "ws-front-1", "sender": "pn_wsv_a",
                          "recipient": "pn_wsv_b", "content": "live", "timestamp": "10:00"})
            assert wait_for(lambda: db("SELECT 1 FROM messages WHERE msg_id = 'ws-front-1'"))   # it was processed
            time.sleep(0.6)
            assert sent == []

            # b's page goes to the background: the next message triggers a push
            wb.send_json({"type": "visibility", "visible": False})
            assert wait_for(lambda: main.manager.visible.get("pn_wsv_b") is False)
            wa.send_json({"type": "send_message", "msg_id": "ws-hidden-1", "sender": "pn_wsv_a",
                          "recipient": "pn_wsv_b", "content": "while away", "timestamp": "10:01"})
            assert wait_for(lambda: len(sent) == 1)
            assert sent[0][1]["body"] == "while away"