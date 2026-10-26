from fastapi import FastAPI, WebSocket, WebSocketDisconnect, UploadFile, File, HTTPException, Request
from fastapi.staticfiles import StaticFiles
from fastapi.responses import HTMLResponse, JSONResponse, Response
import asyncio
import logging
import time
import os
import re
import math
import shutil
import sqlite3
from collections import deque
from logging.handlers import RotatingFileHandler
from datetime import datetime, timedelta, timezone
from pydantic import BaseModel
from typing import Dict, Optional
from database import init_db as init_core_db, get_db as core_get_db
from security import (
    hash_password, verify_password, create_token, decode_token,
    TokenError, SESSION_TIMEOUT_MINUTES,
)
from starlette.concurrency import run_in_threadpool
from auth_service import (
    validate_session, revoke_session, AuthError, request_password_reset,
    reset_password, check_reset_token, RESET_REQUEST_MESSAGE,
)
import profile_service
import push_service
 
 
class RegisterSchema(BaseModel):
    username: str = ""
    email: str = ""
    password: str = ""
 
 
class LoginSchema(BaseModel):
    username: str = ""
    password: str = ""


class ForgotPasswordSchema(BaseModel):
    email: str = ""


class VerifyResetSchema(BaseModel):
    token: str = ""


class ResetPasswordSchema(BaseModel):
    token: str = ""
    new_password: str = ""


class ProfileUpdateSchema(BaseModel):
    display_name: Optional[str] = None
    bio: Optional[str] = None


class PushSubscribeSchema(BaseModel):
    endpoint: str = ""
    keys: Dict[str, str] = {}
    expirationTime: Optional[float] = None


class PushUnsubscribeSchema(BaseModel):
    endpoint: str = ""


class MuteSchema(BaseModel):
    username: str = ""
    muted: bool = True


class BlockUserSchema(BaseModel):
    username: str = ""


class ReportUserSchema(BaseModel):
    username: str = ""
    reason: str = ""
 
 
MAX_FAILED_ATTEMPTS = 5
LOCKOUT_MINUTES = int(os.environ.get("LOCKOUT_MINUTES", "15"))
PUBLIC_API_PATHS = {
    "/api/register", "/api/login",
    "/api/password/forgot", "/api/password/verify", "/api/password/reset",
}
 
app = FastAPI(title="Chat Application - Socket Programming")
GIPHY_API_KEY = os.environ.get("GIPHY_API_KEY", "").strip()
import asyncio
import shutil
import uuid
from datetime import datetime, timezone
from pathlib import Path

from fastapi import FastAPI, File, HTTPException, UploadFile, WebSocket, WebSocketDisconnect
from fastapi.responses import HTMLResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field
from web_database import get_db, init_db as init_web_db

BASE = Path(__file__).resolve().parent
STATIC = BASE / "static"
UPLOADS = BASE / "uploads"
STATIC.mkdir(exist_ok=True)
UPLOADS.mkdir(exist_ok=True)
init_core_db()
init_web_db()

APP_DIR = os.path.dirname(os.path.abspath(__file__))
STATIC_DIR = os.path.join(APP_DIR, "static")
UPLOADS_DIR = profile_service.UPLOADS_DIR   # same folder as before (APP_DIR/uploads) unless CHAT_UPLOADS_DIR is set


# --- Application logging and request diagnostics (Issue #51) ---
LOG_DIR = Path(APP_DIR) / "logs"
LOG_DIR.mkdir(parents=True, exist_ok=True)

logger = logging.getLogger("chat_application")
logger.setLevel(logging.INFO)
logger.propagate = False

if not logger.handlers:
    file_handler = RotatingFileHandler(
        LOG_DIR / "server.log",
        maxBytes=1_000_000,
        backupCount=5,
        encoding="utf-8",
    )
    formatter = logging.Formatter(
        "%(asctime)s | %(levelname)s | %(name)s | %(message)s"
    )
    file_handler.setFormatter(formatter)
    logger.addHandler(file_handler)

    console_handler = logging.StreamHandler()
    console_handler.setFormatter(formatter)
    logger.addHandler(console_handler)

SERVER_STARTED_AT = time.time()
REQUEST_METRICS = {
    "total_requests": 0,
    "server_errors": 0,
    "slow_requests": 0,
    "total_duration_ms": 0.0,
}
RECENT_REQUESTS = deque(maxlen=20)
SLOW_REQUEST_THRESHOLD_SECONDS = 1.5


# --- Session protection: every /api route except register/login needs a valid token ---
def get_bearer_token(request: Request) -> str:
    header = request.headers.get("Authorization", "")
    return header[7:].strip() if header.startswith("Bearer ") else ""
 
 
