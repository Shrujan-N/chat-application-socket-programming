"""Socket-level tests for server.py."""
import json
import os
import socket
import tempfile
import threading

_tmp = tempfile.mkdtemp()
os.environ["CHAT_DB_PATH"] = os.path.join(_tmp, "test_tcp.db")
os.environ["JWT_SECRET"] = "test-secret"

import pytest

import server
from database import init_db

init_db()


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
        self.first = self.recv()  # "Welcome! Please log in or register."

    def send(self, **event):
        self.sock.sendall((json.dumps(event) + "\n").encode())

    def recv(self):
        line = self.reader.readline()
        return json.loads(line) if line else None

    def ask(self, **event):
        self.send(**event)
        return self.recv()

    def close(self):
        self.sock.close()


def register(c, u, e, p):
    return c.ask(type="register", username=u, email=e, password=p)


def login(c, u, p):
    return c.ask(type="login", username=u, password=p)


def logged_in(addr, user, password):
    c = Conn(addr)
    assert login(c, user, password)["success"]
    assert "Welcome" in c.recv()["message"]
    return c


# ---------------------------------------------------------------- registration
def test_register_valid_and_login(address):
    c = Conn(address)
    assert register(c, "tcp_valid", "tcp_valid@example.com", "Valid1234")["success"]
    r = login(c, "tcp_valid", "Valid1234")
    assert r["success"] and r["token"] and r["username"] == "tcp_valid"
    c.close()


@pytest.mark.parametrize("user,email,pw,expected", [
    ("tcp_dup1", "tcp_dup@example.com", "Valid1234", None),
    ("tcp_dup2", "tcp_dup@example.com", "Valid1234", "Email already registered"),
    ("tcp_dup1", "tcp_other@example.com", "Valid1234", "Username already taken"),
    ("tcp_bad", "notanemail", "Valid1234", "Invalid email format"),
    ("tcp_bad", "' OR '1'='1' --", "Valid1234", "Invalid email format"),
    ("tcp_bad", "bad@example.com", "123", "Password too short"),
])
def test_register_validation(address, user, email, pw, expected):
    c = Conn(address)
    r = register(c, user, email, pw)
    if expected is None:
        assert r["success"]
    else:
        assert not r["success"] and r["message"] == expected
    c.close()


# ------------------------------------------------------------------ login
def test_login_errors(address):
    c = Conn(address)
    assert login(c, "testuser02", "WrongPass")["message"] == "Invalid credentials"
    assert login(c, "nonexistent", "Pass123")["message"] == "User not found"
    assert login(c, "", "")["message"] == "Username required"
    c.close()


def test_lockout_after_five_failures(address):
    c = Conn(address)
    register(c, "tcp_brute", "tcp_brute@example.com", "Valid1234")
    for _ in range(4):
        assert login(c, "tcp_brute", "bad")["message"] == "Invalid credentials"
    assert "locked" in login(c, "tcp_brute", "bad")["message"].lower()
    assert "locked" in login(c, "tcp_brute", "Valid1234")["message"].lower()
    c.close()


def test_chat_requires_login_first(address):
    c = Conn(address)
    r = c.ask(type="chat", message="hi")
    assert not r["success"] and "log in" in r["message"].lower()
    c.close()


# ------------------------------------------- original chat features still work
def test_private_chat_between_authenticated_users(address):
    a = logged_in(address, "testuser01", "Test@1234")
    b = logged_in(address, "testuser02", "Pass1234")
    a.send(type="command", message="/chat testuser02")
    assert "Private chat selected" in a.recv()["message"]
    assert "Private chat started" in b.recv()["message"]
    a.send(type="chat", message_id="m1", message="hello over tcp")
    got = b.recv()
    assert got["type"] == "private_message" and got["message"] == "hello over tcp"
    assert got["sender"] == "testuser01"
    a.send(type="command", message="/chat nobody_here")
    reply = a.recv()
    while reply["type"] != "text":      # skip the earlier "sent" status event
        reply = a.recv()
    assert "not registered" in reply["message"]
    a.close(); b.close()


def test_duplicate_session_rejected(address):
    first = logged_in(address, "shrujan", "Pass1234")
    second = Conn(address)
    assert login(second, "shrujan", "Pass1234")["success"]
    assert second.recv()["message"] == "ERROR: Username already in use."
    first.close(); second.close()


def test_idle_session_times_out(address, monkeypatch):
    monkeypatch.setattr(server, "SESSION_TIMEOUT_MINUTES", 0.02)  # ~1.2 seconds
    c = logged_in(address, "tcp_valid", "Valid1234")
    msg = c.recv()
    assert "Session expired" in msg["message"]
    c.close()