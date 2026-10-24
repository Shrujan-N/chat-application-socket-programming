from fastapi import FastAPI, WebSocket, WebSocketDisconnect, UploadFile, File, HTTPException, Request
from fastapi.staticfiles import StaticFiles
from fastapi.responses import HTMLResponse, JSONResponse
import os
import re
import math
import shutil
import sqlite3
from datetime import datetime, timedelta, timezone
from pydantic import BaseModel
from typing import Dict, Optional
from database import init_db, get_db
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
 
 
MAX_FAILED_ATTEMPTS = 5
LOCKOUT_MINUTES = int(os.environ.get("LOCKOUT_MINUTES", "15"))
PUBLIC_API_PATHS = {
    "/api/register", "/api/login",
    "/api/password/forgot", "/api/password/verify", "/api/password/reset",
}
 
app = FastAPI(title="Chat Application - Socket Programming")

# Initialize SQLite database schema
init_db()

APP_DIR = os.path.dirname(os.path.abspath(__file__))
STATIC_DIR = os.path.join(APP_DIR, "static")
UPLOADS_DIR = profile_service.UPLOADS_DIR   # same folder as before (APP_DIR/uploads) unless CHAT_UPLOADS_DIR is set


# --- Session protection: every /api route except register/login needs a valid token ---
def get_bearer_token(request: Request) -> str:
    header = request.headers.get("Authorization", "")
    return header[7:].strip() if header.startswith("Bearer ") else ""
 
 
@app.middleware("http")
async def require_session(request: Request, call_next):
    path = request.url.path
    if path.startswith("/api/") and path not in PUBLIC_API_PATHS:
        try:
            validate_session(get_bearer_token(request))   # also rejects logged-out sessions
        except TokenError as error:
            return JSONResponse(status_code=401, content={"detail": str(error)})
    return await call_next(request)
 
 
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
 
 
# --- Active WebSocket Registry (ADDED FOR ISSUE #19) ---
class ConnectionManager:
    def __init__(self):
        self.active_connections: Dict[str, WebSocket] = {}
 
    async def connect(self, username: str, websocket: WebSocket):
        await websocket.accept()
        self.active_connections[username] = websocket
 
    def disconnect(self, username: str):
        if username in self.active_connections:
            del self.active_connections[username]
 
manager = ConnectionManager()
 
@app.websocket("/ws/{username}")
async def websocket_endpoint(websocket: WebSocket, username: str):
    # Only an authenticated user may open a socket for their own username
    try:
        claims = validate_session(websocket.query_params.get("token", ""))
    except TokenError:
        await websocket.close(code=4401)
        return
    if claims.get("sub") != username:
        await websocket.close(code=4403)
        return
 
    await manager.connect(username, websocket)
    try:
        while True:
            data = await websocket.receive_json()
            if data.get("type") == "send_message":
                recipient = data.get("recipient")
                content = data.get("content")
                msg_id = data.get("msg_id", str(os.urandom(4).hex()))
                timestamp = data.get("timestamp")
 
                # Store message permanently in SQLite Database (ADDED FOR ISSUE #23)
                conn = get_db()
                cursor = conn.cursor()
                cursor.execute("""
                    INSERT INTO messages (msg_id, sender, recipient, content, timestamp, status)
                    VALUES (?, ?, ?, ?, ?, 'sent')
                """, (msg_id, username, recipient, content, timestamp))
                conn.commit()
                conn.close()
                
                incoming_frame = {
                    "type": "receive_message",
                    "msg_id": msg_id,
                    "sender": username,
                    "recipient": recipient,
                    "content": content,
                    "timestamp": timestamp
                }
                
                if recipient in manager.active_connections:
                    await manager.active_connections[recipient].send_json(incoming_frame)
                    # Send 'delivered' status ACK back to sender (ADDED FOR ISSUE #22)
                    await websocket.send_json({"type": "status_update", "msg_id": msg_id, "status": "delivered"})
 
            # Handle Read Receipt ACK frames from recipient (ADDED FOR ISSUE #22)
            elif data.get("type") == "read_ack":
                sender = data.get("sender")
                msg_id = data.get("msg_id")
                if sender in manager.active_connections:
                    await manager.active_connections[sender].send_json({
                        "type": "status_update",
                        "msg_id": msg_id,
                        "status": "read"
                    })
    except WebSocketDisconnect:
        manager.disconnect(username)
 
# --- Profile Management (FR-05) ---
def current_username(request: Request) -> str:
    """The logged-in user, taken from the session token (never from the URL/body)."""
    return validate_session(get_bearer_token(request))["sub"]


def client_ip(request: Request):
    return request.client.host if request.client else None


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