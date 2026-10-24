"""Tests for FR-03 Session Management (web + TCP).

Run from the project root:     pytest -v
"""
import json
import os
import socket
import sqlite3
import tempfile
import threading

_tmp = tempfile.mkdtemp()
os.environ["CHAT_DB_PATH"] = os.path.join(_tmp, "test_session.db")
os.environ["JWT_SECRET"] = "test-secret"

import pytest
from fastapi.testclient import TestClient

import main
import server
from database import DB_PATH, init_db
from security import create_token, decode_token

init_db()
web = TestClient(main.app)


def auth(token):
    return {"Authorization": f"Bearer {token}"}


def web_login(user="testuser01", pw="Test@1234"):
    return web.post("/api/login", json={"username": user, "password": pw}).json()


# ------------------------------------------------------------------ web
def test_web_saved_token_restores_session():
    token = web_login()["token"]                      # token the browser keeps in localStorage
    r = web.get("/api/session", headers=auth(token))  # page reopened -> restoreSession()
    assert r.status_code == 200 and r.json()["username"] == "testuser01"


def test_web_refreshed_token_keeps_same_session_id():
    token = web_login()["token"]
    refreshed = web.get("/api/session", headers=auth(token)).json()["token"]
    assert decode_token(refreshed)["sid"] == decode_token(token)["sid"]


def test_web_expired_token_forces_login():
    expired = create_token("testuser01", ttl_seconds=-1)
    r = web.get("/api/session", headers=auth(expired))
    assert r.status_code == 401 and r.json()["detail"] == "Session expired"


def test_web_invalid_token_forces_login():
    r = web.get("/api/session", headers=auth("not.a.token"))
    assert r.status_code == 401
    tampered = web_login()["token"][:-3] + "abc"
    assert web.get("/api/session", headers=auth(tampered)).status_code == 401


def test_web_logout_revokes_saved_token():
    login = web_login("testuser02", "Pass1234")
    token = login["token"]
    refreshed = web.get("/api/session", headers=auth(token)).json()["token"]
    assert web.post("/api/logout", headers=auth(token)).status_code == 200
    # neither the old nor the refreshed copy of the token can restore the session
    assert web.get("/api/session", headers=auth(token)).status_code == 401
    assert web.get("/api/session", headers=auth(refreshed)).status_code == 401
    # logging in again gives a brand-new working session
    assert web.get("/api/session", headers=auth(web_login("testuser02", "Pass1234")["token"])).status_code == 200


def test_web_websocket_rejects_logged_out_session():
    token = web_login("shrujan", "Pass1234")["token"]
    web.post("/api/logout", headers=auth(token))
    with pytest.raises(Exception):
        with web.websocket_connect(f"/ws/shrujan?token={token}"):
            pass


# ------------------------------------------------------------------ TCP
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
        self.recv()  # welcome line

    def send(self, **event):
        self.sock.sendall((json.dumps(event) + "\n").encode())

    def recv(self):
        line = self.reader.readline()
        return json.loads(line) if line else None

    def recv_type(self, wanted):
        """Skip replayed offline messages until an event of this type arrives."""
        while True:
            e = self.recv()
            if e is None or e.get("type") == wanted:
                return e

    def ask(self, **event):
        self.send(**event)
        return self.recv()

    def close(self):
        self.sock.close()


def tcp_login(addr, user, pw):
    c = Conn(addr)
    r = c.ask(type="login", username=user, password=pw)
    assert r["success"]
    assert "Welcome" in c.recv_type("text")["message"]
    return c, r["token"]


def test_tcp_resume_without_password(address):
    c, token = tcp_login(address, "testuser01", "Test@1234")
    c.close()                                         # client app closed
    c2 = Conn(address)                                # client app reopened
    r = c2.ask(type="resume", token=token)
    assert r["success"] and r["action"] == "resume" and r["username"] == "testuser01"
    assert decode_token(r["token"])["sid"] == decode_token(token)["sid"]
    assert "Welcome" in c2.recv_type("text")["message"]
    c2.close()


def test_tcp_resume_rejects_expired_and_invalid_tokens(address):
    c = Conn(address)
    expired = create_token("testuser01", ttl_seconds=-1)
    r = c.ask(type="resume", token=expired)
    assert not r["success"] and r["message"] == "Session expired" and r["status"] == 401
    r = c.ask(type="resume", token="garbage")
    assert not r["success"] and r["message"] == "Invalid session"
    r = c.ask(type="resume")                          # no token at all
    assert not r["success"]
    # the connection stays usable: the user can still log in normally
    assert c.ask(type="login", username="testuser01", password="Test@1234")["success"]
    c.close()


def test_tcp_resume_rejects_token_of_deleted_user(address):
    c = Conn(address)
    r = c.ask(type="resume", token=create_token("ghost_user"))
    assert not r["success"] and r["message"] == "Invalid session"
    c.close()


def test_tcp_logout_revokes_saved_session(address):
    c, token = tcp_login(address, "testuser02", "Pass1234")
    c.send(type="command", message="/logout")
    assert c.recv_type("logged_out")
    c.close()
    c2 = Conn(address)
    r = c2.ask(type="resume", token=token)
    assert not r["success"] and r["message"] == "Session expired"
    c2.close()


def test_tcp_quit_keeps_session(address):
    c, token = tcp_login(address, "testuser02", "Pass1234")
    c.send(type="command", message="/quit")
    c.close()
    c2 = Conn(address)
    assert c2.ask(type="resume", token=token)["success"]
    c2.close()


def test_tcp_active_client_receives_fresh_token(address, monkeypatch):
    monkeypatch.setattr(server, "SESSION_REFRESH_SECONDS", 0)
    c, token = tcp_login(address, "shrujan", "Pass1234")
    c.send(type="command", message="/groups")
    seen = []
    while True:
        e = c.recv()
        seen.append(e)
        if e["type"] == "text":
            break
    fresh = [e for e in seen if e["type"] == "session"]
    assert fresh and decode_token(fresh[0]["token"])["sid"] == decode_token(token)["sid"]
    c.close()


def test_resume_is_audited():
    conn = sqlite3.connect(DB_PATH)
    try:
        events = {r[0] for r in conn.execute("SELECT event FROM login_logs")}
    finally:
        conn.close()
    assert "session_resumed" in events or "session_resume_failed" in events