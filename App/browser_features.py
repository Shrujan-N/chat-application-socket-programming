"""Browser-only chat features missing from the first web UI integration.

The module intentionally uses the authenticated HTTP session and the existing
WebSocket connection manager. TCP/terminal chat behaviour is not changed.
"""
from __future__ import annotations

import sqlite3
from datetime import datetime, timezone
from typing import Optional

from fastapi import APIRouter, HTTPException, Query, Request
from pydantic import BaseModel

from message_integrity import content_sha256, verify_content_sha256
from security import TokenError
from auth_service import validate_session
from web_database import get_db

router = APIRouter()


class GroupUpdatePayload(BaseModel):
    name: Optional[str] = None
    icon: Optional[str] = None


class GroupMemberPayload(BaseModel):
    username: str = ""


class MessageEditPayload(BaseModel):
    content: str = ""
    integrity_hash: Optional[str] = None


def current_user(request: Request) -> str:
    header = request.headers.get("Authorization", "")
    token = header[7:].strip() if header.startswith("Bearer ") else ""
    try:
        return validate_session(token)["sub"]
    except TokenError as error:
        raise HTTPException(status_code=401, detail=str(error) or "Authentication required") from error


def _group_context(conn, group_id: str, username: str):
    row = conn.execute(
        """SELECT g.group_id, g.name, g.icon, g.creator, g.created_at, gm.role
           FROM groups g JOIN group_members gm ON gm.group_id = g.group_id
           WHERE g.group_id = ? AND gm.username = ?""",
        (group_id, username),
    ).fetchone()
    if row is None:
        exists = conn.execute("SELECT 1 FROM groups WHERE group_id = ?", (group_id,)).fetchone()
        if exists is None:
            raise HTTPException(status_code=404, detail="Group not found")
        raise HTTPException(status_code=403, detail="You are not a member of this group")
    return dict(row)


def _require_admin(group: dict):
    if group.get("role") != "admin":
        raise HTTPException(status_code=403, detail="Only group admins can manage this group")


def _group_members(conn, group_id: str) -> list[str]:
    return [row["username"] for row in conn.execute(
        "SELECT username FROM group_members WHERE group_id = ? ORDER BY username",
        (group_id,),
    ).fetchall()]


async def _broadcast_group_event(request: Request, usernames: list[str], event: dict):
    manager = getattr(request.app.state, "chat_manager", None)
    if manager and usernames:
        await manager.broadcast_many_users(usernames, event)


@router.patch("/api/groups/{group_id}")
async def update_group(group_id: str, payload: GroupUpdatePayload, request: Request):
    username = current_user(request)
    name = payload.name.strip() if payload.name is not None else None
    icon = payload.icon.strip() if payload.icon is not None else None
    if name is None and icon is None:
        raise HTTPException(status_code=400, detail="Provide a group name or icon to update")
    if name is not None and (not name or len(name) > 80):
        raise HTTPException(status_code=400, detail="Group name must contain 1 to 80 characters")
    if icon is not None and (not icon or len(icon) > 16):
        raise HTTPException(status_code=400, detail="Group icon must contain 1 to 16 characters")

    conn = get_db()
    try:
        group = _group_context(conn, group_id, username)
        _require_admin(group)
        members = _group_members(conn, group_id)
        next_name = name if name is not None else group["name"]
        next_icon = icon if icon is not None else group["icon"]
        conn.execute("UPDATE groups SET name = ?, icon = ? WHERE group_id = ?", (next_name, next_icon, group_id))
        conn.commit()
    finally:
        conn.close()

    await _broadcast_group_event(request, members, {
        "type": "group_updated", "group_id": group_id,
        "name": next_name, "icon": next_icon,
    })
    return {"success": True, "group": {"group_id": group_id, "name": next_name, "icon": next_icon}}


