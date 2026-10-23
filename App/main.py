from fastapi import FastAPI, WebSocket, WebSocketDisconnect
from fastapi.staticfiles import StaticFiles
from fastapi.responses import HTMLResponse
import os
from database import init_db

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