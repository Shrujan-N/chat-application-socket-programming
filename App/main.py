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
from typing import Dict
from database import init_db, get_db
from security import (
    hash_password, verify_password, create_token, decode_token,
    TokenError, SESSION_TIMEOUT_MINUTES,
)
 
 
class RegisterSchema(BaseModel):
    username: str = ""
    email: str = ""
    password: str = ""
 
 
class LoginSchema(BaseModel):
    username: str = ""
    password: str = ""
 
 
MAX_FAILED_ATTEMPTS = 5
LOCKOUT_MINUTES = int(os.environ.get("LOCKOUT_MINUTES", "15"))
PUBLIC_API_PATHS = {"/api/register", "/api/login"}
 
app = FastAPI(title="Chat Application - Socket Programming")
import asyncio
import shutil
import uuid
from datetime import datetime, timezone
from pathlib import Path

from fastapi import FastAPI, File, HTTPException, UploadFile, WebSocket, WebSocketDisconnect
from fastapi.responses import HTMLResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field
from database import get_db, init_db

BASE = Path(__file__).resolve().parent
STATIC = BASE / "static"
UPLOADS = BASE / "uploads"
STATIC.mkdir(exist_ok=True)
UPLOADS.mkdir(exist_ok=True)
app = FastAPI(title="Chat Application")
init_db()
app.mount("/static", StaticFiles(directory=str(STATIC)), name="static")
app.mount("/uploads", StaticFiles(directory=str(UPLOADS)), name="uploads")

APP_DIR = os.path.dirname(os.path.abspath(__file__))
STATIC_DIR = os.path.join(APP_DIR, "static")
UPLOADS_DIR = os.path.join(APP_DIR, "uploads")


# --- Session protection: every /api route except register/login needs a valid token ---
def get_bearer_token(request: Request) -> str:
    header = request.headers.get("Authorization", "")
    return header[7:].strip() if header.startswith("Bearer ") else ""
 
 
@app.middleware("http")
async def require_session(request: Request, call_next):
    path = request.url.path
    if path.startswith("/api/") and path not in PUBLIC_API_PATHS:
        try:
            decode_token(get_bearer_token(request))
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
 
 
# --- Session refresh (sliding idle timeout) and logout ---
@app.get("/api/session")
async def refresh_session(request: Request):
    # The middleware already rejected missing/expired/invalid tokens.
    claims = decode_token(get_bearer_token(request))
    return {
        "success": True,
        "username": claims["sub"],
        "token": create_token(claims["sub"]),
        "expires_in": SESSION_TIMEOUT_MINUTES * 60
    }
 
 
@app.post("/api/logout")
async def logout_user(request: Request):
    claims = decode_token(get_bearer_token(request))
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
        claims = decode_token(websocket.query_params.get("token", ""))
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
 
def now():
    return datetime.now(timezone.utc).isoformat(timespec="seconds")

def private_id(a, b):
    return "private:" + "|".join(sorted([a, b]))

class Connections:
    def __init__(self):
        self.clients = {}
        self.lock = asyncio.Lock()
    async def connect(self, username, ws):
        await ws.accept()
        async with self.lock:
            old = self.clients.get(username)
            self.clients[username] = ws
        if old and old is not ws:
            try: await old.close(code=4001)
            except Exception: pass
    async def disconnect(self, username, ws):
        async with self.lock:
            if self.clients.get(username) is ws:
                self.clients.pop(username, None)
    async def send(self, username, event):
        async with self.lock:
            ws = self.clients.get(username)
        if ws:
            try:
                await ws.send_json(event)
                return True
            except Exception:
                return False
        return False
    async def many(self, users, event, exclude=()):
        for user in set(users) - set(exclude):
            await self.send(user, event)

manager = Connections()

def members(group_id):
    with get_db() as db:
        return [r["username"] for r in db.execute(
            "SELECT username FROM group_members WHERE group_id=?", (group_id,)).fetchall()]

def require_member(group_id, username):
    with get_db() as db:
        row = db.execute("SELECT role FROM group_members WHERE group_id=? AND username=?",
                         (group_id, username)).fetchone()
    if not row:
        raise HTTPException(403, "You are not a member of this group.")
    return row["role"]

