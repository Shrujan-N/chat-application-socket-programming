"""Registration and authentication rules shared by main.py (web) and server.py (TCP)."""
import math
import os
import re
import sqlite3
from datetime import datetime, timedelta, timezone

from database import get_db
from security import hash_password, verify_password, create_token, SESSION_TIMEOUT_MINUTES

MAX_FAILED_ATTEMPTS = 5
LOCKOUT_MINUTES = int(os.environ.get("LOCKOUT_MINUTES", "15"))


class AuthError(Exception):
    """A user-facing registration/login failure with an HTTP-style status code."""

    def __init__(self, status_code: int, detail: str):
        super().__init__(detail)
        self.status_code = status_code
        self.detail = detail


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


def log_login_event(cursor, username: str, event: str, success: bool, ip):
    cursor.execute(
        """
        INSERT INTO login_logs (username, event, success, ip_address, timestamp)
        VALUES (?, ?, ?, ?, ?)
        """,
        (str(username)[:100], event, 1 if success else 0, ip, _utc_now().isoformat())
    )


def record_event(username: str, event: str, success: bool = True, ip=None):
    """Write one login_logs row using its own connection (e.g. logout)."""
    conn = get_db()
    try:
        log_login_event(conn.cursor(), username, event, success, ip)
        conn.commit()
    finally:
        conn.close()


def user_exists(username: str) -> bool:
    conn = get_db()
    try:
        cursor = conn.cursor()
        cursor.execute("SELECT 1 FROM users WHERE username = ?", (username,))
        return cursor.fetchone() is not None
    finally:
        conn.close()


# ------------------------------------------------------------ registration
def register_account(username: str, email: str, password: str) -> None:
    username = (username or "").strip()
    email = (email or "").strip().lower()
    password = password or ""

    if not username:
        raise AuthError(400, "Username required")

    if not re.fullmatch(r"[A-Za-z0-9_]{3,30}", username):
        raise AuthError(
            400, "Username must be 3-30 characters using letters, numbers, or underscores."
        )

    if len(email) > 254 or not re.fullmatch(
        r"[A-Za-z0-9._%+\-]+@[A-Za-z0-9\-]+(\.[A-Za-z0-9\-]+)*\.[A-Za-z]{2,}", email
    ):
        raise AuthError(400, "Invalid email format")

    if len(password) < 8:
        raise AuthError(400, "Password too short")

    if len(password) > 128:
        raise AuthError(400, "Password must be at most 128 characters.")

    if not (re.search(r"[a-z]", password)
            and re.search(r"[A-Z]", password)
            and re.search(r"\d", password)):
        raise AuthError(
            400, "Password must include an uppercase letter, a lowercase letter and a number."
        )

    conn = get_db()
    try:
        cursor = conn.cursor()

        cursor.execute("SELECT 1 FROM users WHERE username = ? COLLATE NOCASE", (username,))
        if cursor.fetchone():
            raise AuthError(409, "Username already taken")

        cursor.execute("SELECT 1 FROM users WHERE email = ?", (email,))
        if cursor.fetchone():
            raise AuthError(409, "Email already registered")

        try:
            cursor.execute(
                "INSERT INTO users (username, email, password) VALUES (?, ?, ?)",
                (username, email, hash_password(password))
            )
            conn.commit()
        except sqlite3.IntegrityError:
            raise AuthError(409, "Username already taken")
    finally:
        conn.close()


# ----------------------------------------------------------- authentication
def authenticate(username: str, password: str, ip=None) -> dict:
    """Verify credentials. Returns {username, token, expires_in} or raises AuthError."""
    username = (username or "").strip()
    password = password or ""

    if not username:
        raise AuthError(400, "Username required")
    if not password:
        raise AuthError(400, "Password required")

    conn = get_db()
    try:
        cursor = conn.cursor()
        cursor.execute("SELECT username, password FROM users WHERE username = ?", (username,))
        user = cursor.fetchone()

        if not user:
            log_login_event(cursor, username, "login_failed_unknown_user", False, ip)
            conn.commit()
            raise AuthError(404, "User not found")

        now = _utc_now()
        cursor.execute(
            "SELECT failed_attempts, locked_until FROM login_security WHERE username = ?",
            (username,)
        )
        security = cursor.fetchone()
        failed_attempts = security["failed_attempts"] if security else 0

        if security and security["locked_until"]:
            locked_until = datetime.fromisoformat(security["locked_until"])
            if locked_until > now:
                minutes = max(1, math.ceil((locked_until - now).total_seconds() / 60))
                log_login_event(cursor, username, "login_blocked_locked", False, ip)
                conn.commit()
                raise AuthError(
                    423,
                    f"Account locked due to too many failed attempts. "
                    f"Try again in {minutes} minute(s)."
                )
            failed_attempts = 0  # lock has expired

        if not verify_password(password, user["password"]):
            failed_attempts += 1

            if failed_attempts >= MAX_FAILED_ATTEMPTS:
                locked_until = now + timedelta(minutes=LOCKOUT_MINUTES)
                cursor.execute(
                    "INSERT OR REPLACE INTO login_security (username, failed_attempts, locked_until) VALUES (?, 0, ?)",
                    (username, locked_until.isoformat())
                )
                log_login_event(cursor, username, "account_locked", False, ip)
                conn.commit()
                raise AuthError(
                    423,
                    f"Account locked due to too many failed attempts. "
                    f"Try again in {LOCKOUT_MINUTES} minute(s)."
                )

            cursor.execute(
                "INSERT OR REPLACE INTO login_security (username, failed_attempts, locked_until) VALUES (?, ?, NULL)",
                (username, failed_attempts)
            )
            log_login_event(cursor, username, "login_failed", False, ip)
            conn.commit()
            raise AuthError(401, "Invalid credentials")

        cursor.execute(
            "INSERT OR REPLACE INTO login_security (username, failed_attempts, locked_until) VALUES (?, 0, NULL)",
            (username,)
        )

        # Upgrade an old plain-text password to a secure hash.
        if not user["password"].startswith("pbkdf2_sha256$"):
            cursor.execute(
                "UPDATE users SET password = ? WHERE username = ?",
                (hash_password(password), username)
            )

        log_login_event(cursor, username, "login_success", True, ip)
        conn.commit()

        return {
            "username": username,
            "token": create_token(username),
            "expires_in": SESSION_TIMEOUT_MINUTES * 60,
        }
    finally:
        conn.close()