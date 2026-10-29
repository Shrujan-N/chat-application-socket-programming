from fastapi import FastAPI, WebSocket, WebSocketDisconnect, UploadFile, File, HTTPException, Request
from fastapi.staticfiles import StaticFiles
from fastapi.responses import HTMLResponse, JSONResponse, Response
import asyncio
import logging
import json
from message_integrity import content_sha256, verify_content_sha256
from web_user_mentions import extract_group_mentions, notification_id_for
import performance as performance_metrics
from availability import check_database_connection, get_availability_summary
import scalability as scalability_engine
import time
import os
import re
import math
import shutil
import sqlite3
from collections import deque
from logging.handlers import RotatingFileHandler
from datetime import datetime, timedelta, timezone
from pydantic import BaseModel, Field
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
import e2e_service
 
 
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


class GroupCreateSchema(BaseModel):
    name: str = ""
    members: list[str] = Field(default_factory=list)
    icon: str = "👥"


class E2EPublicKeySchema(BaseModel):
    public_key: str = ""


class ReactionSchema(BaseModel):
    emoji: str = ""


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
from reactions_service import (
    STANDARD_EMOJIS,
    get_reactions_for_message,
    get_reactions_for_messages,
    toggle_message_reaction,
)
import call_service

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
 
 
# --- Availability / Uptime monitoring (NFR-03) ---
@app.get("/health/live")
async def liveness_probe():
    """Return success while the web process is able to handle requests."""
    return {"status": "alive"}


@app.get("/health/ready")
async def readiness_probe():
    """Check database dependencies needed by the chat application."""
    checks = {
        "core_database": check_database_connection(core_get_db),
        "web_database": check_database_connection(get_db),
    }
    ready = all(checks.values())
    payload = {
        "status": "ready" if ready else "not_ready",
        "checks": {
            name: ("ok" if available else "failed")
            for name, available in checks.items()
        },
    }
    if not ready:
        logger.error("Readiness probe failed: checks=%s", payload["checks"])
    return JSONResponse(status_code=200 if ready else 503, content=payload)


@app.get("/api/availability")
async def availability_overview():
    """Return authenticated rolling SLO measurements and existing request metrics."""
    summary = get_availability_summary(server_started_at=SERVER_STARTED_AT)
    total = REQUEST_METRICS["total_requests"]
    summary["request_metrics"] = {
        "total_requests": total,
        "server_errors": REQUEST_METRICS["server_errors"],
        "server_error_rate_percent": round(
            REQUEST_METRICS["server_errors"] / total * 100.0, 3
        ) if total else 0.0,
        "slow_requests": REQUEST_METRICS["slow_requests"],
        "average_response_ms": round(
            REQUEST_METRICS["total_duration_ms"] / total, 2
        ) if total else 0.0,
    }
    return summary


# --- Performance / Latency measurements (NFR-01) ---
@app.get("/api/performance/latency")
async def performance_latency_summary():
    # Auth is enforced by the existing /api middleware.
    performance_metrics.expire_pending_deliveries()
    return performance_metrics.get_latency_summary()


# --- Scalability (Issue #42) ---
@app.get("/api/scalability")
async def scalability_summary():
    # Auth is enforced by the existing /api middleware.
    return scalability_engine.get_scalability_summary(
        registry=manager.registry,
        lock_pool=recipient_lock_pool,
        broker=manager.broker,
    )


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
 
 
# --- Active WebSocket Registry (Issue #19, #46; Multi-device FR-26; Scalability #42) ---
class ConnectionManager:
    """Sharded multi-device WebSocket gateway manager with Pub/Sub fan-out."""

    def __init__(
        self,
        *,
        shard_count: int = scalability_engine.REGISTRY_SHARD_COUNT,
        max_connections: int = scalability_engine.MAX_CONCURRENT_CONNECTIONS,
        broker: Optional[scalability_engine.InMemoryPubSubBroker] = None,
        node_id: Optional[str] = None,
    ):
        # Kept for older callers/tests that expect one representative socket per user.
        self.active_connections: Dict[str, WebSocket] = {}
        self.device_connections: Dict[str, list[WebSocket]] = {}
        self.device_visibility: Dict[str, Dict[int, bool]] = {}
        # Aggregate visibility means any active device is foreground/visible.
        self.visible: Dict[str, bool] = {}
        self.registry = scalability_engine.ShardedConnectionRegistry(
            shard_count=shard_count,
            max_connections=max_connections,
        )
        self.node_id = node_id or os.environ.get("CHAT_GATEWAY_NODE_ID", "gateway-primary")
        self.broker = broker or scalability_engine.InMemoryPubSubBroker()
        self.broker.subscribe_node(self.node_id, self._deliver_local_user)

    async def connect(self, username: str, websocket: WebSocket):
        existing_devices = self.device_connections.get(username, [])
        already_registered = any(existing is websocket for existing in existing_devices)
        if not already_registered and not self.registry.can_accept():
            self.registry.rejected_over_capacity += 1
            raise scalability_engine.CapacityExceededError(
                f"Max concurrent connections ({self.registry.max_connections}) reached"
            )

        await websocket.accept()
        devices = self.device_connections.setdefault(username, [])
        if all(existing is not websocket for existing in devices):
            devices.append(websocket)
        self.registry.register(username, websocket, enforce_capacity=False)
        self.device_visibility.setdefault(username, {})[id(websocket)] = True
        self.active_connections[username] = websocket
        self._refresh_visibility(username)

    def connections_for(self, username: str) -> list[WebSocket]:
        """Return connected device sockets, including legacy direct-map entries."""
        devices = list(self.device_connections.get(username, []))
        primary = self.active_connections.get(username)
        if primary is not None and all(existing is not primary for existing in devices):
            devices.append(primary)
        return devices

    def _refresh_visibility(self, username: str) -> bool:
        devices = self.connections_for(username)
        if not devices:
            self.visible.pop(username, None)
            return False
        per_device = self.device_visibility.get(username, {})
        value = any(per_device.get(id(device), True) for device in devices)
        self.visible[username] = value
        return value

    def set_visibility(self, username: str, websocket: WebSocket, visible: bool) -> None:
        if all(existing is not websocket for existing in self.connections_for(username)):
            return
        self.device_visibility.setdefault(username, {})[id(websocket)] = bool(visible)
        self._refresh_visibility(username)

    async def _deliver_local_user(self, username: str, message: dict, exclude=None) -> int:
        """Deliver concurrently to local sockets for `username` with timeout isolation."""
        targets = [
            device for device in list(self.connections_for(username))
            if device is not exclude
        ]
        if not targets:
            return 0

        if len(targets) == 1:
            device = targets[0]
            try:
                await scalability_engine.send_json_with_timeout(device, message)
                self.registry.record_routed_frames(1)
                return 1
            except Exception:
                logger.exception("WebSocket broadcast failed: username=%s", username)
                self.registry.record_broadcast_failure(1)
                self.disconnect(username, device)
                return 0

        outcomes = await asyncio.gather(
            *(scalability_engine.send_json_with_timeout(device, message) for device in targets),
            return_exceptions=True,
        )
        delivered = 0
        for device, outcome in zip(targets, outcomes):
            if isinstance(outcome, Exception):
                logger.error(
                    "WebSocket broadcast failed: username=%s error=%r",
                    username,
                    outcome,
                )
                self.registry.record_broadcast_failure(1)
                self.disconnect(username, device)
            else:
                delivered += 1
        self.registry.record_routed_frames(delivered)
        return delivered

    async def broadcast_user(self, username: str, message: dict, *, exclude=None) -> int:
        """Send a frame to all live device sockets across subscribed gateway workers."""
        return await self.broker.publish_user(username, message, exclude=exclude)

    async def broadcast_many_users(
        self,
        usernames,
        message: dict,
        *,
        exclude_username: Optional[str] = None,
    ) -> int:
        """Broadcast concurrently in bounded batches so slow sockets never stall fan-out."""
        return await self.broker.publish_many_users(
            usernames, message, exclude_username=exclude_username
        )

    def disconnect(self, username: str, websocket: Optional[WebSocket] = None):
        # With no socket, keep the previous API behaviour and clear the user's registry.
        if websocket is None:
            self.registry.unregister(username, None)
            self.active_connections.pop(username, None)
            self.device_connections.pop(username, None)
            self.device_visibility.pop(username, None)
            self.visible.pop(username, None)
            return

        self.registry.unregister(username, websocket)
        devices = [
            device for device in self.device_connections.get(username, [])
            if device is not websocket
        ]
        visibility = self.device_visibility.get(username, {})
        visibility.pop(id(websocket), None)
        if devices:
            self.device_connections[username] = devices
        else:
            self.device_connections.pop(username, None)

        if self.active_connections.get(username) is websocket:
            if devices:
                self.active_connections[username] = devices[-1]
            else:
                self.active_connections.pop(username, None)
        elif username not in self.active_connections and devices:
            self.active_connections[username] = devices[-1]

        if not self.device_connections.get(username):
            self.device_visibility.pop(username, None)
        self._refresh_visibility(username)


