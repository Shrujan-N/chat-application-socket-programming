"""Tests for FR-04 Password Recovery (web API + TCP server).

Run from the project root:     pytest -v
"""
import json
import os
import socket
import sqlite3
import tempfile
import threading
import time

_tmp = tempfile.mkdtemp()
os.environ["CHAT_DB_PATH"] = os.path.join(_tmp, "test_reset.db")
os.environ["JWT_SECRET"] = "test-secret"

import pytest
from fastapi.testclient import TestClient

import auth_service
import mailer
import main
import server
from database import DB_PATH, init_db

init_db()
web = TestClient(main.app)

FORGOT_MESSAGE = "If that email is registered, a password reset link has been sent."


# ------------------------------------------------------------------ helpers
@pytest.fixture()
def outbox(monkeypatch):
    """Capture emails instead of sending them."""
    sent = []
    monkeypatch.setattr(mailer, "send_email", lambda to, subject, body: sent.append((to, subject, body)))
    return sent


def token_from(email):
    """The reset code contained in an email body."""
    body = email[2]
    link = [line for line in body.splitlines() if "/#reset=" in line][0]
    return link.split("#reset=")[1].strip()


def db(sql, params=()):
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    try:
        rows = conn.execute(sql, params).fetchall()
        conn.commit()
        return rows
    finally:
        conn.close()


def make_user(name, password="Valid1234"):
    r = web.post("/api/register", json={"username": name, "email": f"{name}@example.com", "password": password})
    assert r.status_code == 200
    return f"{name}@example.com"


def forgot(email):
    return web.post("/api/password/forgot", json={"email": email})


def reset(token, password):
    return web.post("/api/password/reset", json={"token": token, "new_password": password})


def login(user, password):
    return web.post("/api/login", json={"username": user, "password": password})


# ------------------------------------------------------------ requesting a reset
def test_forgot_sends_email_with_link(outbox):
    email = make_user("pr_send")
    r = forgot(email)
    assert r.status_code == 200 and r.json()["success"] and r.json()["message"] == FORGOT_MESSAGE
    assert len(outbox) == 1 and outbox[0][0] == email
    assert "/#reset=" in outbox[0][2] and "15 minutes" in outbox[0][2]


def test_forgot_unknown_email_looks_identical_and_sends_nothing(outbox):
    known = make_user("pr_known")
    a = forgot(known)
    b = forgot("nobody_here@example.com")
    assert a.status_code == b.status_code == 200
    assert a.json() == b.json()                      # cannot be used to find registered emails
    assert len(outbox) == 1


def test_forgot_rejects_invalid_email_format(outbox):
    r = forgot("not-an-email")
    assert r.status_code == 400 and r.json()["detail"] == "Invalid email format"
    r = forgot("a@b.com'; DROP TABLE users; --")
    assert r.status_code == 400
    assert outbox == []


def test_token_stored_hashed_only(outbox):
    email = make_user("pr_hash")
    forgot(email)
    token = token_from(outbox[0])
    rows = db("SELECT token_hash FROM password_resets WHERE username = 'pr_hash'")
    assert rows and token not in rows[0]["token_hash"] and len(rows[0]["token_hash"]) == 64


def test_forgot_is_rate_limited_without_revealing_it(outbox):
    email = make_user("pr_rate")
    for _ in range(auth_service.RESET_MAX_REQUESTS_PER_HOUR + 2):
        assert forgot(email).status_code == 200
    assert len(outbox) == auth_service.RESET_MAX_REQUESTS_PER_HOUR


# --------------------------------------------------------------- using the link
def test_full_reset_flow_and_login_with_new_password(outbox):
    email = make_user("pr_flow")
    forgot(email)
    token = token_from(outbox[0])
    assert web.post("/api/password/verify", json={"token": token}).json() == {"valid": True}

    r = reset(token, "Brand0New")
    assert r.status_code == 200 and r.json()["success"]
    assert login("pr_flow", "Brand0New").status_code == 200       # new password works
    assert login("pr_flow", "Valid1234").status_code == 401       # old password no longer works


def test_link_is_single_use(outbox):
    email = make_user("pr_once")
    forgot(email)
    token = token_from(outbox[0])
    assert reset(token, "Brand0New").status_code == 200
    r = reset(token, "Another0ne")
    assert r.status_code == 400 and r.json()["detail"] == "Invalid or expired reset link"
    assert login("pr_once", "Brand0New").status_code == 200       # unchanged by the second attempt
    assert web.post("/api/password/verify", json={"token": token}).json() == {"valid": False}


def test_expired_link_is_rejected(outbox):
    email = make_user("pr_exp")
    forgot(email)
    token = token_from(outbox[0])
    db("UPDATE password_resets SET expires_at = ? WHERE username = 'pr_exp'", (int(time.time()) - 1,))
    r = reset(token, "Brand0New")
    assert r.status_code == 400 and r.json()["detail"] == "Invalid or expired reset link"
    assert web.post("/api/password/verify", json={"token": token}).json() == {"valid": False}
    assert login("pr_exp", "Valid1234").status_code == 200        # password not changed