@router.post("/api/groups/{group_id}/members")
async def add_group_member(group_id: str, payload: GroupMemberPayload, request: Request):
    username = current_user(request)
    target = payload.username.strip()
    if not target or target == username:
        raise HTTPException(status_code=400, detail="Choose another registered user")

    conn = get_db()
    try:
        group = _group_context(conn, group_id, username)
        _require_admin(group)
        exists = conn.execute("SELECT 1 FROM users WHERE username = ?", (target,)).fetchone()
        if exists is None:
            raise HTTPException(status_code=404, detail="User not found")
        if conn.execute(
            "SELECT 1 FROM group_members WHERE group_id = ? AND username = ?", (group_id, target)
        ).fetchone():
            raise HTTPException(status_code=409, detail="That user is already a group member")
        conn.execute(
            "INSERT INTO group_members (group_id, username, role) VALUES (?, ?, 'member')",
            (group_id, target),
        )
        members = _group_members(conn, group_id)
        conn.commit()
    finally:
        conn.close()

    await _broadcast_group_event(request, members, {
        "type": "group_member_added", "group_id": group_id,
        "username": target, "by": username,
    })
    return {"success": True, "members": members}


@router.delete("/api/groups/{group_id}/members/{target_username}")
async def remove_group_member(group_id: str, target_username: str, request: Request):
    username = current_user(request)
    target = target_username.strip()
    conn = get_db()
    try:
        group = _group_context(conn, group_id, username)
        _require_admin(group)
        if target == group["creator"]:
            raise HTTPException(status_code=400, detail="The group creator cannot be removed")
        exists = conn.execute(
            "SELECT 1 FROM group_members WHERE group_id = ? AND username = ?", (group_id, target)
        ).fetchone()
        if exists is None:
            raise HTTPException(status_code=404, detail="That user is not a group member")
        members_before = _group_members(conn, group_id)
        conn.execute("DELETE FROM group_members WHERE group_id = ? AND username = ?", (group_id, target))
        members_after = _group_members(conn, group_id)
        conn.commit()
    finally:
        conn.close()

    event = {"type": "group_member_removed", "group_id": group_id, "username": target, "by": username}
    await _broadcast_group_event(request, members_before, event)
    return {"success": True, "members": members_after}


@router.post("/api/groups/{group_id}/admins")
async def promote_group_admin(group_id: str, payload: GroupMemberPayload, request: Request):
    username = current_user(request)
    target = payload.username.strip()
    conn = get_db()
    try:
        group = _group_context(conn, group_id, username)
        _require_admin(group)
        member = conn.execute(
            "SELECT role FROM group_members WHERE group_id = ? AND username = ?", (group_id, target)
        ).fetchone()
        if member is None:
            raise HTTPException(status_code=404, detail="That user is not a group member")
        if member["role"] == "admin":
            raise HTTPException(status_code=409, detail="That user is already an admin")
        conn.execute(
            "UPDATE group_members SET role = 'admin' WHERE group_id = ? AND username = ?",
            (group_id, target),
        )
        members = _group_members(conn, group_id)
        conn.commit()
    finally:
        conn.close()
    await _broadcast_group_event(request, members, {
        "type": "group_admin_changed", "group_id": group_id,
        "username": target, "role": "admin", "by": username,
    })
    return {"success": True}


@router.delete("/api/groups/{group_id}/admins/{target_username}")
async def demote_group_admin(group_id: str, target_username: str, request: Request):
    username = current_user(request)
    target = target_username.strip()
    conn = get_db()
    try:
        group = _group_context(conn, group_id, username)
        _require_admin(group)
        if target == group["creator"]:
            raise HTTPException(status_code=400, detail="The group creator must remain an admin")
        member = conn.execute(
            "SELECT role FROM group_members WHERE group_id = ? AND username = ?", (group_id, target)
        ).fetchone()
        if member is None or member["role"] != "admin":
            raise HTTPException(status_code=404, detail="That group admin was not found")
        admins = conn.execute(
            "SELECT COUNT(*) FROM group_members WHERE group_id = ? AND role = 'admin'", (group_id,)
        ).fetchone()[0]
        if admins <= 1:
            raise HTTPException(status_code=400, detail="Promote another admin before demoting the last admin")
        conn.execute(
            "UPDATE group_members SET role = 'member' WHERE group_id = ? AND username = ?",
            (group_id, target),
        )
        members = _group_members(conn, group_id)
        conn.commit()
    finally:
        conn.close()
    await _broadcast_group_event(request, members, {
        "type": "group_admin_changed", "group_id": group_id,
        "username": target, "role": "member", "by": username,
    })
    return {"success": True}