manager = ConnectionManager()


# DATA_INTEGRITY_ISSUE_45 + SCALABILITY_ISSUE_42:
# Bounded striped lock pool preserves strict per-recipient message ordering while
# capping lock memory to a fixed stripe count even at 100,000+ concurrent users.
recipient_lock_pool = scalability_engine.StripedLockPool()
recipient_delivery_locks = recipient_lock_pool


def get_recipient_delivery_lock(recipient: str) -> asyncio.Lock:
    return recipient_lock_pool.get(recipient)


def assert_message_integrity(rows) -> None:
    """Reject damaged stored messages rather than silently returning bad content."""
    for row in rows:
        if not verify_content_sha256(row["content"], row["integrity_hash"]):
            msg_id = row["msg_id"] if "msg_id" in row.keys() else "unknown"
            logger.error("DATA_INTEGRITY_ISSUE_45: checksum mismatch for message %s", msg_id)
            raise HTTPException(
                status_code=500,
                detail="A stored message failed integrity verification. Please contact support.",
            )



def get_message_reply_preview(reply_to_msg_id: Optional[str], sender: str, recipient: str):
    """Return a safe, integrity-checked preview only for a message in this conversation."""
    if not reply_to_msg_id:
        return None
    conn = None
    try:
        conn = get_db()
        row = conn.execute(
            """SELECT msg_id, sender, recipient, content, timestamp, status, integrity_hash
               FROM messages
               WHERE msg_id = ? AND (
                   (sender = ? AND recipient = ?) OR
                   (sender = ? AND recipient = ?)
               )""",
            (reply_to_msg_id, sender, recipient, recipient, sender),
        ).fetchone()
    finally:
        if conn is not None:
            conn.close()
    if row is None:
        return None
    assert_message_integrity([row])
    return {
        "msg_id": row["msg_id"],
        "sender": row["sender"],
        "content": row["content"],
        "timestamp": row["timestamp"],
    }