def visible_message(row, username):
    m = dict(row)
    with get_db() as db:
        hidden = db.execute("SELECT 1 FROM user_message_deletions WHERE msg_id=? AND username=?",
                            (m["msg_id"], username)).fetchone()
    if hidden:
        return None
    if m["deleted_everyone"]:
        m["content"] = "This message was deleted."
    m["edited"] = bool(m["edited"])
    m["deleted_everyone"] = bool(m["deleted_everyone"])
    if m.get("sender") == username:
        with get_db() as db:
            receipts = db.execute(
                "SELECT delivered_at, read_at FROM message_receipts WHERE msg_id=?",
                (m["msg_id"],),
            ).fetchall()
        if not receipts:
            m["status"] = "sent"
        elif all(r["read_at"] for r in receipts):
            m["status"] = "read"
        elif all(r["delivered_at"] for r in receipts):
            m["status"] = "delivered"
        else:
            m["status"] = "sent"
    return m

@app.get("/", response_class=HTMLResponse)
async def index():
    return HTMLResponse((STATIC / "index.html").read_text(encoding="utf-8"))

@app.get("/health")
async def health():
    return {"status": "ok"}

@app.get("/api/users")
async def users():
    with get_db() as db:
        rows = db.execute("SELECT username FROM users ORDER BY username").fetchall()
    return {"users": [r["username"] for r in rows]}

@app.get("/api/contacts/{username}")
async def contacts(username: str):
    with get_db() as db:
        rows = db.execute("SELECT contact_id FROM contacts WHERE user_id=? AND status='accepted' ORDER BY contact_id",
                          (username,)).fetchall()
    return {"contacts": [r["contact_id"] for r in rows]}

@app.get("/api/contacts/pending/{username}")
async def pending(username: str):
    with get_db() as db:
        rows = db.execute("SELECT user_id FROM contacts WHERE contact_id=? AND status='pending' ORDER BY user_id",
                          (username,)).fetchall()
    return {"pending": [r["user_id"] for r in rows]}

class Contact(BaseModel):
    user_id: str
    contact_username: str

@app.post("/api/contacts/add")
async def add_contact(p: Contact):
    user, target = p.user_id.strip(), p.contact_username.strip()
    if not user or not target or user == target:
        raise HTTPException(400, "Enter another valid username.")
    with get_db() as db:
        if not db.execute("SELECT 1 FROM users WHERE username=?", (user,)).fetchone():
            raise HTTPException(404, "Current user not found.")
        db.execute("INSERT OR IGNORE INTO users(username,email,password) VALUES(?,?,?)",
                   (target, target + "@example.com", "demo-only"))
        if db.execute("SELECT 1 FROM contacts WHERE user_id=? AND contact_id=?", (user,target)).fetchone():
            raise HTTPException(409, "A request or contact already exists.")
        db.execute("INSERT INTO contacts(user_id,contact_id,status) VALUES(?,?,'pending')", (user,target))
    return {"success": True}

class Respond(BaseModel):
    user_id: str
    requester_id: str
    action: str

@app.post("/api/contacts/respond")
async def respond(p: Respond):
    if p.action not in ("accept","decline"):
        raise HTTPException(400, "Action must be accept or decline.")
    with get_db() as db:
        row = db.execute("SELECT 1 FROM contacts WHERE user_id=? AND contact_id=? AND status='pending'",
                         (p.requester_id,p.user_id)).fetchone()
        if not row: raise HTTPException(404, "Pending request not found.")
        if p.action == "accept":
            db.execute("UPDATE contacts SET status='accepted' WHERE user_id=? AND contact_id=?",
                       (p.requester_id,p.user_id))
            db.execute("INSERT OR REPLACE INTO contacts(user_id,contact_id,status) VALUES(?,?,'accepted')",
                       (p.user_id,p.requester_id))
        else:
            db.execute("DELETE FROM contacts WHERE user_id=? AND contact_id=?", (p.requester_id,p.user_id))
    return {"success": True}

@app.post("/api/contacts/remove")
async def remove_contact(p: Contact):
    with get_db() as db:
        db.execute("""DELETE FROM contacts WHERE (user_id=? AND contact_id=?) OR
                   (user_id=? AND contact_id=?)""", (p.user_id,p.contact_username,p.contact_username,p.user_id))
    return {"success": True}

