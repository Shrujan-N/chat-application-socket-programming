"""Tests for MODULE 1 (User Registration) and MODULE 2 (User Authentication).

Run from the project root:     pytest -v
"""
import os
import sqlite3
import tempfile

_tmp = tempfile.mkdtemp()
os.environ["CHAT_DB_PATH"] = os.path.join(_tmp, "test_chat.db")
os.environ["JWT_SECRET"] = "test-secret"

import pytest
from fastapi.testclient import TestClient

import main
from database import DB_PATH
from security import create_token, decode_token

client = TestClient(main.app)


def register(username, email, password):
    return client.post("/api/register", json={"username": username, "email": email, "password": password})


def login(username, password):
    return client.post("/api/login", json={"username": username, "password": password})


def db_rows(sql, params=()):
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    try:
        return conn.execute(sql, params).fetchall()
    finally:
        conn.close()


# ------------------------------------------------------------ MODULE 1
def test_UT_REG_01_valid_registration():
    r = register("reg_valid", "reg_valid@example.com", "Valid1234")
    assert r.status_code == 200 and r.json()["success"] is True


def test_UT_REG_02_duplicate_email():
    register("dup_email_a", "dup@example.com", "Valid1234")
    r = register("dup_email_b", "dup@example.com", "Valid1234")
    assert r.status_code == 409 and r.json()["detail"] == "Email already registered"


def test_UT_REG_03_duplicate_username():
    register("dup_user", "dup_user1@example.com", "Valid1234")
    r = register("dup_user", "dup_user2@example.com", "Valid1234")
    assert r.status_code == 409 and r.json()["detail"] == "Username already taken"


def test_UT_REG_04_invalid_email_format():
    r = register("bad_email", "notanemail", "Valid1234")
    assert r.status_code == 400 and r.json()["detail"] == "Invalid email format"


def test_UT_REG_05_weak_password_too_short():
    r = register("weak_pw", "weak@example.com", "123")
    assert r.status_code == 400 and r.json()["detail"] == "Password too short"


def test_IT_REG_01_user_record_in_database():
    register("it_reg", "it_reg@example.com", "Valid1234")
    rows = db_rows("SELECT username, email, password FROM users WHERE username = ?", ("it_reg",))
    assert len(rows) == 1
    assert rows[0]["email"] == "it_reg@example.com"
    assert rows[0]["password"].startswith("pbkdf2_sha256$")  # stored hashed, not plain text


def test_ST_REG_01_register_then_login():
    assert register("flow_user", "flow@example.com", "Valid1234").status_code == 200
    r = login("flow_user", "Valid1234")
    assert r.status_code == 200 and r.json()["success"] is True


def test_ST_REG_02_sql_injection_in_email():
    before = db_rows("SELECT COUNT(*) AS n FROM users")[0]["n"]
    r = register("sqli_user", "' OR '1'='1' --", "Valid1234")
    assert r.status_code == 400 and r.json()["detail"] == "Invalid email format"
    r = register("sqli_user2", "a@b.com'; DROP TABLE users; --", "Valid1234")
    assert r.status_code == 400
    assert db_rows("SELECT COUNT(*) AS n FROM users")[0]["n"] == before  # table intact, nothing inserted


# ------------------------------------------------------------ MODULE 2
def test_UT_AUTH_01_valid_login():
    r = login("testuser01", "Test@1234")
    assert r.status_code == 200
    body = r.json()
    assert body["success"] is True and body["username"] == "testuser01" and body["token"]


def test_UT_AUTH_02_wrong_password():
    r = login("testuser02", "WrongPass")
    assert r.status_code == 401 and r.json()["detail"] == "Invalid credentials"


def test_UT_AUTH_03_non_existent_user():
    r = login("nonexistent", "Pass123")
    assert r.status_code == 404 and r.json()["detail"] == "User not found"


def test_UT_AUTH_04_empty_fields():
    r = login("", "")
    assert r.status_code == 400 and r.json()["detail"] == "Username required"


def test_UT_AUTH_05_jwt_token_generated():
    token = login("testuser01", "Test@1234").json()["token"]
    assert len(token.split(".")) == 3                      # header.payload.signature
    assert decode_token(token)["sub"] == "testuser01"
    # token is accepted by protected endpoints and refreshes the session
    r = client.get("/api/session", headers={"Authorization": f"Bearer {token}"})
    assert r.status_code == 200 and r.json()["username"] == "testuser01"


def test_IT_AUTH_01_login_event_logged_with_timestamp():
    login("testuser01", "Test@1234")
    rows = db_rows("SELECT * FROM login_logs WHERE username = ? AND event = 'login_success'", ("testuser01",))
    assert rows and rows[-1]["timestamp"] and rows[-1]["success"] == 1


def test_ST_AUTH_01_brute_force_lockout_after_5_failures():
    register("brute_user", "brute@example.com", "Valid1234")
    for _ in range(4):
        assert login("brute_user", "wrong").status_code == 401
    assert login("brute_user", "wrong").status_code == 423          # 5th failure locks the account
    r = login("brute_user", "Valid1234")                           # correct password is refused while locked
    assert r.status_code == 423 and "locked" in r.json()["detail"].lower()
    lock = db_rows("SELECT locked_until FROM login_security WHERE username = ?", ("brute_user",))
    assert lock and lock[0]["locked_until"]


def test_ST_AUTH_02_session_timeout_redirects_to_login():
    expired = create_token("testuser01", ttl_seconds=-1)           # simulates 30+ idle minutes
    r = client.get("/api/session", headers={"Authorization": f"Bearer {expired}"})
    assert r.status_code == 401 and r.json()["detail"] == "Session expired"
    r = client.get("/api/contacts/testuser01", headers={"Authorization": f"Bearer {expired}"})
    assert r.status_code == 401                                    # front-end sends user back to login


def test_protected_endpoints_require_token():
    assert client.get("/api/contacts/testuser01").status_code == 401