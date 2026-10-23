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
    with open("static/index.html", "r") as f:
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
                print(f"Message from {username} to {recipient}: {content}")
    except WebSocketDisconnect:
        manager.disconnect(username)