def persist_websocket_message(
    msg_id: str,
    sender: str,
    recipient: str,
    content: str,
    timestamp: str,
    integrity_hash: Optional[str] = None,
    reply_to_msg_id: Optional[str] = None,
):
    """Persist a message once; identical retries are safe and idempotent."""
    expected_hash = content_sha256(content)
    if integrity_hash is not None and not verify_content_sha256(content, integrity_hash):
        raise ValueError("Message content checksum mismatch")
    integrity_hash = expected_hash
    conn = None
    try:
        conn = get_db()
        conn.execute("BEGIN IMMEDIATE")
        cursor = conn.execute(
            """
            INSERT OR IGNORE INTO messages
                (msg_id, sender, recipient, content, timestamp, status, integrity_hash, reply_to_msg_id)
            VALUES (?, ?, ?, ?, ?, 'sent', ?, ?)
            """,
            (msg_id, sender, recipient, content, timestamp, integrity_hash, reply_to_msg_id),
        )
        inserted = cursor.rowcount == 1

        if not inserted:
            existing = conn.execute(
                """
                SELECT sender, recipient, content, timestamp, integrity_hash, reply_to_msg_id
                FROM messages
                WHERE msg_id = ?
                """,
                (msg_id,),
            ).fetchone()
            if existing is None or (
                existing["sender"] != sender
                or existing["recipient"] != recipient
                or existing["content"] != content
                or existing["timestamp"] != timestamp
                or existing["reply_to_msg_id"] != reply_to_msg_id
                or not verify_content_sha256(existing["content"], existing["integrity_hash"])
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

def persist_websocket_group_message(
    msg_id: str,
    group_id: str,
    sender: str,
    content: str,
    timestamp: str,
    integrity_hash: Optional[str] = None,
    mentioned_users: Optional[list[str]] = None,
    group_name: str = "",
):
    """Persist a group message and its mention alerts transactionally."""
    expected_hash = content_sha256(content)
    if integrity_hash is not None and not verify_content_sha256(content, integrity_hash):
        raise ValueError("Message content checksum mismatch")

    mentioned_users = list(dict.fromkeys(
        user.strip() for user in (mentioned_users or [])
        if isinstance(user, str) and user.strip()
    ))
    mentions_json = json.dumps(mentioned_users, ensure_ascii=False, separators=(",", ":"))
    group_name = (group_name or group_id).strip() or group_id
    conn = None
    try:
        conn = get_db()
        conn.execute("BEGIN IMMEDIATE")
        cursor = conn.execute(
            """
            INSERT OR IGNORE INTO messages
                (msg_id, conversation_type, conversation_id, sender, recipient,
                 group_id, content, timestamp, status, integrity_hash, mentioned_users)
            VALUES (?, 'group', ?, ?, '', ?, ?, ?, 'sent', ?, ?)
            """,
            (msg_id, f"group:{group_id}", sender, group_id, content, timestamp, expected_hash, mentions_json),
        )
        inserted = cursor.rowcount == 1

        if not inserted:
            existing = conn.execute(
                """
                SELECT sender, conversation_type, group_id, content, timestamp, integrity_hash, mentioned_users
                FROM messages WHERE msg_id = ?
                """,
                (msg_id,),
            ).fetchone()
            if existing is None or (
                existing["sender"] != sender
                or existing["conversation_type"] != "group"
                or existing["group_id"] != group_id
                or existing["content"] != content
                or existing["timestamp"] != timestamp
                or (existing["mentioned_users"] or "[]") != mentions_json
                or not verify_content_sha256(existing["content"], existing["integrity_hash"])
            ):
                raise ValueError("Message ID conflicts with an existing message")

        if inserted:
            mention_preview = (
                e2e_service.ENCRYPTED_MENTION_PREVIEW
                if e2e_service.is_encrypted_content(content)
                else content.replace("\n", " ").strip()[:180]
            )
            for mentioned_user in mentioned_users:
                conn.execute(
                    """INSERT OR IGNORE INTO mention_notifications
                       (notification_id, username, msg_id, group_id, group_name, sender, content_preview)
                       VALUES (?, ?, ?, ?, ?, ?, ?)""",
                    (
                        notification_id_for(msg_id, mentioned_user), mentioned_user, msg_id,
                        group_id, group_name, sender, mention_preview,
                    ),
                )
        conn.commit()
        return inserted
    except Exception:
        if conn is not None:
            try:
                conn.rollback()
            except sqlite3.Error:
                logger.exception("Failed to roll back group message transaction")
        raise
    finally:
        if conn is not None:
            conn.close()


def get_group_for_delivery(group_id: str):
    """Return a group record and its members, or (None, []) when it is missing."""
    conn = None
    try:
        conn = get_db()
        group = conn.execute(
            "SELECT group_id, name, icon, creator FROM groups WHERE group_id = ?",
            (group_id,),
        ).fetchone()
        if group is None:
            return None, []
        members = conn.execute(
            "SELECT username FROM group_members WHERE group_id = ? ORDER BY joined_at, username",
            (group_id,),
        ).fetchall()
        return dict(group), [row["username"] for row in members]
    finally:
        if conn is not None:
            conn.close()


# --- Reconnection handling (FR-21) helpers ---
def get_message_row_id(msg_id: str):
    """Database row id of a stored message. Clients use it as a cursor so that,
    after a reconnect, they can ask for everything they missed."""
    conn = None
    try:
        conn = get_db()
        row = conn.execute("SELECT rowid AS id FROM messages WHERE msg_id = ?", (msg_id,)).fetchone()
        return row["id"] if row else None
    except sqlite3.Error:
        logger.exception("Could not look up message row id")
        return None
    finally:
        if conn is not None:
            conn.close()


def mark_messages_delivered(msg_ids):
    """Persist 'delivered' so history and missed-message sync show the right status."""
    ids = [m for m in msg_ids if m]
    if not ids:
        return
    conn = None
    try:
        conn = get_db()
        conn.executemany(
            "UPDATE messages SET status = 'delivered' WHERE msg_id = ? AND status = 'sent'",
            [(m,) for m in ids],
        )
        conn.commit()
    except sqlite3.Error:
        logger.exception("Could not mark messages as delivered")
    finally:
        if conn is not None:
            conn.close()


def mark_message_read(msg_id: str, sender: str, reader: str) -> bool:
    """Persist a read receipt only when the connected reader owns the recipient side."""
    conn = None
    try:
        conn = get_db()
        row = conn.execute(
            "SELECT sender, recipient FROM messages WHERE msg_id = ?", (msg_id,)
        ).fetchone()
        if row is None or row["sender"] != sender or row["recipient"] != reader:
            return False
        conn.execute("UPDATE messages SET status = 'read' WHERE msg_id = ?", (msg_id,))
        conn.execute(
            """INSERT INTO message_receipts (msg_id, username, read_at)
               VALUES (?, ?, CURRENT_TIMESTAMP)
               ON CONFLICT(msg_id, username) DO UPDATE SET read_at = excluded.read_at""",
            (msg_id, reader),
        )
        conn.commit()
        return True
    except sqlite3.Error:
        logger.exception("Could not persist read receipt: msg_id=%s", msg_id)
        return False
    finally:
        if conn is not None:
            conn.close()


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
    except scalability_engine.CapacityExceededError:
        logger.warning("Rejected WebSocket connection over capacity: username=%s", username)
        try:
            await websocket.close(code=1013, reason="Server at capacity; try again shortly")
        except Exception:
            logger.debug("WebSocket capacity rejection close failed", exc_info=True)
        return
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
                delivery_started_at = time.perf_counter()
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

                reply_to_msg_id = data.get("reply_to_msg_id")
                reply_preview = None
                if reply_to_msg_id is not None:
                    if not isinstance(reply_to_msg_id, str) or not reply_to_msg_id.strip():
                        await websocket.send_json({
                            "type": "error", "msg_id": msg_id,
                            "message": "Invalid reply reference. Please select a message in this chat.",
                        })
                        continue
                    reply_to_msg_id = reply_to_msg_id.strip()
                    reply_preview = get_message_reply_preview(reply_to_msg_id, username, recipient)
                    if reply_preview is None:
                        await websocket.send_json({
                            "type": "error", "msg_id": msg_id,
                            "message": "The message you are replying to was not found in this conversation.",
                        })
                        continue

                # DATA_INTEGRITY_ISSUE_45: reject content changed after the client hashed it.
                provided_integrity_hash = data.get("integrity_hash")
                if provided_integrity_hash is not None and not verify_content_sha256(
                    content, provided_integrity_hash
                ):
                    await websocket.send_json({
                        "type": "error",
                        "msg_id": msg_id,
                        "message": "Message integrity verification failed; message was not sent.",
                    })
                    continue
                try:
                    block_active = users_are_blocked(username, recipient)
                except sqlite3.Error:
                    logger.exception("Could not verify block state: sender=%s recipient=%s", username, recipient)
                    await websocket.send_json({
                        "type": "error",
                        "msg_id": msg_id,
                        "message": "Message was not sent because block settings could not be verified. Please retry.",
                        "retryable": True,
                    })
                    continue
                if block_active:
                    await websocket.send_json({
                        "type": "error",
                        "msg_id": msg_id,
                        "message": "Message not sent: a block is active between you and this user.",
                    })
                    continue
                async with get_recipient_delivery_lock(recipient):
                    try:
                        inserted = persist_websocket_message(
                            msg_id, username, recipient, content, timestamp, provided_integrity_hash, reply_to_msg_id
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
                            "retryable": True,
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
                            "retryable": True,
                        })
                        continue

                    # MULTI_DEVICE_SYNC_FR26: mirror a new outgoing message to the
                    # sender's other logged-in devices before sending the state update.
                    if inserted:
                        await manager.broadcast_user(username, {
                            "type": "message_sync",
                            "msg_id": msg_id,
                            "sender": username,
                            "recipient": recipient,
                            "content": content,
                            "timestamp": timestamp,
                            "status": "sent",
                            "integrity_hash": content_sha256(content),
                            "id": get_message_row_id(msg_id),
                        }, exclude=websocket)

                    # A sent acknowledgment confirms durable server persistence, not recipient delivery.
                    await manager.broadcast_user(username, {
                        "type": "status_update", "msg_id": msg_id, "status": "sent"
                    })

                    # A duplicate retry was already persisted and must not be forwarded twice.
                    if not inserted:
                        continue

                    incoming_frame = {
                        "type": "receive_message", "msg_id": msg_id, "sender": username,
                        "recipient": recipient, "content": content, "timestamp": timestamp,
                        "integrity_hash": content_sha256(content),
                        "id": get_message_row_id(msg_id),
                        "reply_to_msg_id": reply_to_msg_id,
                        "reply_preview": reply_preview,
                    }
                    recipient_devices = manager.connections_for(recipient)
                    if recipient_devices:
                        performance_metrics.begin_delivery(
                            msg_id, username, recipient, started_at=delivery_started_at
                        )
                        delivered_devices = await manager.broadcast_user(recipient, incoming_frame)
                        if delivered_devices:
                            # Delivery state is shared across every sender device.
                            await manager.broadcast_user(username, {
                                "type": "status_update", "msg_id": msg_id, "status": "delivered"
                            })
                            mark_messages_delivered([msg_id])
                        else:
                            performance_metrics.cancel_pending_delivery(msg_id, recipient)

                    if not manager.connections_for(recipient) or not manager.visible.get(recipient, True):
                        try:
                            schedule_push(msg_id, username, recipient, content)
                        except Exception:
                            logger.exception("Failed to schedule push notification: recipient=%s", recipient)

            elif message_type == "send_group_message":
                delivery_started_at = time.perf_counter()
                group_id = data.get("group_id")
                content = data.get("content")
                msg_id = data.get("msg_id") or os.urandom(16).hex()
                timestamp = data.get("timestamp")

                if (
                    not isinstance(group_id, str) or not group_id.strip()
                    or not isinstance(content, str) or not content.strip()
                    or not isinstance(msg_id, str) or not msg_id.strip()
                    or not isinstance(timestamp, str) or not timestamp.strip()
                ):
                    await websocket.send_json({"type": "error", "message": "Invalid group message fields."})
                    continue

                group_id = group_id.strip()
                msg_id = msg_id.strip()
                group, group_members = get_group_for_delivery(group_id)
                if group is None or username not in group_members:
                    await websocket.send_json({
                        "type": "error",
                        "msg_id": msg_id,
                        "message": "Group not found or you are no longer a member.",
                    })
                    continue

                # The server resolves @names against this group's current membership.
                if e2e_service.is_encrypted_content(content):
                    mentioned_users = e2e_service.filter_client_mentions(
                        data.get("mentioned_users"), group_members, sender=username
                    )
                else:
                    mentioned_users = extract_group_mentions(content, group_members, sender=username)

                provided_integrity_hash = data.get("integrity_hash")
                if provided_integrity_hash is not None and not verify_content_sha256(
                    content, provided_integrity_hash
                ):
                    await websocket.send_json({
                        "type": "error",
                        "msg_id": msg_id,
                        "message": "Message integrity verification failed; message was not sent.",
                    })
                    continue

                async with get_recipient_delivery_lock(f"group:{group_id}"):
                    try:
                        inserted = persist_websocket_group_message(
                            msg_id, group_id, username, content, timestamp,
                            provided_integrity_hash, mentioned_users, group["name"],
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
                            "message": "Group message could not be saved. Please retry.",
                            "retryable": True,
                        })
                        continue
                    except Exception:
                        logger.exception(
                            "Unexpected group message-processing failure: username=%s group_id=%s",
                            username, group_id,
                        )
                        await websocket.send_json({
                            "type": "error",
                            "msg_id": msg_id,
                            "message": "Group message processing failed. Please retry.",
                            "retryable": True,
                        })
                        continue

                    if inserted:
                        frame = {
                            "type": "message_sync",
                            "msg_id": msg_id,
                            "sender": username,
                            "recipient": None,
                            "group_id": group_id,
                            "conversation_type": "group",
                            "group_name": group["name"],
                            "content": content,
                            "timestamp": timestamp,
                            "status": "sent",
                            "integrity_hash": content_sha256(content),
                            "id": get_message_row_id(msg_id),
                            "mentioned_users": mentioned_users,
                        }
                        await manager.broadcast_user(username, frame, exclude=websocket)

                    await manager.broadcast_user(username, {
                        "type": "status_update", "msg_id": msg_id, "status": "sent"
                    })

                    # A duplicate retry is already persisted and must not be forwarded twice.
                    if not inserted:
                        continue

                    incoming_frame = {
                        "type": "receive_message",
                        "msg_id": msg_id,
                        "sender": username,
                        "recipient": None,
                        "group_id": group_id,
                        "conversation_type": "group",
                        "group_name": group["name"],
                        "content": content,
                        "timestamp": timestamp,
                        "integrity_hash": content_sha256(content),
                        "id": get_message_row_id(msg_id),
                        "mentioned_users": mentioned_users,
                    }
                    await manager.broadcast_many_users(
                        group_members, incoming_frame, exclude_username=username
                    )

                    # USER_MENTIONS_WEB_FR25: distinct live event in addition to the group message.
                    if mentioned_users:
                        content_preview = (
                            e2e_service.ENCRYPTED_MENTION_PREVIEW
                            if e2e_service.is_encrypted_content(content)
                            else content.replace("\n", " ").strip()[:180]
                        )
                        await scalability_engine.gather_bounded(
                            (
                                lambda target=mentioned_user: manager.broadcast_user(
                                    target,
                                    {
                                        "type": "mention_notification",
                                        "notification_id": notification_id_for(msg_id, target),
                                        "msg_id": msg_id,
                                        "group_id": group_id,
                                        "group_name": group["name"],
                                        "sender": username,
                                        "content_preview": content_preview,
                                    },
                                )
                                for mentioned_user in mentioned_users
                            )
                        )

            elif message_type == "visibility":
                manager.set_visibility(username, websocket, bool(data.get("visible")))

            elif message_type == "ping":
                # Heartbeat (FR-21): lets the client detect a dead connection quickly.
                await websocket.send_json({"type": "pong"})

            elif message_type == "delivery_ack":
                msg_id = data.get("msg_id")
                if not isinstance(msg_id, str) or not msg_id.strip():
                    continue
                completed = performance_metrics.complete_delivery_ack(msg_id.strip(), recipient=username)
                if completed is None:
                    continue
                await manager.broadcast_user(completed["sender"], {
                    "type": "performance_metric",
                    "msg_id": completed["message_id"],
                    "server_delivery_latency_ms": completed["server_latency_ms"],
                    "target_ms": performance_metrics.LATENCY_TARGET_MS,
                })

            elif message_type == "latency_report":
                msg_id = data.get("msg_id")
                latency_ms = data.get("latency_ms")
                if isinstance(msg_id, str) and msg_id.strip():
                    performance_metrics.record_client_latency_report(msg_id.strip(), sender=username, latency_ms=latency_ms)

            elif message_type == "read_ack":
                sender = data.get("sender")
                msg_id = data.get("msg_id")
                if not isinstance(sender, str) or not isinstance(msg_id, str):
                    await websocket.send_json({"type": "error", "message": "Invalid read receipt."})
                    continue

                # Persist only a read receipt from the actual message recipient.
                if not mark_message_read(msg_id, sender, username):
                    continue

                await manager.broadcast_user(sender, {
                    "type": "status_update", "msg_id": msg_id, "status": "read"
                })
                # Keep the reader's other devices in sync as well.
                await manager.broadcast_user(username, {
                    "type": "message_read_sync", "msg_id": msg_id,
                    "sender": sender, "reader": username, "status": "read"
                }, exclude=websocket)
            elif message_type == "toggle_reaction":
                msg_id = data.get("msg_id")
                emoji = data.get("emoji")
                if not isinstance(msg_id, str) or not msg_id.strip() or not isinstance(emoji, str) or not emoji.strip():
                    await websocket.send_json({"type": "error", "message": "Invalid reaction fields."})
                    continue
                msg_id = msg_id.strip()
                emoji = emoji.strip()

                conn = get_db()
                try:
                    msg_row = conn.execute(
                        "SELECT msg_id, sender, recipient, group_id, conversation_type FROM messages WHERE msg_id = ?",
                        (msg_id,),
                    ).fetchone()
                    if not msg_row:
                        await websocket.send_json({"type": "error", "message": "Message not found."})
                        continue

                    if msg_row["conversation_type"] == "group":
                        group_id = msg_row["group_id"]
                        membership = conn.execute(
                            "SELECT 1 FROM group_members WHERE group_id = ? AND username = ?",
                            (group_id, username),
                        ).fetchone()
                        if not membership:
                            await websocket.send_json({"type": "error", "message": "You are not a member of this group."})
                            continue
                        group_recipients = [
                            r["username"] for r in conn.execute(
                                "SELECT username FROM group_members WHERE group_id = ?", (group_id,)
                            ).fetchall()
                        ]
                    else:
                        if username != msg_row["sender"] and username != msg_row["recipient"]:
                            await websocket.send_json({"type": "error", "message": "You are not part of this conversation."})
                            continue
                        group_recipients = None

                    result = toggle_message_reaction(conn, msg_id, username, emoji)
                finally:
                    conn.close()

                broadcast_data = {
                    "type": "reaction_update",
                    "msg_id": msg_id,
                    "emoji": emoji,
                    "username": username,
                    "action": result["action"],
                    "reactions": result["reactions"],
                }
                if group_recipients:
                    await manager.broadcast_many_users(group_recipients, broadcast_data)
                else:
                    await manager.broadcast_user(msg_row["sender"], broadcast_data)
                    if msg_row["recipient"] and msg_row["recipient"] != msg_row["sender"]:
                        await manager.broadcast_user(msg_row["recipient"], broadcast_data)
            elif message_type == "call_offer":
                call_id = data.get("call_id") or os.urandom(12).hex()
                recipient = data.get("recipient")
                group_id = data.get("group_id")
                media_type = data.get("media_type") or "voice"
                sdp = data.get("sdp")

                conn = get_db()
                try:
                    if group_id:
                        membership = conn.execute(
                            "SELECT 1 FROM group_members WHERE group_id = ? AND username = ?",
                            (group_id, username)
                        ).fetchone()
                        if not membership:
                            await websocket.send_json({"type": "error", "message": "Not a group member."})
                            continue
                        members = [
                            r["username"] for r in conn.execute(
                                "SELECT username FROM group_members WHERE group_id = ? AND username != ?",
                                (group_id, username)
                            ).fetchall()
                        ]
                        call_service.create_call(conn, call_id, username, None, group_id, media_type)
                        offer_frame = {
                            "type": "call_offer",
                            "call_id": call_id,
                            "caller": username,
                            "group_id": group_id,
                            "media_type": media_type,
                            "sdp": sdp,
                        }
                        await manager.broadcast_many_users(members, offer_frame)
                        await websocket.send_json({
                            "type": "call_ringing",
                            "call_id": call_id,
                            "group_id": group_id,
                            "media_type": media_type
                        })
                    elif recipient:
                        if recipient == username:
                            await websocket.send_json({"type": "error", "message": "Cannot call yourself."})
                            continue
                        # Check block
                        blocked = conn.execute(
                            "SELECT 1 FROM user_blocks WHERE blocker_username = ? AND blocked_username = ?",
                            (recipient, username)
                        ).fetchone()
                        if blocked:
                            await websocket.send_json({"type": "error", "message": "User is unavailable."})
                            continue
                        call_service.create_call(conn, call_id, username, recipient, None, media_type)
                        offer_frame = {
                            "type": "call_offer",
                            "call_id": call_id,
                            "caller": username,
                            "recipient": recipient,
                            "media_type": media_type,
                            "sdp": sdp,
                        }
                        await manager.broadcast_user(recipient, offer_frame)
                        await websocket.send_json({
                            "type": "call_ringing",
                            "call_id": call_id,
                            "recipient": recipient,
                            "media_type": media_type
                        })
                    else:
                        await websocket.send_json({"type": "error", "message": "Recipient or group_id required."})
                finally:
                    conn.close()

            elif message_type == "call_answer":
                call_id = data.get("call_id")
                sdp = data.get("sdp")
                session = call_service.get_active_call(call_id)
                if not session:
                    await websocket.send_json({"type": "error", "message": "Call not found."})
                    continue
                conn = get_db()
                try:
                    call_service.accept_call(conn, call_id, username)
                finally:
                    conn.close()
                delivered = await manager.broadcast_user(session.caller, {
                    "type": "call_answer",
                    "call_id": call_id,
                    "answerer": username,
                    "sdp": sdp,
                })

            elif message_type == "ice_candidate":
                call_id = data.get("call_id")
                candidate = data.get("candidate")
                target = data.get("target")
                session = call_service.get_active_call(call_id)
                if session and not target:
                    target = session.recipient if username == session.caller else session.caller
                if target:
                    await manager.broadcast_user(target, {
                        "type": "ice_candidate",
                        "call_id": call_id,
                        "sender": username,
                        "candidate": candidate,
                    })

            elif message_type == "call_reject":
                call_id = data.get("call_id")
                reason = data.get("reason") or "declined"
                session = call_service.get_active_call(call_id)
                conn = get_db()
                try:
                    call_service.end_call(conn, call_id, status="declined")
                finally:
                    conn.close()
                if session and session.caller != username:
                    await manager.broadcast_user(session.caller, {
                        "type": "call_rejected",
                        "call_id": call_id,
                        "by": username,
                        "reason": reason,
                    })

            elif message_type == "call_end":
                call_id = data.get("call_id")
                session = call_service.get_active_call(call_id)
                conn = get_db()
                try:
                    call_service.end_call(conn, call_id, status="completed")
                    if session and session.group_id:
                        members = [
                            r["username"] for r in conn.execute(
                                "SELECT username FROM group_members WHERE group_id = ?",
                                (session.group_id,)
                            ).fetchall()
                        ]
                        targets = set(members)
                    elif session:
                        targets = set(session.participants) | ({session.recipient} if session.recipient else set())
                    else:
                        targets = set()
                finally:
                    conn.close()

                targets.discard(username)
                for t in targets:
                    if t:
                        await manager.broadcast_user(t, {
                            "type": "call_ended",
                            "call_id": call_id,
                            "by": username,
                        })
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


