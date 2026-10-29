"""Voice and Video Call Service (Issue #39, FR-28).

Manages WebRTC signaling sessions, active call states, and durable call history
in SQLite for one-to-one and group voice/video calls.
"""

from datetime import datetime, timezone
from typing import Dict, List, Optional
import sqlite3
import time


class CallSession:
    def __init__(
        self,
        call_id: str,
        caller: str,
        recipient: Optional[str] = None,
        group_id: Optional[str] = None,
        media_type: str = "voice",
    ):
        self.call_id = call_id
        self.caller = caller
        self.recipient = recipient
        self.group_id = group_id
        self.media_type = media_type if media_type in ("voice", "video") else "voice"
        self.status = "ringing"  # ringing -> active -> completed/declined/missed
        self.created_at_ts = time.time()
        self.connected_at_ts: Optional[float] = None
        self.participants = {caller}

    def to_dict(self) -> dict:
        duration = 0
        if self.connected_at_ts:
            duration = int(time.time() - self.connected_at_ts)
        return {
            "call_id": self.call_id,
            "caller": self.caller,
            "recipient": self.recipient,
            "group_id": self.group_id,
            "media_type": self.media_type,
            "status": self.status,
            "duration_seconds": duration,
            "participants": list(self.participants),
        }


# In-memory registry of ongoing/ringing calls
_active_calls: Dict[str, CallSession] = {}


def create_call(
    conn: sqlite3.Connection,
    call_id: str,
    caller: str,
    recipient: Optional[str] = None,
    group_id: Optional[str] = None,
    media_type: str = "voice",
) -> CallSession:
    """Register a new ringing call in memory and database."""
    session = CallSession(
        call_id=call_id,
        caller=caller,
        recipient=recipient,
        group_id=group_id,
        media_type=media_type,
    )
    _active_calls[call_id] = session

    cursor = conn.cursor()
    cursor.execute(
        """
        INSERT OR REPLACE INTO call_records (
            call_id, caller, recipient, group_id, media_type, status, started_at
        ) VALUES (?, ?, ?, ?, ?, 'ringing', CURRENT_TIMESTAMP)
        """,
        (call_id, caller, recipient, group_id, session.media_type),
    )
    conn.commit()
    return session


def get_active_call(call_id: str) -> Optional[CallSession]:
    """Retrieve an ongoing active call by ID."""
    return _active_calls.get(call_id)


def is_user_in_active_call(username: str) -> bool:
    """Check if the user is currently connected in an active call."""
    for call in _active_calls.values():
        if call.status == "active" and username in call.participants:
            return True
    return False


def accept_call(conn: sqlite3.Connection, call_id: str, user: str) -> Optional[CallSession]:
    """Mark call as active and record connection start."""
    session = _active_calls.get(call_id)
    if not session:
        return None

    session.status = "active"
    session.participants.add(user)
    if session.connected_at_ts is None:
        session.connected_at_ts = time.time()

    cursor = conn.cursor()
    cursor.execute(
        "UPDATE call_records SET status = 'active' WHERE call_id = ?",
        (call_id,),
    )
    conn.commit()
    return session


def end_call(
    conn: sqlite3.Connection,
    call_id: str,
    status: str = "completed",
) -> Optional[dict]:
    """Terminate call session, compute duration, update database, and clean up.

    Enforces FR-28 fail criteria:
    Declined or unanswered calls end cleanly without leaving orphan states.
    """
    session = _active_calls.pop(call_id, None)

    duration = 0
    now_iso = datetime.now(timezone.utc).isoformat()

    if session and session.connected_at_ts:
        duration = max(0, int(time.time() - session.connected_at_ts))
        if status == "declined":
            status = "completed"
    elif status not in ("declined", "missed", "busy"):
        status = "missed"

    cursor = conn.cursor()
    cursor.execute(
        """
        UPDATE call_records
        SET status = ?, ended_at = ?, duration_seconds = ?
        WHERE call_id = ?
        """,
        (status, now_iso, duration, call_id),
    )
    conn.commit()

    if session:
        session.status = status
        d = session.to_dict()
        d["duration_seconds"] = duration
        return d

    return {
        "call_id": call_id,
        "status": status,
        "duration_seconds": duration,
    }


def get_call_history(conn: sqlite3.Connection, username: str, limit: int = 50) -> List[dict]:
    """Fetch call records where the user participated (as caller, recipient, or group member)."""
    cursor = conn.cursor()
    cursor.execute(
        """
        SELECT id, call_id, caller, recipient, group_id, media_type,
               status, started_at, ended_at, duration_seconds
        FROM call_records
        WHERE caller = ?
           OR recipient = ?
           OR group_id IN (SELECT group_id FROM group_members WHERE username = ?)
        ORDER BY id DESC
        LIMIT ?
        """,
        (username, username, username, limit),
    )
    rows = cursor.fetchall()
    return [dict(row) for row in rows]