@app.get("/api/groups/{username}")
async def list_groups(username: str):
    with get_db() as db:
        rows = db.execute("""SELECT g.group_id,g.name,g.icon,gm.role FROM groups g
          JOIN group_members gm ON gm.group_id=g.group_id WHERE gm.username=? ORDER BY g.name""",
          (username,)).fetchall()
    return {"groups": [dict(r) for r in rows]}

class CreateGroup(BaseModel):
    creator: str
    name: str = Field(min_length=1, max_length=80)
    icon: str = Field(default="👥", max_length=12)
    members: list[str] = Field(default_factory=list)

@app.post("/api/groups")
async def create_group(p: CreateGroup):
    name = p.name.strip()
    if not name: raise HTTPException(400, "Group name is required.")
    gid = str(uuid.uuid4())
    selected = set(p.members) | {p.creator}
    with get_db() as db:
        rows = db.execute("SELECT username FROM users").fetchall()
        known = {r["username"] for r in rows}
        if not selected <= known: raise HTTPException(400, "Unknown user selected.")
        db.execute("INSERT INTO groups(group_id,name,icon,creator) VALUES(?,?,?,?)",
                   (gid,name,p.icon.strip() or "👥",p.creator))
        for user in selected:
            db.execute("INSERT INTO group_members(group_id,username,role) VALUES(?,?,?)",
                       (gid,user,"admin" if user == p.creator else "member"))
    await manager.many(selected, {"type":"groups_changed"})
    return {"success":True,"group_id":gid}

@app.get("/api/groups/{group_id}/members")
async def group_members(group_id: str, username: str):
    require_member(group_id,username)
    with get_db() as db:
        group = db.execute("SELECT group_id,name,icon,creator FROM groups WHERE group_id=?", (group_id,)).fetchone()
        rows = db.execute("SELECT username,role FROM group_members WHERE group_id=? ORDER BY username", (group_id,)).fetchall()
    if not group: raise HTTPException(404,"Group not found.")
    return {"group":dict(group),"members":[dict(r) for r in rows]}

class GroupChange(BaseModel):
    username: str
    value: str = ""

class GroupMember(BaseModel):
    username: str
    target: str

@app.post("/api/groups/{group_id}/rename")
async def rename_group(group_id: str,p: GroupChange):
    if require_member(group_id,p.username)!="admin": raise HTTPException(403,"Only admins can rename groups.")
    if not p.value.strip(): raise HTTPException(400,"Group name cannot be empty.")
    with get_db() as db: db.execute("UPDATE groups SET name=? WHERE group_id=?",(p.value.strip(),group_id))
    await manager.many(members(group_id),{"type":"groups_changed"})
    return {"success":True}

@app.post("/api/groups/{group_id}/icon")
async def group_icon(group_id: str,p: GroupChange):
    if require_member(group_id,p.username)!="admin": raise HTTPException(403,"Only admins can change the icon.")
    with get_db() as db: db.execute("UPDATE groups SET icon=? WHERE group_id=?",(p.value.strip()[:12] or "👥",group_id))
    await manager.many(members(group_id),{"type":"groups_changed"})
    return {"success":True}

@app.post("/api/groups/{group_id}/members/add")
async def add_group_member(group_id: str,p: GroupMember):
    if require_member(group_id,p.username)!="admin": raise HTTPException(403,"Only admins can add members.")
    with get_db() as db:
        if not db.execute("SELECT 1 FROM users WHERE username=?",(p.target,)).fetchone():
            raise HTTPException(404,"User not found.")
        db.execute("INSERT OR IGNORE INTO group_members(group_id,username,role) VALUES(?,?,'member')",(group_id,p.target))
    await manager.many(set(members(group_id))|{p.target},{"type":"groups_changed"})
    return {"success":True}

@app.post("/api/groups/{group_id}/members/remove")
async def remove_group_member(group_id: str,p: GroupMember):
    if require_member(group_id,p.username)!="admin": raise HTTPException(403,"Only admins can remove members.")
    with get_db() as db:
        group=db.execute("SELECT creator FROM groups WHERE group_id=?",(group_id,)).fetchone()
        if not group: raise HTTPException(404,"Group not found.")
        if p.target==group["creator"]: raise HTTPException(400,"Cannot remove the creator.")
        db.execute("DELETE FROM group_members WHERE group_id=? AND username=?",(group_id,p.target))
    await manager.many(set(members(group_id))|{p.target},{"type":"groups_changed"})
    return {"success":True}