# --- Message Reactions Endpoints (Issue #34, FR-23) ---
@app.post("/api/messages/{msg_id}/react")
async def react_to_message_endpoint(msg_id: str, payload: ReactionSchema, request: Request):
    username = current_username(request)
    emoji = (payload.emoji or "").strip()
    if not emoji:
        raise HTTPException(status_code=400, detail="Emoji cannot be empty.")

    conn = get_db()
    try:
        msg_row = conn.execute(
            "SELECT msg_id, sender, recipient, group_id, conversation_type FROM messages WHERE msg_id = ?",
            (msg_id,),
        ).fetchone()
        if not msg_row:
            raise HTTPException(status_code=404, detail="Message not found.")

        if msg_row["conversation_type"] == "group":
            group_id = msg_row["group_id"]
            membership = conn.execute(
                "SELECT 1 FROM group_members WHERE group_id = ? AND username = ?",
                (group_id, username),
            ).fetchone()
            if not membership:
                raise HTTPException(status_code=403, detail="Not a group member.")
            recipients = [
                r["username"] for r in conn.execute(
                    "SELECT username FROM group_members WHERE group_id = ?", (group_id,)
                ).fetchall()
            ]
        else:
            if username != msg_row["sender"] and username != msg_row["recipient"]:
                raise HTTPException(status_code=403, detail="Not a participant in this conversation.")
            recipients = [msg_row["sender"]]
            if msg_row["recipient"] and msg_row["recipient"] != msg_row["sender"]:
                recipients.append(msg_row["recipient"])

        result = toggle_message_reaction(conn, msg_id, username, emoji)
    finally:
        conn.close()

    broadcast_data = {
        "type": "reaction_update",
        "msg_id": msg_id,
        "emoji": emoji,
        "username": username,
        "action": result["action"],
        "reactions": result["reactions"],
    }
    if msg_row["conversation_type"] == "group":
        await manager.broadcast_many_users(recipients, broadcast_data)
    else:
        for u in recipients:
            await manager.broadcast_user(u, broadcast_data)

    return result