@app.middleware("http")
async def require_session(request: Request, call_next):
    started = time.perf_counter()
    path = request.url.path

    if path.startswith("/api/") and path not in PUBLIC_API_PATHS:
        try:
            validate_session(get_bearer_token(request))
        except TokenError:
            logger.warning(
                "Rejected unauthenticated API request: method=%s path=%s",
                request.method,
                path,
            )
            response = JSONResponse(
                status_code=401,
                content={"detail": "Authentication required"},
            )
            record_request_metric(request.method, path, response.status_code,
                                  time.perf_counter() - started)
            return response

    try:
        response = await call_next(request)
    except Exception:
        duration = time.perf_counter() - started
        record_request_metric(request.method, path, 500, duration)
        logger.exception(
            "Unhandled request error: method=%s path=%s duration_seconds=%.3f",
            request.method,
            path,
            duration,
        )
        raise

    duration = time.perf_counter() - started
    record_request_metric(request.method, path, response.status_code, duration)

    if response.status_code >= 500:
        logger.error(
            "HTTP server error: method=%s path=%s status=%s duration_seconds=%.3f",
            request.method,
            path,
            response.status_code,
            duration,
        )
    elif duration >= SLOW_REQUEST_THRESHOLD_SECONDS:
        logger.warning(
            "Slow HTTP request: method=%s path=%s status=%s duration_seconds=%.3f",
            request.method,
            path,
            response.status_code,
            duration,
        )
    else:
        logger.info(
            "HTTP request: method=%s path=%s status=%s duration_seconds=%.3f",
            request.method,
            path,
            response.status_code,
            duration,
        )
    return response


def record_request_metric(method: str, path: str, status: int, duration: float):
    REQUEST_METRICS["total_requests"] += 1
    REQUEST_METRICS["total_duration_ms"] += duration * 1000

    if status >= 500:
        REQUEST_METRICS["server_errors"] += 1
    if duration >= SLOW_REQUEST_THRESHOLD_SECONDS:
        REQUEST_METRICS["slow_requests"] += 1

    RECENT_REQUESTS.appendleft({
        "method": method,
        "path": path,
        "status": status,
        "duration_ms": round(duration * 1000, 2),
        "timestamp": utc_now().isoformat(),
    })


@app.get("/api/diagnostics/summary")
async def diagnostics_summary():
    """Return aggregate diagnostics to an authenticated user; never expose log contents."""
    total = REQUEST_METRICS["total_requests"]
    average = REQUEST_METRICS["total_duration_ms"] / total if total else 0.0
    return {
        "status": "ok",
        "uptime_seconds": round(time.time() - SERVER_STARTED_AT, 1),
        "total_requests": total,
        "server_errors": REQUEST_METRICS["server_errors"],
        "slow_requests": REQUEST_METRICS["slow_requests"],
        "average_response_ms": round(average, 2),
        "recent_requests": list(RECENT_REQUESTS),
        "log_file": "Server-side rotating log enabled",
    }
 
 
# Mount Static Files and Uploads
app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")
 
os.makedirs(UPLOADS_DIR, exist_ok=True)
app.mount("/uploads", StaticFiles(directory=UPLOADS_DIR), name="uploads")
 
@app.get("/")
async def get_index():
    with open(os.path.join(STATIC_DIR, "index.html"), "r", encoding="utf-8") as f:
        return HTMLResponse(content=f.read())
 
 
def utc_now() -> datetime:
    return datetime.now(timezone.utc)
 
 
def log_login_event(cursor, username: str, event: str, success: bool, ip):
    cursor.execute(
        """
        INSERT INTO login_logs (username, event, success, ip_address, timestamp)
        VALUES (?, ?, ?, ?, ?)
        """,
        (username[:100], event, 1 if success else 0, ip, utc_now().isoformat())
    )
 
 
# --- User Registration Endpoint ---
@app.post("/api/register")
async def register_user(payload: RegisterSchema):
    username = payload.username.strip()
    email = payload.email.strip().lower()
    password = payload.password
 
    # Validate username
    if not username:
        raise HTTPException(status_code=400, detail="Username required")
 
    if not re.fullmatch(r"[A-Za-z0-9_]{3,30}", username):
        raise HTTPException(
            status_code=400,
            detail="Username must be 3-30 characters using letters, numbers, or underscores."
        )
 
    # Validate email (rejects SQL injection strings like "' OR 1=1 --")
    if len(email) > 254 or not re.fullmatch(
        r"[A-Za-z0-9._%+\-]+@[A-Za-z0-9\-]+(\.[A-Za-z0-9\-]+)*\.[A-Za-z]{2,}", email
    ):
        raise HTTPException(status_code=400, detail="Invalid email format")
 
    # Validate password length, then complexity policy
    if len(password) < 8:
        raise HTTPException(status_code=400, detail="Password too short")
 
    if len(password) > 128:
        raise HTTPException(
            status_code=400,
            detail="Password must be at most 128 characters."
        )
 
    if not (re.search(r"[a-z]", password)
            and re.search(r"[A-Z]", password)
            and re.search(r"\d", password)):
        raise HTTPException(
            status_code=400,
            detail="Password must include an uppercase letter, a lowercase letter and a number."
        )
 
    conn = get_db()
 
    try:
        cursor = conn.cursor()
 
        # Check username (parameterized query - no SQL injection)
        cursor.execute(
            "SELECT 1 FROM users WHERE username = ? COLLATE NOCASE", (username,)
        )
        if cursor.fetchone():
            raise HTTPException(status_code=409, detail="Username already taken")
 
        # Check email
        cursor.execute("SELECT 1 FROM users WHERE email = ?", (email,))
        if cursor.fetchone():
            raise HTTPException(status_code=409, detail="Email already registered")
 
        # Store the account with a hashed password
        try:
            cursor.execute(
                "INSERT INTO users (username, email, password) VALUES (?, ?, ?)",
                (username, email, hash_password(password))
            )
            conn.commit()
        except sqlite3.IntegrityError:
            raise HTTPException(
                status_code=409, detail="Username already taken"
            )
 
        return {
            "success": True,
            "message": "Registration successful. You can now log in."
        }
 
    finally:
        conn.close()
 
        