@app.post("/api/groups/{group_id}/admins")
async def promote_admin(group_id: str,p: GroupMember):
    if require_member(group_id,p.username)!="admin": raise HTTPException(403,"Only admins can change roles.")
    with get_db() as db:
        cur=db.execute("UPDATE group_members SET role='admin' WHERE group_id=? AND username=?",(group_id,p.target))
        if not cur.rowcount: raise HTTPException(404,"Group member not found.")
    await manager.many(members(group_id),{"type":"groups_changed"})
    return {"success":True}

@app.delete("/api/groups/{group_id}/admins/{target}")
async def demote_admin(group_id: str,target: str,username: str):
    if require_member(group_id,username)!="admin": raise HTTPException(403,"Only admins can change roles.")
    with get_db() as db:
        group=db.execute("SELECT creator FROM groups WHERE group_id=?",(group_id,)).fetchone()
        if not group: raise HTTPException(404,"Group not found.")
        if target==group["creator"]: raise HTTPException(400,"Creator must remain admin.")
        cur = db.execute("UPDATE group_members SET role='member' WHERE group_id=? AND username=? AND role='admin'",(group_id,target))
        if not cur.rowcount:
            raise HTTPException(404, "Group admin not found.")
    await manager.many(members(group_id),{"type":"groups_changed"})
    return {"success":True}

@app.get("/api/messages/history/{user1}/{user2}")
async def private_history(user1: str, user2: str):
    if user1 == user2:
        raise HTTPException(400, "Choose another user.")
    with get_db() as db:
        for username in (user1, user2):
            if not db.execute("SELECT 1 FROM users WHERE username=?", (username,)).fetchone():
                raise HTTPException(404, "User not found.")
        accepted = db.execute(
            "SELECT 1 FROM contacts WHERE user_id=? AND contact_id=? AND status='accepted'",
            (user1, user2),
        ).fetchone()
        if not accepted:
            raise HTTPException(403, "You can only view history with an accepted contact.")
        rows = db.execute(
            "SELECT * FROM messages WHERE conversation_type='private' AND conversation_id=? ORDER BY rowid",
            (private_id(user1, user2),),
        ).fetchall()
    return {"history": [m for row in rows if (m := visible_message(row, user1)) is not None]}

@app.get("/api/groups/{group_id}/history")
async def group_history(group_id: str,username: str):
    require_member(group_id,username)
    with get_db() as db:
        rows=db.execute("SELECT * FROM messages WHERE conversation_type='group' AND conversation_id=? ORDER BY rowid",
                        (group_id,)).fetchall()
    return {"history":[m for row in rows if (m:=visible_message(row,username)) is not None]}

@app.get("/api/search")
async def search(username: str,q: str):
    term="%"+q.strip()+"%"
    if not q.strip(): return {"contacts":[],"messages":[]}
    with get_db() as db:
        c=db.execute("SELECT contact_id FROM contacts WHERE user_id=? AND status='accepted' AND contact_id LIKE ?",(username,term)).fetchall()
        rows=db.execute("""SELECT * FROM messages
          WHERE content LIKE ? AND (
            (conversation_type='private' AND (sender=? OR recipient=?) AND EXISTS (
              SELECT 1 FROM contacts c WHERE c.user_id=?
                AND c.contact_id=CASE WHEN sender=? THEN recipient ELSE sender END
                AND c.status='accepted'))
            OR (conversation_type='group' AND group_id IN
              (SELECT group_id FROM group_members WHERE username=?)))
          ORDER BY rowid DESC LIMIT 100""",
          (term, username, username, username, username, username)).fetchall()
    return {"contacts":[r["contact_id"] for r in c],"messages":[m for row in rows if (m:=visible_message(row,username)) is not None]}

