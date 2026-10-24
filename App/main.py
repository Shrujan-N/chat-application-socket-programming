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
    members: list[str] = []

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
        db.execute("UPDATE group_members SET role='member' WHERE group_id=? AND username=?",(group_id,target))
    await manager.many(members(group_id),{"type":"groups_changed"})
    return {"success":True}

@app.get("/api/messages/history/{user1}/{user2}")
async def private_history(user1: str,user2: str):
    with get_db() as db:
        rows=db.execute("SELECT * FROM messages WHERE conversation_type='private' AND conversation_id=? ORDER BY rowid",
                        (private_id(user1,user2),)).fetchall()
    return {"history":[m for row in rows if (m:=visible_message(row,user1)) is not None]}

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
        c=db.execute("SELECT contact_id FROM contacts WHERE user_id=? AND status='accepted' AND contact_id LIKE=?",(username,term)).fetchall()
        rows=db.execute("SELECT * FROM messages WHERE (sender=? OR recipient=? OR group_id IN (SELECT group_id FROM group_members WHERE username=?)) AND content LIKE ? ORDER BY rowid DESC LIMIT 100",
                        (username,username,username,term)).fetchall()
    return {"contacts":[r["contact_id"] for r in c],"messages":[m for row in rows if (m:=visible_message(row,username)) is not None]}

@app.post("/api/upload")
async def upload(file: UploadFile=File(...)):
    filename=Path(file.filename or "attachment.bin").name
    safe=f"{uuid.uuid4().hex}_{filename}"
    with (UPLOADS/safe).open("wb") as dest: shutil.copyfileobj(file.file,dest)
    return {"success":True,"filename":filename,"file_url":f"/uploads/{safe}","content_type":file.content_type}

async def save_and_send(username,data):
    content=str(data.get("content","")).strip()
    if not content: return
    msg_id=str(data.get("msg_id") or uuid.uuid4())
    timestamp=str(data.get("timestamp") or now())
    gid=data.get("group_id")
    recipient=data.get("recipient")
    if gid:
        require_member(gid,username)
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
        await manager.send(username,{"type":"receive_message","msg_id":row["msg_id"],"sender":row["sender"],
          "recipient":row["recipient"],"group_id":row["group_id"],"conversation_type":row["conversation_type"],
          "conversation_id":row["conversation_id"],"content":row["content"],"timestamp":row["timestamp"],"edited":bool(row["edited"])})
        with get_db() as db: db.execute("UPDATE message_receipts SET delivered_at=? WHERE msg_id=? AND username=?",(now(),row["msg_id"],username))
        await manager.send(row["sender"],{"type":"status_update","msg_id":row["msg_id"],"username":username,"status":"delivered"})

async def edit_message(username,data):
    mid=str(data.get("msg_id","")); content=str(data.get("content","")).strip()
    if not mid or not content:return
    with get_db() as db:
        row=db.execute("SELECT * FROM messages WHERE msg_id=?",(mid,)).fetchone()
        if not row or row["sender"]!=username or row["deleted_everyone"]:
            await manager.send(username,{"type":"error","message":"Only your existing, non-deleted messages can be edited."});return
        db.execute("UPDATE messages SET content=?,edited=1,edited_at=? WHERE msg_id=?",(content,now(),mid))
        targets=set(members(row["group_id"])) if row["group_id"] else {row["sender"],row["recipient"]}
    await manager.many(targets,{"type":"message_edit","msg_id":mid,"content":content,"edited":True})

async def delete_message(username,data):
    mid=str(data.get("msg_id",""));scope=data.get("scope","me")
    if scope not in ("me","everyone"):return
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
                    try: targets=set(members(gid))-{username}; require_member(gid,username)
                    except HTTPException: continue
                else: targets={recipient} if recipient else set()
                await manager.many(targets,{"type":kind,"username":username,"group_id":gid,"recipient":recipient})
            elif kind=="edit_message": await edit_message(username,data)
            elif kind=="delete_message": await delete_message(username,data)
            elif kind=="read_ack": await read_ack(username,data)
    except WebSocketDisconnect:
        await manager.disconnect(username,ws)
