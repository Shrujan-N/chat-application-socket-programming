"""Registration and authentication rules shared by main.py (web) and server.py (TCP)."""
import hashlib
import math
import os
import re
import secrets
import sqlite3
import time
from datetime import datetime, timedelta, timezone

import mailer
from database import get_db
from security import (
    hash_password, verify_password, create_token, decode_token,
    TokenError, SESSION_TIMEOUT_MINUTES,
)

MAX_FAILED_ATTEMPTS = 5
LOCKOUT_MINUTES = int(os.environ.get("LOCKOUT_MINUTES", "15"))

# Password recovery (FR-04)
RESET_TOKEN_MINUTES = int(os.environ.get("RESET_TOKEN_MINUTES", "15"))
RESET_MAX_REQUESTS_PER_HOUR = int(os.environ.get("RESET_MAX_REQUESTS_PER_HOUR", "3"))
RESET_BASE_URL = os.environ.get("RESET_BASE_URL", "http://127.0.0.1:8000").rstrip("/")
RESET_REQUEST_MESSAGE = "If that email is registered, a password reset link has been sent."
INVALID_RESET_MESSAGE = "Invalid or expired reset link"

EMAIL_PATTERN = r"[A-Za-z0-9._%+\-]+@[A-Za-z0-9\-]+(\.[A-Za-z0-9\-]+)*\.[A-Za-z]{2,}"


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


# ------------------------------------------------------- shared validation
def _check_email(email: str) -> None:
    if len(email) > 254 or not re.fullmatch(EMAIL_PATTERN, email):
        raise AuthError(400, "Invalid email format")


def _check_password(password: str) -> None:
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

    _check_email(email)
    _check_password(password)

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


# ------------------------------------------------- session management (FR-03)
def _is_revoked(sid: str) -> bool:
    conn = get_db()
    try:
        row = conn.execute("SELECT 1 FROM revoked_sessions WHERE sid = ?", (sid,)).fetchone()
        return row is not None
    finally:
        conn.close()


def _issued_before_password_change(claims: dict) -> bool:
    """True if the user's password was reset after this token was issued."""
    conn = get_db()
    try:
        row = conn.execute(
            "SELECT changed_at_ms FROM password_changes WHERE username = ?",
            (claims.get("sub", ""),)
        ).fetchone()
    finally:
        conn.close()

    if not row:
        return False

    issued_ms = claims.get("iat_ms", int(claims.get("iat", 0)) * 1000)
    return issued_ms < row["changed_at_ms"]


def validate_session(token: str) -> dict:
    """Return the token claims, or raise TokenError if the session is
    missing, invalid, expired or has been logged out."""
    claims = decode_token(token)
    sid = claims.get("sid")
    if sid and _is_revoked(sid):
        raise TokenError("Session expired")
    if _issued_before_password_change(claims):
        raise TokenError("Session expired")
    return claims


def revoke_session(token: str) -> None:
    """Log out: make this session's token (and its refreshed copies) unusable."""
    try:
        claims = decode_token(token)
    except TokenError:
        return  # already expired/invalid - nothing to revoke
    sid = claims.get("sid")
    if not sid:
        return
    conn = get_db()
    try:
        now = int(time.time())
        conn.execute("DELETE FROM revoked_sessions WHERE expires_at < ?", (now,))
        conn.execute(
            "INSERT OR IGNORE INTO revoked_sessions (sid, username, expires_at) VALUES (?, ?, ?)",
            (sid, claims.get("sub", ""), int(claims.get("exp", now)))
        )
        conn.commit()
    finally:
        conn.close()


def refresh_session_token(token: str) -> dict:
    """Sliding session: issue a fresh token for the same session.
    Raises TokenError if the session is no longer valid."""
    claims = validate_session(token)
    return {
        "username": claims["sub"],
        "token": create_token(claims["sub"], sid=claims.get("sid")),
        "expires_in": SESSION_TIMEOUT_MINUTES * 60,
    }


def resume_session(token: str, ip=None) -> dict:
    """Return to an existing session using a saved token (no password needed).
    Returns {username, token, expires_in} or raises AuthError(401)."""
    try:
        result = refresh_session_token(token)
    except TokenError as error:
        record_event("(unknown)", "session_resume_failed", False, ip)
        raise AuthError(401, str(error))

    username = result["username"]
    if not user_exists(username):
        record_event(username, "session_resume_failed", False, ip)
        raise AuthError(401, "Invalid session")

    record_event(username, "session_resumed", True, ip)
    return result



