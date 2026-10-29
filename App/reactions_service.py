"""Message Reactions Service (Issue #34, FR-23).

Handles adding and toggling emoji reactions for messages in 1-to-1 and group chats,
ensuring real-time updates and persistence in SQLite.
"""

from typing import Dict, List, Optional
import sqlite3

# Default quick reaction emojis
STANDARD_EMOJIS = ["👍", "❤️", "😂", "😮", "😢", "🙏"]


def format_reactions_rows(rows: List[sqlite3.Row]) -> List[Dict]:
    """Group rows by emoji into a list of {emoji, count, users}."""
    grouped: Dict[str, List[str]] = {}
    for row in rows:
        emoji = row["emoji"]
        username = row["username"]
        if emoji not in grouped:
            grouped[emoji] = []
        if username not in grouped[emoji]:
            grouped[emoji].append(username)
    return [
        {"emoji": emoji, "count": len(users), "users": users}
        for emoji, users in grouped.items()
    ]


def get_reactions_for_message(conn: sqlite3.Connection, msg_id: str) -> List[Dict]:
    """Get aggregated reactions for a single message."""
    cursor = conn.cursor()
    cursor.execute(
        "SELECT emoji, username FROM message_reactions WHERE msg_id = ? ORDER BY id ASC",
        (msg_id,),
    )
    return format_reactions_rows(cursor.fetchall())


def get_reactions_for_messages(conn: sqlite3.Connection, msg_ids: List[str]) -> Dict[str, List[Dict]]:
    """Batch-fetch reactions for multiple messages to avoid N+1 database queries."""
    if not msg_ids:
        return {}
    
    unique_ids = list(dict.fromkeys(msg_ids))
    placeholders = ",".join("?" for _ in unique_ids)
    cursor = conn.cursor()
    cursor.execute(
        f"SELECT msg_id, emoji, username FROM message_reactions WHERE msg_id IN ({placeholders}) ORDER BY id ASC",
        unique_ids,
    )
    grouped: Dict[str, Dict[str, List[str]]] = {mid: {} for mid in unique_ids}
    for row in cursor.fetchall():
        mid = row["msg_id"]
        emoji = row["emoji"]
        username = row["username"]
        if emoji not in grouped[mid]:
            grouped[mid][emoji] = []
        if username not in grouped[mid][emoji]:
            grouped[mid][emoji].append(username)

    return {
        mid: [
            {"emoji": emoji, "count": len(users), "users": users}
            for emoji, users in emojis.items()
        ]
        for mid, emojis in grouped.items()
    }


def toggle_message_reaction(conn: sqlite3.Connection, msg_id: str, username: str, emoji: str) -> Dict:
    """Add a reaction if not present, or remove (toggle off) if already present.

    Enforces FR-23 acceptance criteria:
    Selecting the same reaction a second time toggles it off rather than stacking duplicates.
    """
    cursor = conn.cursor()
    # Check if this reaction already exists for this user on this message
    cursor.execute(
        "SELECT id FROM message_reactions WHERE msg_id = ? AND username = ? AND emoji = ?",
        (msg_id, username, emoji),
    )
    existing = cursor.fetchone()

    if existing:
        cursor.execute(
            "DELETE FROM message_reactions WHERE msg_id = ? AND username = ? AND emoji = ?",
            (msg_id, username, emoji),
        )
        action = "removed"
    else:
        cursor.execute(
            "INSERT INTO message_reactions (msg_id, username, emoji) VALUES (?, ?, ?)",
            (msg_id, username, emoji),
        )
        action = "added"

    conn.commit()
    reactions = get_reactions_for_message(conn, msg_id)
    return {
        "success": True,
        "action": action,
        "msg_id": msg_id,
        "username": username,
        "emoji": emoji,
        "reactions": reactions,
    }