@pytest.mark.parametrize("token", ["", "garbage", "A" * 43])
def test_invalid_link_is_rejected(token):
    make_user(f"pr_bad{len(token)}")
    r = reset(token, "Brand0New")
    assert r.status_code == 400 and r.json()["detail"] == "Invalid or expired reset link"


def test_weak_password_rejected_and_link_still_usable(outbox):
    email = make_user("pr_weak")
    forgot(email)
    token = token_from(outbox[0])
    assert reset(token, "123").json()["detail"] == "Password too short"
    assert "uppercase" in reset(token, "alllowercase1").json()["detail"]
    assert reset(token, "Brand0New").status_code == 200           # typo did not burn the link


def test_newest_link_replaces_older_one(outbox):
    email = make_user("pr_new")
    forgot(email)
    forgot(email)
    first, second = token_from(outbox[0]), token_from(outbox[1])
    assert reset(first, "Brand0New").status_code == 400
    assert reset(second, "Brand0New").status_code == 200


def test_reset_unlocks_a_locked_account(outbox):
    email = make_user("pr_lock")
    for _ in range(5):
        login("pr_lock", "wrong")
    assert login("pr_lock", "Valid1234").status_code == 423
    forgot(email)
    assert reset(token_from(outbox[0]), "Brand0New").status_code == 200
    assert login("pr_lock", "Brand0New").status_code == 200


def test_reset_ends_existing_sessions(outbox):
    email = make_user("pr_sess")
    old = login("pr_sess", "Valid1234").json()["token"]
    headers = {"Authorization": f"Bearer {old}"}
    assert web.get("/api/session", headers=headers).status_code == 200
    forgot(email)
    reset(token_from(outbox[0]), "Brand0New")
    assert web.get("/api/session", headers=headers).status_code == 401      # attacker's old session is dead
    new = login("pr_sess", "Brand0New").json()["token"]
    assert web.get("/api/session", headers={"Authorization": f"Bearer {new}"}).status_code == 200


def test_reset_endpoints_are_public_but_chat_api_is_not():
    assert web.post("/api/password/verify", json={"token": "x"}).status_code == 200
    assert web.get("/api/users").status_code == 401


def test_password_recovery_is_audited(outbox):
    email = make_user("pr_audit")
    forgot(email)
    reset(token_from(outbox[0]), "Brand0New")
    reset("garbage", "Brand0New")
    events = {r["event"] for r in db("SELECT event FROM login_logs WHERE username IN ('pr_audit','(unknown)')")}
    assert {"password_reset_requested", "password_reset_success", "password_reset_failed"} <= events


# ------------------------------------------------------------------------ TCP
@pytest.fixture(scope="module")
def address():
    listener = socket.socket()
    listener.bind(("127.0.0.1", 0))
    listener.listen()
    stop = threading.Event()

    def accept_loop():
        listener.settimeout(0.2)
        while not stop.is_set():
            try:
                conn, addr = listener.accept()
            except (socket.timeout, OSError):
                continue
            threading.Thread(target=server.handle_client, args=(conn, addr), daemon=True).start()

    threading.Thread(target=accept_loop, daemon=True).start()
    yield listener.getsockname()
    stop.set()
    listener.close()


class Conn:
    def __init__(self, addr):
        self.sock = socket.create_connection(addr, timeout=5)
        self.reader = self.sock.makefile("r", encoding="utf-8")
        self.reader.readline()  # welcome line

    def ask(self, **event):
        self.sock.sendall((json.dumps(event) + "\n").encode())
        return json.loads(self.reader.readline())

    def close(self):
        self.sock.close()


def test_tcp_forgot_and_reset_then_login(address, outbox):
    email = make_user("pr_tcp")
    c = Conn(address)
    r = c.ask(type="forgot_password", email=email)
    assert r["success"] and r["action"] == "forgot_password" and r["message"] == FORGOT_MESSAGE
    token = token_from(outbox[0])
    r = c.ask(type="reset_password", token=token, new_password="Brand0New")
    assert r["success"] and r["action"] == "reset_password"
    assert not c.ask(type="login", username="pr_tcp", password="Valid1234")["success"]
    assert c.ask(type="login", username="pr_tcp", password="Brand0New")["success"]
    c.close()


def test_tcp_invalid_and_reused_codes_rejected(address, outbox):
    email = make_user("pr_tcp2")
    c = Conn(address)
    r = c.ask(type="reset_password", token="garbage", new_password="Brand0New")
    assert not r["success"] and r["message"] == "Invalid or expired reset link"
    c.ask(type="forgot_password", email=email)
    token = token_from(outbox[0])
    assert c.ask(type="reset_password", token=token, new_password="short")["message"] == "Password too short"
    assert c.ask(type="reset_password", token=token, new_password="Brand0New")["success"]
    assert not c.ask(type="reset_password", token=token, new_password="Another0ne")["success"]
    c.close()


def test_tcp_unknown_email_same_answer(address, outbox):
    c = Conn(address)
    r = c.ask(type="forgot_password", email="ghost@example.com")
    assert r["success"] and r["message"] == FORGOT_MESSAGE and outbox == []
    assert not c.ask(type="forgot_password", email="bad")["success"]
    c.close()