@app.get("/api/messages/{msg_id}/reactions")
async def get_message_reactions_endpoint(msg_id: str, request: Request):
    username = current_username(request)
    conn = get_db()
    try:
        msg_row = conn.execute(
            "SELECT msg_id, sender, recipient, group_id, conversation_type FROM messages WHERE msg_id = ?",
            (msg_id,),
        ).fetchone()
        if not msg_row:
            raise HTTPException(status_code=404, detail="Message not found.")
        if msg_row["conversation_type"] == "group":
            group_id = msg_row["group_id"]
            membership = conn.execute(
                "SELECT 1 FROM group_members WHERE group_id = ? AND username = ?",
                (group_id, username),
            ).fetchone()
            if not membership:
                raise HTTPException(status_code=403, detail="Not a group member.")
        else:
            if username != msg_row["sender"] and username != msg_row["recipient"]:
                raise HTTPException(status_code=403, detail="Not a participant in this conversation.")

        reactions = get_reactions_for_message(conn, msg_id)
        return {"msg_id": msg_id, "reactions": reactions}
    finally:
        conn.close()


# --- Voice / Video Calls Endpoints (Issue #39, FR-28) ---
@app.get("/api/calls/history")
async def get_calls_history_endpoint(request: Request):
    username = current_username(request)
    conn = get_db()
    try:
        calls = call_service.get_call_history(conn, username)
        return {"calls": calls}
    finally:
        conn.close()