def get_group_message_reply_preview(reply_to_msg_id: Optional[str], group_id: str):
    if not reply_to_msg_id:
        return None
    conn = get_db()
    try:
        row = conn.execute(
            """SELECT msg_id, sender, content, timestamp, integrity_hash
               FROM messages WHERE msg_id = ? AND conversation_type = 'group'
                 AND group_id = ?""",
            (reply_to_msg_id, group_id),
        ).fetchone()
    finally:
        conn.close()
    if row is None:
        return None
    if not verify_content_sha256(row["content"], row["integrity_hash"]):
        raise HTTPException(status_code=500, detail="The original message failed integrity verification")
    return {"msg_id": row["msg_id"], "sender": row["sender"], "content": row["content"], "timestamp": row["timestamp"]}


def _message_audience(conn, msg_id: str, username: str):
    row = conn.execute(
        """SELECT msg_id, sender, recipient, group_id, conversation_type, content,
                  timestamp, edited, deleted_everyone, integrity_hash
           FROM messages WHERE msg_id = ?""",
        (msg_id,),
    ).fetchone()
    if row is None:
        raise HTTPException(status_code=404, detail="Message not found")
    message = dict(row)
    if message["conversation_type"] == "group":
        group_id = message["group_id"]
        members = _group_members(conn, group_id)
        if username not in members:
            raise HTTPException(status_code=403, detail="You are not a member of this group")
    else:
        if username not in {message["sender"], message["recipient"]}:
            raise HTTPException(status_code=403, detail="You are not part of this conversation")
        members = list(dict.fromkeys([message["sender"], message["recipient"]]))
    return message, members


async def _edit_message(msg_id: str, username: str, content: str, integrity_hash: Optional[str], manager):
    content = content.strip()
    if not content:
        raise HTTPException(status_code=400, detail="Message content cannot be empty")
    if len(content) > 20000:
        raise HTTPException(status_code=400, detail="Message is too long to edit")
    expected_hash = content_sha256(content)
    if integrity_hash and not verify_content_sha256(content, integrity_hash):
        raise HTTPException(status_code=400, detail="Message integrity verification failed")
    conn = get_db()
    try:
        message, audience = _message_audience(conn, msg_id, username)
        if message["sender"] != username:
            raise HTTPException(status_code=403, detail="Only the message sender can edit it")
        if message["deleted_everyone"]:
            raise HTTPException(status_code=409, detail="A deleted message cannot be edited")
        edited_at = datetime.now(timezone.utc).isoformat()
        conn.execute(
            "UPDATE messages SET content = ?, integrity_hash = ?, edited = 1, edited_at = ? WHERE msg_id = ?",
            (content, expected_hash, edited_at, msg_id),
        )
        conn.commit()
    finally:
        conn.close()
    event = {
        "type": "message_edit", "msg_id": msg_id, "sender": username,
        "recipient": message["recipient"], "group_id": message["group_id"],
        "conversation_type": message["conversation_type"], "content": content,
        "integrity_hash": expected_hash, "edited": True, "edited_at": edited_at,
    }
    if manager:
        await manager.broadcast_many_users(audience, event)
    return {"success": True, "edited_at": edited_at}