@app.post("/api/upload")
async def upload(file: UploadFile=File(...)):
    raw_filename = (file.filename or "attachment.bin").replace("\\", "/")
    filename = Path(raw_filename).name or "attachment.bin"
    safe = f"{uuid.uuid4().hex}_{filename}"
    destination = UPLOADS / safe
    size = 0
    try:
        with destination.open("wb") as dest:
            while chunk := await file.read(1024 * 1024):
                size += len(chunk)
                if size > 10 * 1024 * 1024:
                    raise HTTPException(413, "Files must be 10 MB or smaller.")
                dest.write(chunk)
    except HTTPException:
        destination.unlink(missing_ok=True)
        raise
    finally:
        await file.close()
    return {"success":True,"filename":filename,"file_url":f"/uploads/{safe}","content_type":file.content_type}

async def save_and_send(username,data):
    content=str(data.get("content","")).strip()
    if not content: return
    msg_id=str(data.get("msg_id") or uuid.uuid4())
    timestamp=str(data.get("timestamp") or now())
    gid=data.get("group_id")
    recipient=data.get("recipient")
    if gid:
        try:
            require_member(gid,username)
        except HTTPException:
            await manager.send(username, {"type":"error", "message":"You are not a member of this group."})
            return
        targets=set(members(gid))-{username}
        kind,cid="group",gid
    else:
        if not recipient or recipient==username: return
        with get_db() as db:
            accepted=db.execute("SELECT 1 FROM contacts WHERE user_id=? AND contact_id=? AND status='accepted'",(username,recipient)).fetchone()
        if not accepted:
            await manager.send(username,{"type":"error","message":"You can only message accepted contacts."})
            return
        targets={recipient};kind,cid="private",private_id(username,recipient)
    with get_db() as db:
        if db.execute("SELECT 1 FROM messages WHERE msg_id=?", (msg_id,)).fetchone():
            await manager.send(username, {"type": "error", "message": "Duplicate message ID; message was not sent."})
            return
        db.execute("""INSERT INTO messages(msg_id,conversation_type,conversation_id,sender,recipient,group_id,content,timestamp)
                      VALUES(?,?,?,?,?,?,?,?)""",(msg_id,kind,cid,username,recipient,gid,content,timestamp))
        for target in targets:
            db.execute("INSERT OR IGNORE INTO message_receipts(msg_id,username) VALUES(?,?)",(msg_id,target))
    event={"type":"receive_message","msg_id":msg_id,"sender":username,"recipient":recipient,"group_id":gid,
           "conversation_type":kind,"conversation_id":cid,"content":content,"timestamp":timestamp,"edited":False}
    await manager.send(username,{"type":"status_update","msg_id":msg_id,"status":"sent"})
    for target in targets:
        if await manager.send(target,event):
            with get_db() as db: db.execute("UPDATE message_receipts SET delivered_at=? WHERE msg_id=? AND username=?",(now(),msg_id,target))
            await manager.send(username,{"type":"status_update","msg_id":msg_id,"username":target,"status":"delivered"})

async def deliver_pending(username):
    with get_db() as db:
        rows=db.execute("""SELECT m.* FROM messages m JOIN message_receipts r ON r.msg_id=m.msg_id
          WHERE r.username=? AND r.delivered_at IS NULL AND m.deleted_everyone=0 ORDER BY m.rowid""",(username,)).fetchall()
    for row in rows:
        with get_db() as db:
            hidden=db.execute("SELECT 1 FROM user_message_deletions WHERE msg_id=? AND username=?",(row["msg_id"],username)).fetchone()
        if hidden: continue
        delivered = await manager.send(username,{"type":"receive_message","msg_id":row["msg_id"],"sender":row["sender"],
          "recipient":row["recipient"],"group_id":row["group_id"],"conversation_type":row["conversation_type"],
          "conversation_id":row["conversation_id"],"content":row["content"],"timestamp":row["timestamp"],"edited":bool(row["edited"])})
        if delivered:
            with get_db() as db: db.execute("UPDATE message_receipts SET delivered_at=? WHERE msg_id=? AND username=?",(now(),row["msg_id"],username))
            await manager.send(row["sender"],{"type":"status_update","msg_id":row["msg_id"],"username":username,"status":"delivered"})