@app.get("/api/calls/active")
async def get_active_calls_endpoint(request: Request):
    username = current_username(request)
    for call in call_service._active_calls.values():
        if username in call.participants or username == call.recipient:
            return {"active_call": call.to_dict()}
    return {"active_call": None}


# --- End-to-End Encryption Public Key Directory (NFR-04, Issue #44) ---
@app.post("/api/e2e/keys")
async def upload_e2e_public_key(payload: E2EPublicKeySchema, request: Request):
    """Save the logged-in user's public ECDH key (server never receives private keys)."""
    username = current_username(request)
    try:
        record = e2e_service.save_public_key(username, payload.public_key)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return {"success": True, **record}


@app.get("/api/e2e/keys/{target_username}")
async def fetch_e2e_public_key(target_username: str, request: Request):
    """Return a user's public ECDH key so the caller's browser can derive a shared AES key."""
    current_username(request)
    clean_target = target_username.strip()
    if not clean_target:
        raise HTTPException(status_code=400, detail="Username is required.")
    record = e2e_service.get_public_key(clean_target)
    return {
        "username": clean_target,
        "has_key": record is not None,
        "public_key": record["public_key"] if record else None,
        "updated_at": record["updated_at"] if record else None,
    }


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
    await manager.broadcast_many_users(targets, frame)


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
 
