import asyncio
import json
import time
from typing import Dict, List, Optional
from fastapi import FastAPI, WebSocket, WebSocketDisconnect, HTTPException, status
from fastapi.staticfiles import StaticFiles
from fastapi.responses import HTMLResponse, FileResponse
from pydantic import BaseModel

app = FastAPI(title="Chat Application (WebSocket) - Python Base")

# Serve static frontend files
app.mount("/static", StaticFiles(directory="static"), name="static")


# In-Memory Database (Base Store for Sprint 1)

users_db: Dict[str, str] = {}  # username -> password
sessions_db: Dict[str, str] = {}  # token -> username
chat_history: List[dict] = []  # List of message objects


# Pydantic Schemas

class UserRegister(BaseModel):
    username: str
    password: str

class UserLogin(BaseModel):
    username: str
    password: str


# REST Endpoints 

@app.get("/")
async def get_index():
    return FileResponse("static/index.html")

@app.post("/api/register")
async def register(user: UserRegister):
    if not user.username or not user.password:
        raise HTTPException(status_code=400, detail="Username and password required.")
    if user.username in users_db:
        raise HTTPException(status_code=400, detail="Username already taken.")
    
    users_db[user.username] = user.password
    return {"message": "Account created successfully. You can now log in."}

@app.post("/api/login")
async def login(user: UserLogin):
    stored_password = users_db.get(user.username)
    if not stored_password or stored_password != user.password:
        raise HTTPException(status_code=401, detail="Invalid username or password.")
    
    # Generate simple session token
    token = f"token_{user.username}_{int(time.time())}"
    sessions_db[token] = user.username
    return {"token": token, "username": user.username}


# WebSocket Connection Manager (FR-07, FR-08, FR-09, FR-10, FR-21)

class ConnectionManager:
    def __init__(self):
        # Maps username -> active WebSocket connection
        self.active_connections: Dict[str, WebSocket] = {}

    async def connect(self, username: str, websocket: WebSocket):
        await websocket.accept()
        self.active_connections[username] = websocket

    def disconnect(self, username: str):
        if username in self.active_connections:
            del self.active_connections[username]

    async def send_personal_message(self, message: dict, recipient: str):
        if recipient in self.active_connections:
            await self.active_connections[recipient].send_text(json.dumps(message))
            return True
        return False

    async def broadcast(self, message: dict):
        for connection in self.active_connections.values():
            await connection.send_text(json.dumps(message))

manager = ConnectionManager()

@app.websocket("/ws/{token}")
async def websocket_endpoint(websocket: WebSocket, token: str):
    # Verify session token
    username = sessions_db.get(token)
    if not username:
        await websocket.close(code=status.WS_1008_POLICY_VIOLATION)
        return

    await manager.connect(username, websocket)

    # FR-12: Send message history upon successful authentication/connect
    await websocket.send_text(json.dumps({
        "type": "INIT_HISTORY",
        "username": username,
        "history": chat_history
    }))

    # Notify online status
    await manager.broadcast({
        "type": "USER_STATUS",
        "username": username,
        "status": "online"
    })

    try:
        while True:
            raw_data = await websocket.receive_text()
            data = json.loads(raw_data)
            
            msg_type = data.get("type")

            # FR-08 & FR-09: Process text messaging
            if msg_type == "SEND_MESSAGE":
                recipient = data.get("recipient", "Global")
                text = data.get("text", "").strip()
                
                if not text:
                    continue

                msg_obj = {
                    "id": f"msg_{int(time.time() * 1000)}",
                    "sender": username,
                    "recipient": recipient if recipient else "Global",
                    "text": text,
                    "timestamp": time.strftime("%H:%M:%S"),
                    "status": "delivered" if (recipient in manager.active_connections or recipient == "Global") else "sent"
                }

                chat_history.append(msg_obj)

                # Route 1-on-1 vs Global Broadcast
                if recipient and recipient != "Global":
                    delivered = await manager.send_personal_message({"type": "NEW_MESSAGE", "message": msg_obj}, recipient)
                    await websocket.send_text(json.dumps({"type": "NEW_MESSAGE", "message": msg_obj}))
                else:
                    await manager.broadcast({"type": "NEW_MESSAGE", "message": msg_obj})

    except WebSocketDisconnect:
        manager.disconnect(username)
        await manager.broadcast({
            "type": "USER_STATUS",
            "username": username,
            "status": "offline"
        })

if __name__ == "__main__":
    import uvicorn
    uvicorn.run("main:app", host="0.0.0.0", port=8000, reload=True)