async def _delete_message(msg_id: str, username: str, scope: str, manager, websocket=None):
    if scope not in {"me", "everyone"}:
        raise HTTPException(status_code=400, detail="Delete scope must be 'me' or 'everyone'")
    conn = get_db()
    try:
        message, audience = _message_audience(conn, msg_id, username)
        if scope == "everyone":
            if message["sender"] != username:
                raise HTTPException(status_code=403, detail="Only the message sender can delete it for everyone")
            tombstone = "[This message was deleted]"
            tombstone_hash = content_sha256(tombstone)
            conn.execute(
                """UPDATE messages SET content = ?, integrity_hash = ?, deleted_everyone = 1,
                   edited = 0, edited_at = NULL WHERE msg_id = ?""",
                (tombstone, tombstone_hash, msg_id),
            )
            conn.execute(
                "UPDATE mention_notifications SET content_preview = ? WHERE msg_id = ?",
                (tombstone, msg_id),
            )
        else:
            conn.execute(
                "INSERT OR IGNORE INTO user_message_deletions (msg_id, username) VALUES (?, ?)",
                (msg_id, username),
            )
        conn.commit()
    finally:
        conn.close()

    event = {
        "type": "message_delete", "msg_id": msg_id, "scope": scope,
        "sender": message["sender"], "recipient": message["recipient"],
        "group_id": message["group_id"], "conversation_type": message["conversation_type"],
        "deleted_by": username,
    }
    if scope == "me":
        if manager:
            await manager.broadcast_user(username, event, exclude=websocket)
        elif websocket is not None:
            await websocket.send_json(event)
    elif manager:
        await manager.broadcast_many_users(audience, event)
    return {"success": True, "scope": scope}


@router.patch("/api/messages/{msg_id}")
async def edit_message_endpoint(msg_id: str, payload: MessageEditPayload, request: Request):
    username = current_user(request)
    return await _edit_message(
        msg_id, username, payload.content, payload.integrity_hash,
        getattr(request.app.state, "chat_manager", None),
    )


@router.delete("/api/messages/{msg_id}")
async def delete_message_endpoint(msg_id: str, request: Request, scope: str = Query("me")):
    username = current_user(request)
    return await _delete_message(
        msg_id, username, scope, getattr(request.app.state, "chat_manager", None)
    )


@router.get("/api/search/complete")
async def search_everything(request: Request, q: str = Query("", max_length=100)):
    """Search contacts/groups and recent message candidates for local E2E search.

    Message bodies are E2E encrypted for some conversations. The server cannot
    search their plaintext, so it returns a bounded set of the user's recent
    authorized messages for decryption and filtering in the user's browser. All
    plaintext matches are also included even when older than that candidate set.
    """
    username = current_user(request)
    query = q.strip()
    if not query:
        return {"contacts": [], "groups": [], "messages": []}
    pattern = f"%{query}%"
    conn = get_db()
    try:
        contacts = [row["contact_id"] for row in conn.execute(
            """SELECT contact_id FROM contacts
               WHERE user_id = ? AND status = 'accepted' AND contact_id LIKE ?
               ORDER BY contact_id COLLATE NOCASE LIMIT 50""",
            (username, pattern),
        ).fetchall()]
        groups = [dict(row) for row in conn.execute(
            """SELECT g.group_id, g.name, g.icon, g.creator, gm.role AS my_role,
                      COUNT(all_members.username) AS member_count
               FROM groups g JOIN group_members gm ON gm.group_id = g.group_id
               JOIN group_members all_members ON all_members.group_id = g.group_id
               WHERE gm.username = ? AND g.name LIKE ?
               GROUP BY g.group_id ORDER BY g.name COLLATE NOCASE LIMIT 50""",
            (username, pattern),
        ).fetchall()]

        access_sql = """(
            (m.conversation_type = 'private' AND (m.sender = ? OR m.recipient = ?))
            OR (m.conversation_type = 'group' AND m.group_id IN
                (SELECT group_id FROM group_members WHERE username = ?))
        ) AND NOT EXISTS (
            SELECT 1 FROM user_message_deletions d
            WHERE d.msg_id = m.msg_id AND d.username = ?
        )"""
        params = (username, username, username, username)
        columns = """m.rowid AS id, m.msg_id, m.sender, m.recipient, m.group_id,
                      m.conversation_type, m.content, m.timestamp, m.edited,
                      m.deleted_everyone, m.integrity_hash, g.name AS group_name"""

        # Search stored plaintext across the full history, then add a bounded
        # recent candidate window so this browser can search decrypted content.
        plain_rows = conn.execute(
            f"""SELECT {columns} FROM messages m LEFT JOIN groups g ON g.group_id = m.group_id
                WHERE {access_sql} AND m.content LIKE ?
                ORDER BY m.rowid DESC LIMIT 200""",
            (*params, pattern),
        ).fetchall()
        encrypted_candidates = conn.execute(
            f"""SELECT {columns} FROM messages m LEFT JOIN groups g ON g.group_id = m.group_id
                WHERE {access_sql}
                ORDER BY m.rowid DESC LIMIT 500""",
            params,
        ).fetchall()

        by_id = {}
        for row in [*plain_rows, *encrypted_candidates]:
            item = dict(row)
            by_id[item["msg_id"]] = item
        messages = sorted(by_id.values(), key=lambda item: item["id"], reverse=True)
        for item in messages:
            if not verify_content_sha256(item["content"], item["integrity_hash"]):
                raise HTTPException(status_code=500, detail="A stored message failed integrity verification")
            item.pop("integrity_hash", None)
    finally:
        conn.close()
    return {
        "contacts": contacts,
        "groups": groups,
        "messages": messages,
        "encrypted_search_candidate_limit": 500,
    }