async def edit_message(username,data):
    mid=str(data.get("msg_id","")); content=str(data.get("content","")).strip()
    if not mid or not content:
        await manager.send(username, {"type":"error", "message":"A message ID and non-empty content are required."})
        return
    with get_db() as db:
        row=db.execute("SELECT * FROM messages WHERE msg_id=?",(mid,)).fetchone()
        if not row or row["sender"]!=username or row["deleted_everyone"]:
            await manager.send(username,{"type":"error","message":"Only your existing, non-deleted messages can be edited."});return
        db.execute("UPDATE messages SET content=?,edited=1,edited_at=? WHERE msg_id=?",(content,now(),mid))
        targets=set(members(row["group_id"])) if row["group_id"] else {row["sender"],row["recipient"]}
    await manager.many(targets,{"type":"message_edit","msg_id":mid,"content":content,"edited":True})

async def delete_message(username,data):
    mid=str(data.get("msg_id",""));scope=data.get("scope","me")
    if scope not in ("me","everyone"):
        await manager.send(username, {"type":"error", "message":"Delete scope must be 'me' or 'everyone'."})
        return
    with get_db() as db:
        row=db.execute("SELECT * FROM messages WHERE msg_id=?",(mid,)).fetchone()
        if not row:
            await manager.send(username,{"type":"error","message":"Message not found."});return
        targets=set(members(row["group_id"]))|{row["sender"]} if row["group_id"] else {row["sender"],row["recipient"]}
        if username not in targets:
            await manager.send(username,{"type":"error","message":"You are not part of this conversation."});return
        if scope=="everyone":
            if row["sender"]!=username:
                await manager.send(username,{"type":"error","message":"Only the sender can delete for everyone."});return
            db.execute("UPDATE messages SET deleted_everyone=1,content='This message was deleted.' WHERE msg_id=?",(mid,))
        else:
            db.execute("INSERT OR IGNORE INTO user_message_deletions(msg_id,username) VALUES(?,?)",(mid,username))
            targets={username}
    await manager.many(targets,{"type":"message_delete","msg_id":mid,"scope":scope})

async def read_ack(username,data):
    mid=str(data.get("msg_id",""))
    with get_db() as db:
        row=db.execute("SELECT sender FROM messages WHERE msg_id=?",(mid,)).fetchone()
        receipt=db.execute("SELECT 1 FROM message_receipts WHERE msg_id=? AND username=?",(mid,username)).fetchone()
        if not row or not receipt:return
        db.execute("UPDATE message_receipts SET read_at=? WHERE msg_id=? AND username=?",(now(),mid,username))
    await manager.send(row["sender"],{"type":"status_update","msg_id":mid,"username":username,"status":"read"})

@app.websocket("/ws/{username}")
async def websocket_endpoint(ws: WebSocket,username: str):
    with get_db() as db: exists=db.execute("SELECT 1 FROM users WHERE username=?",(username,)).fetchone()
    if not exists:
        await ws.close(code=1008,reason="Unknown user");return
    await manager.connect(username,ws)
    await deliver_pending(username)
    try:
        while True:
            data=await ws.receive_json(); kind=data.get("type")
            if kind=="send_message": await save_and_send(username,data)
            elif kind in ("typing","stop_typing"):
                gid=data.get("group_id");recipient=data.get("recipient")
                if gid:
                    try:
                        require_member(gid, username)
                        targets=set(members(gid))-{username}
                    except HTTPException:
                        await manager.send(username, {"type":"error", "message":"You are not a member of this group."})
                        continue
                else:
                    if not recipient or recipient == username:
                        continue
                    with get_db() as db:
                        accepted = db.execute(
                            "SELECT 1 FROM contacts WHERE user_id=? AND contact_id=? AND status='accepted'",
                            (username, recipient),
                        ).fetchone()
                    if not accepted:
                        await manager.send(username, {"type":"error", "message":"Typing indicators are only available to accepted contacts."})
                        continue
                    targets={recipient}
                await manager.many(targets,{"type":kind,"username":username,"group_id":gid,"recipient":recipient})
            elif kind=="edit_message": await edit_message(username,data)
            elif kind=="delete_message": await delete_message(username,data)
            elif kind=="read_ack": await read_ack(username,data)
    except WebSocketDisconnect:
        await manager.disconnect(username,ws)
    except (HTTPException, ValueError, TypeError) as exc:
        await manager.send(username, {"type":"error", "message": str(getattr(exc, "detail", exc))})
    finally:
        await manager.disconnect(username, ws)