# --- User Login Endpoint ---
@app.post("/api/login")
async def login_user(payload: LoginSchema, request: Request):
    username = payload.username.strip()
    password = payload.password
    ip = request.client.host if request.client else None
 
    if not username:
        raise HTTPException(status_code=400, detail="Username required")
 
    if not password:
        raise HTTPException(status_code=400, detail="Password required")
 
    conn = get_db()
    try:
        cursor = conn.cursor()
 
        cursor.execute(
            "SELECT username, password FROM users WHERE username = ?",
            (username,)
        )
        user = cursor.fetchone()
 
        if not user:
            log_login_event(cursor, username, "login_failed_unknown_user", False, ip)
            conn.commit()
            raise HTTPException(status_code=404, detail="User not found")
 
        now = utc_now()
        cursor.execute(
            "SELECT failed_attempts, locked_until FROM login_security WHERE username = ?",
            (username,)
        )
        security = cursor.fetchone()
        failed_attempts = security["failed_attempts"] if security else 0
 
        # Reject while the account is locked
        if security and security["locked_until"]:
            locked_until = datetime.fromisoformat(security["locked_until"])
            if locked_until > now:
                minutes = max(1, math.ceil((locked_until - now).total_seconds() / 60))
                log_login_event(cursor, username, "login_blocked_locked", False, ip)
                conn.commit()
                raise HTTPException(
                    status_code=423,
                    detail=f"Account locked due to too many failed attempts. "
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
                raise HTTPException(
                    status_code=423,
                    detail=f"Account locked due to too many failed attempts. "
                           f"Try again in {LOCKOUT_MINUTES} minute(s)."
                )
 
            cursor.execute(
                "INSERT OR REPLACE INTO login_security (username, failed_attempts, locked_until) VALUES (?, ?, NULL)",
                (username, failed_attempts)
            )
            log_login_event(cursor, username, "login_failed", False, ip)
            conn.commit()
            raise HTTPException(status_code=401, detail="Invalid credentials")
 
        # Success: clear failure counter
        cursor.execute(
            "INSERT OR REPLACE INTO login_security (username, failed_attempts, locked_until) VALUES (?, 0, NULL)",
            (username,)
        )
 
        # Upgrade an old plain-text test password to a secure hash.
        if not user["password"].startswith("pbkdf2_sha256$"):
            cursor.execute(
                "UPDATE users SET password = ? WHERE username = ?",
                (hash_password(password), username)
            )
 
        log_login_event(cursor, username, "login_success", True, ip)
        conn.commit()
 
        return {
            "success": True,
            "message": "Login successful.",
            "username": username,
            "token": create_token(username),
            "expires_in": SESSION_TIMEOUT_MINUTES * 60
        }
 
    finally:
        conn.close()
 
 
# --- Password Recovery (FR-04) ---
@app.post("/api/password/forgot")
async def forgot_password(payload: ForgotPasswordSchema, request: Request):
    ip = request.client.host if request.client else None
    try:
        # Same answer whether or not the email has an account.
        await run_in_threadpool(request_password_reset, payload.email, ip)
    except AuthError as error:
        raise HTTPException(status_code=error.status_code, detail=error.detail)
    return {"success": True, "message": RESET_REQUEST_MESSAGE}


@app.post("/api/password/verify")
async def verify_reset_link(payload: VerifyResetSchema):
    """Lets the reset page reject an expired or used link before asking for a password."""
    return {"valid": check_reset_token(payload.token)}


@app.post("/api/password/reset")
async def reset_user_password(payload: ResetPasswordSchema, request: Request):
    ip = request.client.host if request.client else None
    try:
        await run_in_threadpool(reset_password, payload.token, payload.new_password, ip)
    except AuthError as error:
        raise HTTPException(status_code=error.status_code, detail=error.detail)
    return {
        "success": True,
        "message": "Password reset successful. You can now log in with your new password."
    }


# --- Session refresh (sliding idle timeout) and logout ---
@app.get("/api/session")
async def refresh_session(request: Request):
    # The middleware already rejected missing/expired/invalid tokens.
    claims = validate_session(get_bearer_token(request))
    return {
        "success": True,
        "username": claims["sub"],
        # same sid: the refreshed token belongs to the same login session
        "token": create_token(claims["sub"], sid=claims.get("sid")),
        "expires_in": SESSION_TIMEOUT_MINUTES * 60
    }
 
 
@app.post("/api/logout")
async def logout_user(request: Request):
    token = get_bearer_token(request)
    claims = validate_session(token)
    revoke_session(token)   # a saved copy of this token can no longer restore the session
    conn = get_db()
    try:
        cursor = conn.cursor()
        log_login_event(cursor, claims["sub"], "logout", True,
                        request.client.host if request.client else None)
        conn.commit()
    finally:
        conn.close()
    return {"success": True, "message": "Logged out."}
 
 
# --- Active WebSocket Registry (Issue #19, #46) ---
class ConnectionManager:
    def __init__(self):
        self.active_connections: Dict[str, WebSocket] = {}
        self.visible: Dict[str, bool] = {}

    async def connect(self, username: str, websocket: WebSocket):
        await websocket.accept()
        previous = self.active_connections.get(username)
        self.active_connections[username] = websocket
        self.visible[username] = True
        if previous is not None and previous is not websocket:
            try:
                await previous.close(code=4001, reason="Connection replaced")
            except Exception:
                logger.warning("Failed to close replaced WebSocket: username=%s", username, exc_info=True)

    def disconnect(self, username: str, websocket: Optional[WebSocket] = None):
        current = self.active_connections.get(username)
        if websocket is not None and current is not websocket:
            return
        self.active_connections.pop(username, None)
        self.visible.pop(username, None)


manager = ConnectionManager()


def persist_websocket_message(
    msg_id: str,
    sender: str,
    recipient: str,
    content: str,
    timestamp: str,
):
    """Persist a message once; identical retries are safe and idempotent."""
    conn = None
    try:
        conn = get_db()
        conn.execute("BEGIN IMMEDIATE")
        cursor = conn.execute(
            """
            INSERT OR IGNORE INTO messages
                (msg_id, sender, recipient, content, timestamp, status)
            VALUES (?, ?, ?, ?, ?, 'sent')
            """,
            (msg_id, sender, recipient, content, timestamp),
        )
        inserted = cursor.rowcount == 1

        if not inserted:
            existing = conn.execute(
                """
                SELECT sender, recipient, content
                FROM messages
                WHERE msg_id = ?
                """,
                (msg_id,),
            ).fetchone()
            if existing is None or (
                existing["sender"] != sender
                or existing["recipient"] != recipient
                or existing["content"] != content
            ):
                raise ValueError("Message ID conflicts with an existing message")

        conn.commit()
        return inserted
    except Exception:
        if conn is not None:
            try:
                conn.rollback()
            except sqlite3.Error:
                logger.exception("Failed to roll back message transaction")
        logger.exception("Failed to persist WebSocket message")
        raise
    finally:
        if conn is not None:
            try:
                conn.close()
            except sqlite3.Error:
                logger.exception("Failed to close message database connection")

@app.websocket("/ws/{username}")
async def websocket_endpoint(websocket: WebSocket, username: str):
    try:
        claims = validate_session(websocket.query_params.get("token", ""))
    except TokenError:
        logger.warning("Rejected WebSocket connection: invalid session")
        await websocket.close(code=4401)
        return

    if claims.get("sub") != username:
        logger.warning("Rejected WebSocket connection: username mismatch")
        await websocket.close(code=4403)
        return

    try:
        await manager.connect(username, websocket)
    except Exception:
        logger.exception("Failed to establish WebSocket: username=%s", username)
        try:
            await websocket.close(code=1011, reason="Connection setup failed")
        except Exception:
            logger.debug("WebSocket setup cleanup failed", exc_info=True)
        return

    try:
        while True:
            try:
                data = await websocket.receive_json()
            except WebSocketDisconnect:
                raise
            except Exception:
                logger.warning("Invalid WebSocket payload: username=%s", username, exc_info=True)
                try:
                    await websocket.send_json({"type": "error", "message": "Invalid message format."})
                except Exception:
                    logger.debug("Could not report invalid payload: username=%s", username, exc_info=True)
                    break
                continue

            if not isinstance(data, dict):
                await websocket.send_json({"type": "error", "message": "Message must be a JSON object."})
                continue

            message_type = data.get("type")

            if message_type == "send_message":
                recipient = data.get("recipient")
                content = data.get("content")
                msg_id = data.get("msg_id") or os.urandom(16).hex()
                timestamp = data.get("timestamp")

                if (
                    not isinstance(recipient, str) or not recipient.strip()
                    or not isinstance(content, str) or not content.strip()
                    or not isinstance(msg_id, str) or not msg_id.strip()
                    or not isinstance(timestamp, str) or not timestamp.strip()
                ):
                    await websocket.send_json({"type": "error", "message": "Invalid message fields."})
                    continue

                recipient = recipient.strip()
                msg_id = msg_id.strip()
                try:
                    block_active = users_are_blocked(username, recipient)
                except sqlite3.Error:
                    logger.exception("Could not verify block state: sender=%s recipient=%s", username, recipient)
                    await websocket.send_json({
                        "type": "error",
                        "msg_id": msg_id,
                        "message": "Message was not sent because block settings could not be verified. Please retry.",
                    })
                    continue
                if block_active:
                    await websocket.send_json({
                        "type": "error",
                        "msg_id": msg_id,
                        "message": "Message not sent: a block is active between you and this user.",
                    })
                    continue
                try:
                    inserted = persist_websocket_message(
                        msg_id, username, recipient, content, timestamp
                    )
                except ValueError:
                    await websocket.send_json({
                        "type": "error",
                        "msg_id": msg_id,
                        "message": "Message ID conflicts with an existing message.",
                    })
                    continue
                except sqlite3.Error:
                    await websocket.send_json({
                        "type": "error",
                        "msg_id": msg_id,
                        "message": "Message could not be saved. Please retry.",
                    })
                    continue
                except Exception:
                    logger.exception(
                        "Unexpected message-processing failure: username=%s",
                        username,
                    )
                    await websocket.send_json({
                        "type": "error",
                        "msg_id": msg_id,
                        "message": "Message processing failed. Please retry.",
                    })
                    continue

                # A sent acknowledgment confirms durable server persistence, not recipient delivery.
                await websocket.send_json({
                    "type": "status_update",
                    "msg_id": msg_id,
                    "status": "sent",
                })

                # A duplicate retry was already persisted and must not be forwarded twice.
                if not inserted:
                    continue

                incoming_frame = {
                    "type": "receive_message", "msg_id": msg_id, "sender": username,
                    "recipient": recipient, "content": content, "timestamp": timestamp,
                }
                recipient_socket = manager.active_connections.get(recipient)
                if recipient_socket is not None:
                    try:
                        await recipient_socket.send_json(incoming_frame)
                        await websocket.send_json({"type": "status_update", "msg_id": msg_id, "status": "delivered"})
                    except Exception:
                        logger.exception("Live message delivery failed: recipient=%s", recipient)
                        manager.disconnect(recipient, recipient_socket)
                        try:
                            await websocket.send_json({"type": "status_update", "msg_id": msg_id, "status": "sent"})
                        except Exception:
                            logger.debug("Could not send message status: username=%s", username, exc_info=True)

                if recipient not in manager.active_connections or not manager.visible.get(recipient, True):
                    try:
                        schedule_push(msg_id, username, recipient, content)
                    except Exception:
                        logger.exception("Failed to schedule push notification: recipient=%s", recipient)

            elif message_type == "visibility":
                manager.visible[username] = bool(data.get("visible"))

            elif message_type == "read_ack":
                sender = data.get("sender")
                msg_id = data.get("msg_id")
                if not isinstance(sender, str) or not isinstance(msg_id, str):
                    await websocket.send_json({"type": "error", "message": "Invalid read receipt."})
                    continue
                sender_socket = manager.active_connections.get(sender)
                if sender_socket is not None:
                    try:
                        await sender_socket.send_json({"type": "status_update", "msg_id": msg_id, "status": "read"})
                    except Exception:
                        logger.exception("Failed to send read receipt: recipient=%s", sender)
                        manager.disconnect(sender, sender_socket)
            else:
                await websocket.send_json({"type": "error", "message": "Unsupported message type."})

    except WebSocketDisconnect:
        logger.info("WebSocket disconnected: username=%s", username)
    except Exception:
        logger.exception("Unexpected WebSocket failure: username=%s", username)
        try:
            await websocket.close(code=1011, reason="Unexpected server error")
        except Exception:
            logger.debug("Failed to close WebSocket after an error: username=%s", username, exc_info=True)
    finally:
        manager.disconnect(username, websocket)
        logger.info("WebSocket connection cleanup completed: username=%s", username)


# --- Profile Management (FR-05) ---
def current_username(request: Request) -> str:
    """The logged-in user, taken from the session token (never from the URL/body)."""
    return validate_session(get_bearer_token(request))["sub"]


def client_ip(request: Request):
    return request.client.host if request.client else None


@app.get("/api/gifs/config")
async def get_gif_config(request: Request):
    """Return client configuration after confirming the current session."""
    current_username(request)
    return {
        "provider": "giphy",
        "enabled": bool(GIPHY_API_KEY),
        # GIPHY's Search API is intended to be called by the client.
        "api_key": GIPHY_API_KEY or None,
    }


async def broadcast_profile(username: str, profile: dict):
    """Push the changed profile to the user's other open tabs and to every
    online contact, so the change is visible immediately."""
    targets = {username} | set(profile_service.contacts_of(username))
    frame = {"type": "profile_update", "profile": profile}
    for name in targets:
        socket_ = manager.active_connections.get(name)
        if socket_ is None:
            continue
        try:
            await socket_.send_json(frame)
        except Exception:
            logger.exception("Profile broadcast failed for an active connection")
            manager.disconnect(name)


@app.get("/api/profiles")
async def get_profiles_batch(users: str = ""):
    """Profiles for a comma-separated list of usernames (used for the contact list)."""
    return {"profiles": profile_service.get_profiles(users.split(","))}


@app.get("/api/profile/{username}")
async def get_user_profile(username: str):
    try:
        return {"success": True, "profile": profile_service.get_profile(username)}
    except AuthError as error:
        raise HTTPException(status_code=error.status_code, detail=error.detail)


@app.put("/api/profile")
async def update_my_profile(payload: ProfileUpdateSchema, request: Request):
    username = current_username(request)
    try:
        profile = await run_in_threadpool(
            profile_service.update_profile,
            username, payload.display_name, payload.bio, client_ip(request)
        )
    except AuthError as error:
        raise HTTPException(status_code=error.status_code, detail=error.detail)
    await broadcast_profile(username, profile)
    return {"success": True, "message": "Profile updated.", "profile": profile}


@app.post("/api/profile/avatar")
async def upload_my_avatar(request: Request, file: UploadFile = File(...)):
    username = current_username(request)
    # Read one byte past the limit so an oversized file is rejected without
    # loading all of it into memory.
    data = await file.read(profile_service.MAX_AVATAR_BYTES + 1)
    try:
        profile = await run_in_threadpool(
            profile_service.set_avatar, username, data, client_ip(request)
        )
    except AuthError as error:
        raise HTTPException(status_code=error.status_code, detail=error.detail)
    await broadcast_profile(username, profile)
    return {"success": True, "message": "Profile picture updated.", "profile": profile}


@app.delete("/api/profile/avatar")
async def delete_my_avatar(request: Request):
    username = current_username(request)
    try:
        profile = await run_in_threadpool(
            profile_service.remove_avatar, username, client_ip(request)
        )
    except AuthError as error:
        raise HTTPException(status_code=error.status_code, detail=error.detail)
    await broadcast_profile(username, profile)
    return {"success": True, "message": "Profile picture removed.", "profile": profile}


# --- Push Notifications & Mute (FR-19) ---
_background_tasks = set()


def schedule_push(msg_id, sender, recipient, content):
    """Send the push in the background so chatting is never slowed down."""
    async def run():
        try:
            await run_in_threadpool(
                push_service.notify_new_message, msg_id, sender, recipient, content
            )
        except Exception:
            logger.exception("Push notification task failed")

    task = asyncio.create_task(run())
    _background_tasks.add(task)
    task.add_done_callback(_background_tasks.discard)


@app.get("/sw.js")
async def service_worker():
    """The service worker must be served from the site root to control the whole app."""
    path = os.path.join(STATIC_DIR, "sw.js")
    if not os.path.isfile(path):
        raise HTTPException(status_code=404, detail="Service worker not found")
    with open(path, "r", encoding="utf-8") as f:
        body = f.read()
    return Response(
        content=body,
        media_type="application/javascript",
        headers={"Service-Worker-Allowed": "/", "Cache-Control": "no-cache"}
    )


@app.get("/api/push/public-key")
async def push_public_key():
    enabled = await run_in_threadpool(push_service.is_enabled)
    return {
        "enabled": enabled,
        "public_key": push_service.get_public_key() if enabled else None
    }


@app.post("/api/push/subscribe")
async def push_subscribe(payload: PushSubscribeSchema, request: Request):
    username = current_username(request)
    try:
        await run_in_threadpool(
            push_service.save_subscription, username, payload.endpoint, payload.keys,
            request.headers.get("user-agent"), client_ip(request)
        )
    except AuthError as error:
        raise HTTPException(status_code=error.status_code, detail=error.detail)
    return {"success": True, "message": "Notifications enabled on this device."}


@app.post("/api/push/unsubscribe")
async def push_unsubscribe(payload: PushUnsubscribeSchema, request: Request):
    username = current_username(request)
    try:
        await run_in_threadpool(
            push_service.remove_subscription, username, payload.endpoint, client_ip(request)
        )
    except AuthError as error:
        raise HTTPException(status_code=error.status_code, detail=error.detail)
    return {"success": True, "message": "Notifications disabled on this device."}


@app.post("/api/push/test")
async def push_test(request: Request):
    username = current_username(request)
    sent = await run_in_threadpool(push_service.send_test, username)
    return {"success": True, "sent": sent}


@app.get("/api/mute")
async def get_muted(request: Request):
    username = current_username(request)
    return {"muted": await run_in_threadpool(push_service.list_muted, username)}


@app.post("/api/mute")
async def set_muted(payload: MuteSchema, request: Request):
    username = current_username(request)
    try:
        muted = await run_in_threadpool(
            push_service.set_muted, username, payload.username, payload.muted
        )
    except AuthError as error:
        raise HTTPException(status_code=error.status_code, detail=error.detail)
    return {"success": True, "muted": muted}


def users_are_blocked(first_user: str, second_user: str) -> bool:
    """Return True if either user has blocked the other."""
    if not first_user or not second_user:
        return False
    conn = get_db()
    try:
        row = conn.execute(
            """
            SELECT 1 FROM user_blocks
            WHERE (blocker_username = ? AND blocked_username = ?)
               OR (blocker_username = ? AND blocked_username = ?)
            LIMIT 1
            """,
            (first_user, second_user, second_user, first_user),
        ).fetchone()
        return row is not None
    finally:
        conn.close()


def list_blocked_users(username: str) -> list[str]:
    conn = get_db()
    try:
        rows = conn.execute(
            "SELECT blocked_username FROM user_blocks WHERE blocker_username = ? ORDER BY blocked_username",
            (username,),
        ).fetchall()
        return [row["blocked_username"] for row in rows]
    finally:
        conn.close()


@app.get("/api/blocks")
async def get_blocked_users(request: Request):
    username = current_username(request)
    return {"blocked": list_blocked_users(username)}


@app.post("/api/blocks")
async def block_user(payload: BlockUserSchema, request: Request):
    username = current_username(request)
    target = payload.username.strip()
    if not re.fullmatch(r"[A-Za-z0-9_]{3,30}", target):
        raise HTTPException(status_code=400, detail="Enter a valid username.")
    if target.casefold() == username.casefold():
        raise HTTPException(status_code=400, detail="You cannot block your own account.")

    conn = get_db()
    try:
        exists = conn.execute(
            "SELECT username FROM users WHERE username = ? COLLATE NOCASE", (target,)
        ).fetchone()
        if exists is None:
            raise HTTPException(status_code=404, detail="User not found.")
        target = exists["username"]
        conn.execute(
            "INSERT OR IGNORE INTO user_blocks(blocker_username, blocked_username) VALUES (?, ?)",
            (username, target),
        )
        conn.commit()
    finally:
        conn.close()

    return {
        "success": True,
        "blocked": list_blocked_users(username),
        "message": f"@{target} is blocked. Neither of you can send messages to the other while this block is active.",
    }


@app.delete("/api/blocks/{target_username}")
async def unblock_user(target_username: str, request: Request):
    username = current_username(request)
    target = target_username.strip()
    conn = get_db()
    try:
        conn.execute(
            "DELETE FROM user_blocks WHERE blocker_username = ? AND blocked_username = ?",
            (username, target),
        )
        conn.commit()
    finally:
        conn.close()
    return {
        "success": True,
        "blocked": list_blocked_users(username),
        "message": f"@{target} has been unblocked.",
    }


@app.post("/api/reports")
async def report_user(payload: ReportUserSchema, request: Request):
    reporter = current_username(request)
    target = payload.username.strip()
    reason = payload.reason.strip()
    if not re.fullmatch(r"[A-Za-z0-9_]{3,30}", target):
        raise HTTPException(status_code=400, detail="Enter a valid username to report.")
    if target.casefold() == reporter.casefold():
        raise HTTPException(status_code=400, detail="You cannot report your own account.")
    if len(reason) < 5:
        raise HTTPException(status_code=400, detail="Please provide a report reason of at least 5 characters.")
    if len(reason) > 1000:
        raise HTTPException(status_code=400, detail="Report reason must be at most 1000 characters.")

    conn = get_db()
    try:
        exists = conn.execute(
            "SELECT username FROM users WHERE username = ? COLLATE NOCASE", (target,)
        ).fetchone()
        if exists is None:
            raise HTTPException(status_code=404, detail="User not found.")
        target = exists["username"]
        conn.execute(
            "INSERT INTO user_reports(reporter_username, reported_username, reason) VALUES (?, ?, ?)",
            (reporter, target, reason),
        )
        conn.commit()
    finally:
        conn.close()

    logger.info("User report submitted: reporter=%s reported=%s", reporter, target)
    return {"success": True, "message": "Report submitted. Thank you for helping keep the chat community safe."}


# --- Contact Management API Endpoints (ADDED FOR ISSUE #17) ---
# --- Complete Friend Request & Contact Lifecycle Endpoints (UPDATED FOR ISSUE #17) ---
class AddContactSchema(BaseModel):
    user_id: str
    contact_username: str
 
class RespondRequestSchema(BaseModel):
    user_id: str
    requester_id: str
    action: str  # 'accept' or 'decline'
 
class RemoveContactSchema(BaseModel):
    user_id: str
    contact_username: str
 
# --- Fetch All Users Endpoint (kept; now requires a valid session) ---
@app.get("/api/users")
async def get_all_users():
    conn = get_db()
    cursor = conn.cursor()
    cursor.execute("SELECT username FROM users")
    rows = cursor.fetchall()
    conn.close()
    return {"users": [row["username"] for row in rows]}
 
@app.get("/api/contacts/{username}")
async def get_contacts(username: str):
    conn = get_db()
    cursor = conn.cursor()
    cursor.execute("SELECT contact_id FROM contacts WHERE user_id = ? AND status = 'accepted'", (username,))
    rows = cursor.fetchall()
    conn.close()
    return {"contacts": [row["contact_id"] for row in rows]}
 
# Get Pending Incoming Friend Requests
@app.get("/api/contacts/pending/{username}")
async def get_pending_requests(username: str):
    conn = get_db()
    cursor = conn.cursor()
    cursor.execute("SELECT user_id FROM contacts WHERE contact_id = ? AND status = 'pending'", (username,))
    rows = cursor.fetchall()
    conn.close()
    return {"pending": [row["user_id"] for row in rows]}
 
# Send Friend Request
@app.post("/api/contacts/add")
async def send_friend_request(payload: AddContactSchema):
    conn = get_db()
    cursor = conn.cursor()
    
    if payload.user_id == payload.contact_username:
        conn.close()
        return {"success": False, "error": "You cannot add yourself as a contact"}
    if users_are_blocked(payload.user_id, payload.contact_username):
        conn.close()
        return {"success": False, "error": "A block is active between these users"}
 
    # Accounts now come from registration, so contacts must be real users
    # (previously unknown names were auto-created with a default password).
    cursor.execute("SELECT 1 FROM users WHERE username = ?", (payload.contact_username,))
    if not cursor.fetchone():
        conn.close()
        return {"success": False, "error": "User not found"}
    
    try:
        cursor.execute("INSERT INTO contacts (user_id, contact_id, status) VALUES (?, ?, 'pending')", 
                       (payload.user_id, payload.contact_username))
        conn.commit()
        conn.close()
        return {"success": True, "message": f"Friend request sent to '{payload.contact_username}'"}
    except Exception:
        conn.close()
        return {"success": False, "error": "Friend request or contact already exists"}
 
# Accept or Decline Friend Request
@app.post("/api/contacts/respond")
async def respond_friend_request(payload: RespondRequestSchema):
    if payload.action == 'accept' and users_are_blocked(payload.user_id, payload.requester_id):
        return {"success": False, "error": "Cannot accept this request while a block is active between these users"}
    conn = get_db()
    cursor = conn.cursor()
    
    if payload.action == 'accept':
        cursor.execute("UPDATE contacts SET status = 'accepted' WHERE user_id = ? AND contact_id = ?", 
                       (payload.requester_id, payload.user_id))
        cursor.execute("INSERT OR REPLACE INTO contacts (user_id, contact_id, status) VALUES (?, ?, 'accepted')", 
                       (payload.user_id, payload.requester_id))
        conn.commit()
        conn.close()
        return {"success": True, "message": f"Accepted friend request from '{payload.requester_id}'"}
    else:
        cursor.execute("DELETE FROM contacts WHERE user_id = ? AND contact_id = ?", 
                       (payload.requester_id, payload.user_id))
        conn.commit()
        conn.close()
        return {"success": True, "message": f"Declined request from '{payload.requester_id}'"}
 
# Remove Contact
@app.post("/api/contacts/remove")
async def remove_contact(payload: RemoveContactSchema):
    conn = get_db()
    cursor = conn.cursor()
    cursor.execute("DELETE FROM contacts WHERE (user_id = ? AND contact_id = ?) OR (user_id = ? AND contact_id = ?)", 
                   (payload.user_id, payload.contact_username, payload.contact_username, payload.user_id))
    conn.commit()
    conn.close()
    return {"success": True, "message": f"Removed '{payload.contact_username}' from contacts"}
 
# --- Fetch Conversation History Endpoint (ADDED FOR ISSUE #23) ---
@app.get("/api/messages/history/{user1}/{user2}")
async def get_chat_history(user1: str, user2: str):
    conn = get_db()
    cursor = conn.cursor()
    cursor.execute("""
        SELECT msg_id, sender, recipient, content, timestamp, status 
        FROM messages 
        WHERE (sender = ? AND recipient = ?) OR (sender = ? AND recipient = ?)
        ORDER BY id ASC
    """, (user1, user2, user2, user1))
    rows = cursor.fetchall()
    conn.close()
    return {"history": [dict(row) for row in rows]}
 
# --- File & Image Attachment Multipart Upload Endpoint (ADDED FOR ISSUE #29) ---
@app.post("/api/upload")
async def upload_file(file: UploadFile = File(...)):
    os.makedirs(UPLOADS_DIR, exist_ok=True)
        
    file_location = os.path.join(UPLOADS_DIR, file.filename)
    with open(file_location, "wb") as buffer:
        shutil.copyfileobj(file.file, buffer)
    
    file_url = f"/uploads/{file.filename}"
    return {
        "success": True, 
        "file_url": file_url, 
        "filename": file.filename,
        "content_type": file.content_type
    }
 
# --- Voice message upload endpoint (Issue #38) ---
VOICE_MESSAGE_UPLOAD_MARKER = "issue38-voice-message-upload"
MAX_VOICE_MESSAGE_BYTES = 5 * 1024 * 1024
VOICE_MESSAGE_MIME_TYPES = {
    "audio/webm": ".webm",
    "audio/ogg": ".ogg",
    "audio/mp4": ".m4a",
    "audio/mpeg": ".mp3",
    "audio/wav": ".wav",
    "audio/x-wav": ".wav",
}


@app.post("/api/voice-messages/upload")
async def upload_voice_message(file: UploadFile = File(...)):
    """Save a short voice recording using a server-generated filename."""
    content_type = (file.content_type or "").split(";", 1)[0].strip().lower()
    extension = VOICE_MESSAGE_MIME_TYPES.get(content_type)
    if extension is None:
        await file.close()
        raise HTTPException(status_code=415, detail="Unsupported voice recording format.")

    try:
        data = await file.read(MAX_VOICE_MESSAGE_BYTES + 1)
        if not data:
            raise HTTPException(status_code=400, detail="Voice recording is empty.")
        if len(data) > MAX_VOICE_MESSAGE_BYTES:
            raise HTTPException(status_code=413, detail="Voice recording must be 5 MB or smaller.")

        os.makedirs(UPLOADS_DIR, exist_ok=True)
        filename = f"voice-{uuid.uuid4().hex}{extension}"
        file_location = os.path.join(UPLOADS_DIR, filename)
        with open(file_location, "xb") as output:
            output.write(data)

        return {
            "success": True,
            "file_url": f"/uploads/{filename}",
            "filename": "Voice message",
            "content_type": content_type,
            "size": len(data),
        }
    finally:
        await file.close()


# --- Full-Text Search API Endpoint (ADDED FOR ISSUE #31) ---
@app.get("/api/search")
async def search_query(username: str, q: str):
    if not q or len(q.strip()) == 0:
        return {"contacts": [], "messages": []}
 
    search_term = f"%{q.strip()}%"
    conn = get_db()
    cursor = conn.cursor()
 
    # 1. Search Contacts matching search term
    cursor.execute("""
        SELECT contact_id FROM contacts 
        WHERE user_id = ? AND status = 'accepted' AND contact_id LIKE ?
    """, (username, search_term))
    contacts = [row["contact_id"] for row in cursor.fetchall()]
 
    # 2. Search Messages matching content search term
    cursor.execute("""
        SELECT msg_id, sender, recipient, content, timestamp 
        FROM messages 
        WHERE (sender = ? OR recipient = ?) AND content LIKE ?
        ORDER BY id DESC
    """, (username, username, search_term))
    messages = [dict(row) for row in cursor.fetchall()]
 
    conn.close()
    return {"contacts": contacts, "messages": messages}