async def handle_browser_websocket_event(message_type: str, data: dict, username: str, websocket, manager) -> bool:
    """Handle browser-only WebSocket events; return True when consumed."""
    if message_type in {"typing", "typing_stop"}:
        group_id = data.get("group_id")
        if group_id:
            conn = get_db()
            try:
                _group_context(conn, str(group_id), username)
                members = _group_members(conn, str(group_id))
            except HTTPException as error:
                await websocket.send_json({"type": "error", "message": error.detail})
                return True
            finally:
                conn.close()
            await manager.broadcast_many_users(members, {
                "type": "typing", "username": username,
                "group_id": str(group_id), "active": message_type == "typing",
            }, exclude_username=username)
        else:
            recipient = data.get("recipient")
            if not isinstance(recipient, str) or not recipient.strip() or recipient == username:
                await websocket.send_json({"type": "error", "message": "A valid typing recipient is required"})
                return True
            recipient = recipient.strip()
            conn = get_db()
            try:
                accepted = conn.execute(
                    """SELECT 1 FROM contacts WHERE user_id = ? AND contact_id = ? AND status = 'accepted'
                       UNION SELECT 1 FROM contacts WHERE user_id = ? AND contact_id = ? AND status = 'accepted'
                       LIMIT 1""",
                    (username, recipient, recipient, username),
                ).fetchone()
                blocked = conn.execute(
                    """SELECT 1 FROM user_blocks WHERE
                       (blocker_username = ? AND blocked_username = ?) OR
                       (blocker_username = ? AND blocked_username = ?) LIMIT 1""",
                    (username, recipient, recipient, username),
                ).fetchone()
            finally:
                conn.close()
            if not accepted or blocked:
                await websocket.send_json({"type": "error", "message": "Typing updates are not allowed for this conversation"})
                return True
            await manager.broadcast_user(recipient, {
                "type": "typing", "username": username,
                "recipient": recipient, "active": message_type == "typing",
            })
        return True

    if message_type == "edit_message":
        msg_id = data.get("msg_id")
        if not isinstance(msg_id, str) or not msg_id.strip():
            await websocket.send_json({"type": "error", "message": "A valid message ID is required"})
            return True
        try:
            await _edit_message(
                msg_id.strip(), username, data.get("content", ""),
                data.get("integrity_hash"), manager,
            )
        except HTTPException as error:
            await websocket.send_json({"type": "error", "msg_id": msg_id, "message": error.detail})
        return True

    if message_type == "delete_message":
        msg_id = data.get("msg_id")
        scope = data.get("scope", "me")
        if not isinstance(msg_id, str) or not msg_id.strip():
            await websocket.send_json({"type": "error", "message": "A valid message ID is required"})
            return True
        try:
            await _delete_message(msg_id.strip(), username, scope, manager, websocket=websocket)
            if scope == "me":
                # Echo to this device; other devices receive the manager broadcast.
                await websocket.send_json({
                    "type": "message_delete", "msg_id": msg_id.strip(), "scope": "me",
                    "deleted_by": username,
                })
        except HTTPException as error:
            await websocket.send_json({"type": "error", "msg_id": msg_id, "message": error.detail})
        return True

    return False