# --- Group chat API (web UI) ---
def require_group_membership(conn, group_id: str, username: str):
    group = conn.execute(
        "SELECT group_id, name, icon, creator, created_at FROM groups WHERE group_id = ?",
        (group_id,),
    ).fetchone()
    if group is None:
        raise HTTPException(status_code=404, detail="Group not found")

    membership = conn.execute(
        "SELECT role FROM group_members WHERE group_id = ? AND username = ?",
        (group_id, username),
    ).fetchone()
    if membership is None:
        raise HTTPException(status_code=403, detail="You are not a member of this group")
    return dict(group), membership["role"]


@app.post("/api/groups")
async def create_group(payload: GroupCreateSchema, request: Request):
    creator = current_username(request)
    name = payload.name.strip()
    icon = (payload.icon or "👥").strip()
    if not name:
        raise HTTPException(status_code=400, detail="Group name is required")
    if len(name) > 80:
        raise HTTPException(status_code=400, detail="Group name must be 80 characters or fewer")
    if not icon or len(icon) > 16:
        raise HTTPException(status_code=400, detail="Choose a valid group icon")

    members = []
    for value in payload.members:
        member = value.strip() if isinstance(value, str) else ""
        if member and member != creator and member not in members:
            members.append(member)
    if not members:
        raise HTTPException(status_code=400, detail="Choose at least one other member")
    if len(members) > 100:
        raise HTTPException(status_code=400, detail="A group can have at most 100 invited members")

    conn = get_db()
    try:
        placeholders = ",".join("?" for _ in members)
        rows = conn.execute(
            f"SELECT username FROM users WHERE username IN ({placeholders})", members
        ).fetchall()
        found = {row["username"] for row in rows}
        missing = [member for member in members if member not in found]
        if missing:
            raise HTTPException(
                status_code=400,
                detail="Unknown group member(s): " + ", ".join(missing),
            )

        group_id = "grp-" + uuid.uuid4().hex
        conn.execute("BEGIN IMMEDIATE")
        conn.execute(
            "INSERT INTO groups (group_id, name, icon, creator) VALUES (?, ?, ?, ?)",
            (group_id, name, icon, creator),
        )
        conn.execute(
            "INSERT INTO group_members (group_id, username, role) VALUES (?, ?, 'admin')",
            (group_id, creator),
        )
        conn.executemany(
            "INSERT INTO group_members (group_id, username, role) VALUES (?, ?, 'member')",
            [(group_id, member) for member in members],
        )
        conn.commit()
    except HTTPException:
        conn.rollback()
        raise
    except sqlite3.IntegrityError as error:
        conn.rollback()
        logger.exception("Could not create group for username=%s", creator)
        raise HTTPException(status_code=409, detail="Could not create the group") from error
    finally:
        conn.close()

    return {
        "success": True,
        "group": {
            "group_id": group_id,
            "name": name,
            "icon": icon,
            "creator": creator,
            "member_count": len(members) + 1,
        },
    }


@app.get("/api/groups")
async def list_my_groups(request: Request):
    username = current_username(request)
    conn = get_db()
    try:
        rows = conn.execute(
            """
            SELECT g.group_id, g.name, g.icon, g.creator, g.created_at,
                   COUNT(all_members.username) AS member_count
            FROM groups AS g
            JOIN group_members AS mine ON mine.group_id = g.group_id
            JOIN group_members AS all_members ON all_members.group_id = g.group_id
            WHERE mine.username = ?
            GROUP BY g.group_id
            ORDER BY g.created_at DESC, g.name COLLATE NOCASE
            """,
            (username,),
        ).fetchall()
        return {"groups": [dict(row) for row in rows]}
    finally:
        conn.close()


@app.get("/api/groups/{group_id}/members")
async def get_group_members(group_id: str, request: Request):
    username = current_username(request)
    conn = get_db()
    try:
        require_group_membership(conn, group_id, username)
        rows = conn.execute(
            """
            SELECT username, role, joined_at
            FROM group_members WHERE group_id = ?
            ORDER BY CASE role WHEN 'admin' THEN 0 ELSE 1 END, joined_at, username
            """,
            (group_id,),
        ).fetchall()
        return {"members": [dict(row) for row in rows]}
    finally:
        conn.close()


@app.get("/api/groups/{group_id}/history")
async def get_group_history(group_id: str, request: Request):
    username = current_username(request)
    conn = get_db()
    try:
        group, _ = require_group_membership(conn, group_id, username)
        rows = conn.execute(
            """
            SELECT rowid AS id, msg_id, sender, content, timestamp, status,
                   integrity_hash, reply_to_msg_id, group_id, conversation_type, mentioned_users
            FROM messages
            WHERE conversation_type = 'group' AND group_id = ?
            ORDER BY rowid ASC
            """,
            (group_id,),
        ).fetchall()
        assert_message_integrity(rows)
        msg_ids = [row["msg_id"] for row in rows if row["msg_id"]]
        reactions_map = get_reactions_for_messages(conn, msg_ids)
        history = []
        for row in rows:
            item = dict(row)
            item.pop("integrity_hash", None)
            item["reply_preview"] = None
            try:
                item["mentioned_users"] = json.loads(item.get("mentioned_users") or "[]")
            except (TypeError, ValueError):
                item["mentioned_users"] = []
            item["reactions"] = reactions_map.get(item["msg_id"], [])
            history.append(item)
        return {"group": group, "history": history}
    finally:
        conn.close()



