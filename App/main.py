from fastapi import FastAPI, WebSocket, WebSocketDisconnect
from fastapi.staticfiles import StaticFiles
from fastapi.responses import HTMLResponse
import os
from database import init_db
from pydantic import BaseModel
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
    with open("static/index.html", "r") as f:
        return HTMLResponse(content=f.read())

# --- Contact Management Endpoints (ADDED FOR ISSUE #17) ---
class AddContactSchema(BaseModel):
    user_id: str
    contact_username: str

@app.get("/api/contacts/{username}")
async def get_contacts(username: str):
    conn = get_db()
    cursor = conn.cursor()
    cursor.execute("SELECT contact_id FROM contacts WHERE user_id = ? AND status = 'accepted'", (username,))
    rows = cursor.fetchall()
    conn.close()
    return {"contacts": [row["contact_id"] for row in rows]}

@app.post("/api/contacts/add")
async def add_contact(payload: AddContactSchema):
    conn = get_db()
    cursor = conn.cursor()
    cursor.execute("SELECT username FROM users WHERE username = ?", (payload.contact_username,))
    if not cursor.fetchone():
        conn.close()
        return {"success": False, "error": "User not found"}
        
    try:
        cursor.execute("INSERT INTO contacts (user_id, contact_id, status) VALUES (?, ?, 'accepted')", (payload.user_id, payload.contact_username))
        cursor.execute("INSERT INTO contacts (user_id, contact_id, status) VALUES (?, ?, 'accepted')", (payload.contact_username, payload.user_id))
        conn.commit()
        conn.close()
        return {"success": True, "message": f"Added {payload.contact_username} to contacts"}
    except Exception:
        conn.close()
        return {"success": False, "error": "Contact already exists"}