from fastapi import FastAPI, WebSocket, WebSocketDisconnect
from fastapi.staticfiles import StaticFiles
from fastapi.responses import HTMLResponse
import os
from database import init_db
from pydantic import BaseModel
from typing import Dict
from database import get_db

app = FastAPI(title="Chat Application - Socket Programming")

# Initialize SQLite database schema
init_db()

# Mount Static Files and Uploads
app.mount("/static", StaticFiles(directory="static"), name="static")

if not os.path.exists("uploads"):
    os.makedirs("uploads")
app.mount("/uploads", StaticFiles(directory="uploads"), name="uploads")

@app.get("/")
async def get_index():
    with open("static/index.html", "r", encoding="utf-8") as f:
        return HTMLResponse(content=f.read())

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

# --- Fetch All Users Endpoint for Account Switcher (ADDED FOR EASY SWITCHING) ---
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

    cursor.execute("INSERT OR IGNORE INTO users (username, email, password) VALUES (?, ? || '@example.com', 'Pass1234')", 
                   (payload.contact_username, payload.contact_username))
    
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