# USER_MENTIONS_WEB_FR25: authenticated, durable mention notifications.
@app.get("/api/mentions")
async def list_mention_notifications(request: Request, unread_only: bool = True):
    username = current_username(request)
    condition = " AND read_at IS NULL" if unread_only else ""
    conn = get_db()
    try:
        rows = conn.execute(
            """SELECT notification_id, username, msg_id, group_id, group_name,
                      sender, content_preview, created_at, read_at
               FROM mention_notifications
               WHERE username = ?""" + condition + " ORDER BY created_at DESC, rowid DESC LIMIT 100",
            (username,),
        ).fetchall()
        return {"notifications": [dict(row) for row in rows]}
    finally:
        conn.close()


@app.post("/api/mentions/{notification_id}/read")
async def mark_mention_notification_read(notification_id: str, request: Request):
    username = current_username(request)
    conn = get_db()
    try:
        cursor = conn.execute(
            "UPDATE mention_notifications SET read_at = CURRENT_TIMESTAMP WHERE notification_id = ? AND username = ?",
            (notification_id, username),
        )
        conn.commit()
        return {"success": cursor.rowcount > 0}
    finally:
        conn.close()

# --- Fetch Conversation History Endpoint (ADDED FOR ISSUE #23) ---
@app.get("/api/messages/history/{user1}/{user2}")
async def get_chat_history(user1: str, user2: str):
    conn = get_db()
    try:
        cursor = conn.cursor()
        cursor.execute("""
            SELECT msg_id, sender, recipient, content, timestamp, status, integrity_hash, reply_to_msg_id 
            FROM messages 
            WHERE (sender = ? AND recipient = ?) OR (sender = ? AND recipient = ?)
            ORDER BY rowid ASC
        """, (user1, user2, user2, user1))
        rows = cursor.fetchall()
        assert_message_integrity(rows)
        msg_ids = [row["msg_id"] for row in rows if row["msg_id"]]
        reactions_map = get_reactions_for_messages(conn, msg_ids)
        history = []
        for row in rows:
            item = dict(row)
            item.pop("integrity_hash", None)
            item["reply_preview"] = get_message_reply_preview(
                item.get("reply_to_msg_id"), item["sender"], item["recipient"]
            )
            item["reactions"] = reactions_map.get(item["msg_id"], [])
            history.append(item)
        return {"history": history}
    finally:
        conn.close()


# --- Missed-message sync after a reconnect (FR-21) ---
SYNC_PAGE_LIMIT = 200


@app.get("/api/messages/sync")
async def sync_missed_messages(request: Request, since: int = -1):
    """Messages addressed to the logged-in user with a row id greater than `since`.

    since < 0 only returns the current newest id (used the first time a device
    syncs, so old history is not replayed as "new"). Pages of SYNC_PAGE_LIMIT;
    has_more tells the client to call again with since=latest_id. Messages that
    were still 'sent' become 'delivered', and their senders are told.
    """
    username = current_username(request)
    newly_delivered = []
    conn = get_db()
    try:
        latest = conn.execute(
            """
            SELECT COALESCE(MAX(rowid), 0) FROM messages
            WHERE recipient = ? OR (
                conversation_type = 'group'
                AND group_id IN (SELECT group_id FROM group_members WHERE username = ?)
            )
            """,
            (username, username),
        ).fetchone()[0]
        if since < 0:
            return {"messages": [], "latest_id": latest, "has_more": False}

        rows = conn.execute(
            """
            SELECT rowid AS id, msg_id, sender, recipient, content, timestamp, status,
                   integrity_hash, reply_to_msg_id, group_id, conversation_type
            FROM messages
            WHERE recipient = ? AND rowid > ?
            UNION ALL
            SELECT rowid AS id, msg_id, sender, recipient, content, timestamp, status,
                   integrity_hash, reply_to_msg_id, group_id, conversation_type
            FROM messages
            WHERE conversation_type = 'group'
              AND group_id IN (SELECT group_id FROM group_members WHERE username = ?)
              AND rowid > ?
            ORDER BY id ASC
            LIMIT ?
            """,
            (username, since, username, since, SYNC_PAGE_LIMIT),
        ).fetchall()
        assert_message_integrity(rows)
        messages = [dict(row) for row in rows]
        msg_ids = [m["msg_id"] for m in messages if m.get("msg_id")]
        reactions_map = get_reactions_for_messages(conn, msg_ids)
        for message in messages:
            message.pop("integrity_hash", None)
            message["reply_preview"] = get_message_reply_preview(
                message.get("reply_to_msg_id"), message["sender"], message["recipient"]
            )
            message["reactions"] = reactions_map.get(message.get("msg_id"), [])

        newly_delivered = [
            m for m in messages
            if m["status"] == "sent" and m.get("conversation_type") != "group"
        ]
        if newly_delivered:
            conn.executemany(
                "UPDATE messages SET status = 'delivered' WHERE rowid = ? AND status = 'sent'",
                [(m["id"],) for m in newly_delivered],
            )
            conn.commit()
            for m in newly_delivered:
                m["status"] = "delivered"
    finally:
        conn.close()

    for m in newly_delivered:
        sender_socket = manager.active_connections.get(m["sender"])
        if sender_socket is None:
            continue
        try:
            await sender_socket.send_json(
                {"type": "status_update", "msg_id": m["msg_id"], "status": "delivered"}
            )
        except Exception:
            logger.exception("Could not send delivery status after sync: sender=%s", m["sender"])
            manager.disconnect(m["sender"], sender_socket)

    return {
        "messages": messages,
        "latest_id": messages[-1]["id"] if messages else latest,
        "has_more": len(messages) == SYNC_PAGE_LIMIT,
    }
 
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
        SELECT msg_id, sender, recipient, content, timestamp, integrity_hash 
        FROM messages 
        WHERE (sender = ? OR recipient = ?) AND content LIKE ?
        ORDER BY rowid DESC
    """, (username, username, search_term))
    message_rows = cursor.fetchall()
    conn.close()
    assert_message_integrity(message_rows)
    messages = []
    for row in message_rows:
        item = dict(row)
        item.pop("integrity_hash", None)
        messages.append(item)
    return {"contacts": contacts, "messages": messages}