# ------------------------------------------------- password recovery (FR-04)
def _hash_reset_token(token: str) -> str:
    """Only this hash is stored, so a leaked database cannot be used to reset accounts."""
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def request_password_reset(email: str, ip=None) -> None:
    """Email a time-limited, single-use reset link.

    Behaves identically whether or not the email is registered (and when the
    rate limit is hit), so it cannot be used to discover which emails have
    accounts. Only an invalid email format is reported.
    """
    email = (email or "").strip().lower()
    _check_email(email)

    token = None
    username = None

    conn = get_db()
    try:
        cursor = conn.cursor()
        cursor.execute("SELECT username FROM users WHERE email = ?", (email,))
        user = cursor.fetchone()

        if not user:
            log_login_event(cursor, "(unknown)", "password_reset_unknown_email", False, ip)
            conn.commit()
            return

        username = user["username"]
        now = int(time.time())

        cursor.execute("DELETE FROM password_resets WHERE expires_at < ?", (now - 86400,))
        cursor.execute(
            "SELECT COUNT(*) FROM password_resets WHERE username = ? AND created_at > ?",
            (username, now - 3600)
        )
        if cursor.fetchone()[0] >= RESET_MAX_REQUESTS_PER_HOUR:
            log_login_event(cursor, username, "password_reset_rate_limited", False, ip)
            conn.commit()
            return

        # Only the newest link works: retire any earlier unused ones.
        cursor.execute(
            "UPDATE password_resets SET used = 1 WHERE username = ? AND used = 0",
            (username,)
        )

        token = secrets.token_urlsafe(32)
        cursor.execute(
            """
            INSERT INTO password_resets (token_hash, username, created_at, expires_at, used)
            VALUES (?, ?, ?, ?, 0)
            """,
            (_hash_reset_token(token), username, now, now + RESET_TOKEN_MINUTES * 60)
        )
        log_login_event(cursor, username, "password_reset_requested", True, ip)
        conn.commit()
    finally:
        conn.close()

    try:
        mailer.send_reset_email(
            email, username, f"{RESET_BASE_URL}/#reset={token}", token, RESET_TOKEN_MINUTES
        )
    except Exception as error:  # delivery problems must not reveal anything to the requester
        print(f"Could not send password reset email: {error}")


def check_reset_token(token: str) -> bool:
    """True if the reset link/code is still usable (exists, unused, not expired)."""
    token = (token or "").strip()
    if not token:
        return False

    conn = get_db()
    try:
        row = conn.execute(
            "SELECT used, expires_at FROM password_resets WHERE token_hash = ?",
            (_hash_reset_token(token),)
        ).fetchone()
    finally:
        conn.close()

    return bool(row and not row["used"] and row["expires_at"] > int(time.time()))


def reset_password(token: str, new_password: str, ip=None) -> None:
    """Set a new password using a valid reset token, or raise AuthError."""
    token = (token or "").strip()
    new_password = new_password or ""
    token_hash = _hash_reset_token(token)

    conn = get_db()
    try:
        cursor = conn.cursor()
        now = int(time.time())

        cursor.execute(
            "SELECT username, used, expires_at FROM password_resets WHERE token_hash = ?",
            (token_hash,)
        )
        row = cursor.fetchone() if token else None

        if not row or row["used"] or row["expires_at"] <= now:
            log_login_event(cursor, row["username"] if row else "(unknown)",
                            "password_reset_failed", False, ip)
            conn.commit()
            raise AuthError(400, INVALID_RESET_MESSAGE)

        # A weak password is rejected without using up the link.
        _check_password(new_password)

        # Mark used first; the WHERE clause makes the link single-use even if
        # two requests arrive at the same moment.
        cursor.execute(
            "UPDATE password_resets SET used = 1 WHERE token_hash = ? AND used = 0 AND expires_at > ?",
            (token_hash, now)
        )
        if cursor.rowcount != 1:
            conn.rollback()
            raise AuthError(400, INVALID_RESET_MESSAGE)

        username = row["username"]
        cursor.execute(
            "UPDATE users SET password = ? WHERE username = ?",
            (hash_password(new_password), username)
        )
        # The user can sign in straight away, even if the account was locked.
        cursor.execute("DELETE FROM login_security WHERE username = ?", (username,))
        # Existing sessions (possibly an attacker's) stop working.
        cursor.execute(
            "INSERT OR REPLACE INTO password_changes (username, changed_at_ms) VALUES (?, ?)",
            (username, int(time.time() * 1000))
        )
        log_login_event(cursor, username, "password_reset_success", True, ip)
        conn.commit()
    finally:
        conn.close()