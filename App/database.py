"""Core SQLite connection handling and schema initialization."""

import os
import sqlite3
from pathlib import Path

from security import hash_password, verify_password


DB_PATH = Path(
    os.environ.get(
        "CHAT_DB_PATH",
        Path(__file__).resolve().parent / "chat_app.db",
    )
).resolve()


def get_db() -> sqlite3.Connection:
    conn = sqlite3.connect(DB_PATH, timeout=30)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys=ON")
    conn.execute("PRAGMA busy_timeout=10000")
    return conn
 
def init_db() -> None:
    conn = get_db()
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA synchronous=NORMAL")
    cursor = conn.cursor()
 
    cursor.execute("""
    CREATE TABLE IF NOT EXISTS users (
        username TEXT PRIMARY KEY,
        email TEXT UNIQUE NOT NULL,
        password TEXT NOT NULL
    )
    """)
    
    # Core User Table
    cursor.execute("""
    CREATE TABLE IF NOT EXISTS contacts (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        user_id TEXT NOT NULL,
        contact_id TEXT NOT NULL,
        status TEXT DEFAULT 'accepted',
        UNIQUE(user_id, contact_id)
    );
    """)
 
    # Message Persistence Table (ADDED FOR ISSUE #23)
    cursor.execute("""
    CREATE TABLE IF NOT EXISTS messages (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        msg_id TEXT UNIQUE NOT NULL,
        sender TEXT NOT NULL,
        recipient TEXT NOT NULL,
        content TEXT NOT NULL,
        timestamp TEXT NOT NULL,
        status TEXT DEFAULT 'sent'
    );
    """)
 
    # Login audit trail (IT-AUTH-01: login event logged with timestamp)
    cursor.execute("""
    CREATE TABLE IF NOT EXISTS login_logs (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        username TEXT NOT NULL,
        event TEXT NOT NULL,
        success INTEGER NOT NULL DEFAULT 0,
        ip_address TEXT,
        timestamp TEXT NOT NULL
    );
    """)
 
    # Brute-force protection state (ST-AUTH-01: lock after 5 failures)
    cursor.execute("""
    CREATE TABLE IF NOT EXISTS login_security (
        username TEXT PRIMARY KEY,
        failed_attempts INTEGER NOT NULL DEFAULT 0,
        locked_until TEXT
    );
    """)
    
    # Revoked (logged-out) sessions, so a saved token cannot be reused (FR-03)
    cursor.execute("""
    CREATE TABLE IF NOT EXISTS revoked_sessions (
        sid TEXT PRIMARY KEY,
        username TEXT NOT NULL,
        expires_at INTEGER NOT NULL
    );
    """)
    
    # Password recovery (FR-04). Only a SHA-256 hash of each reset token is stored.
    cursor.execute("""
    CREATE TABLE IF NOT EXISTS password_resets (
        token_hash TEXT PRIMARY KEY,
        username TEXT NOT NULL,
        created_at INTEGER NOT NULL,
        expires_at INTEGER NOT NULL,
        used INTEGER NOT NULL DEFAULT 0
    );
    """)

    # When a password was last changed by a reset: sessions issued before that
    # moment are no longer accepted.
    cursor.execute("""
    CREATE TABLE IF NOT EXISTS password_changes (
        username TEXT PRIMARY KEY,
        changed_at_ms INTEGER NOT NULL
    );
    """)
    
    # User profiles (FR-05): display name, status/bio and profile picture file name.
    # A user with no row here simply shows their username and no picture.
    cursor.execute("""
    CREATE TABLE IF NOT EXISTS profiles (
        username TEXT PRIMARY KEY,
        display_name TEXT,
        bio TEXT NOT NULL DEFAULT '',
        avatar TEXT,
        updated_at TEXT NOT NULL
    );
    """)

    # Push notifications (FR-19): one row per subscribed browser/device.
    cursor.execute("""
    CREATE TABLE IF NOT EXISTS push_subscriptions (
        endpoint TEXT PRIMARY KEY,
        username TEXT NOT NULL,
        p256dh TEXT NOT NULL,
        auth TEXT NOT NULL,
        user_agent TEXT,
        created_at TEXT NOT NULL
    );
    """)
    cursor.execute("CREATE INDEX IF NOT EXISTS idx_push_subscriptions_user ON push_subscriptions(username)")
    cursor.execute("CREATE INDEX IF NOT EXISTS idx_contacts_user_status ON contacts(user_id, status)")
    cursor.execute("CREATE INDEX IF NOT EXISTS idx_contacts_contact_status ON contacts(contact_id, status)")
    cursor.execute("CREATE INDEX IF NOT EXISTS idx_messages_recipient_status ON messages(recipient, status)")
    cursor.execute("CREATE INDEX IF NOT EXISTS idx_messages_sender_recipient ON messages(sender, recipient)")
    cursor.execute("CREATE INDEX IF NOT EXISTS idx_revoked_sessions_expires ON revoked_sessions(expires_at)")

    # Muted chats/contacts: no push notification for messages from `muted_user`.
    cursor.execute("""
    CREATE TABLE IF NOT EXISTS muted_chats (
        username TEXT NOT NULL,
        muted_user TEXT NOT NULL,
        created_at TEXT NOT NULL,
        PRIMARY KEY (username, muted_user)
    );
    """)

    # Which message was already pushed to which device (prevents duplicate notifications).
    cursor.execute("""
    CREATE TABLE IF NOT EXISTS push_log (
        msg_id TEXT NOT NULL,
        endpoint TEXT NOT NULL,
        sent_at INTEGER NOT NULL,
        PRIMARY KEY (msg_id, endpoint)
    );
    """)

    # Pre-seed initial users for testing if database is empty
    cursor.execute("SELECT COUNT(*) FROM users")
    if cursor.fetchone()[0] == 0:
        seeds = [
            ("shrujan", "shrujan@example.com", "Pass1234"),
            ("testuser01", "user01@example.com", "Test@1234"),
            ("testuser02", "user02@example.com", "Pass1234"),
        ]
        for username, email, password in seeds:
            cursor.execute(
                "INSERT INTO users (username, email, password) VALUES (?, ?, ?)",
                (username, email, hash_password(password))
            )
        conn.commit()
    else:
        # Existing database: align testuser01 with the test-case credentials
        # (testuser01 / Test@1234) if it still has the old seed password.
        cursor.execute("SELECT password FROM users WHERE username = 'testuser01'")
        row = cursor.fetchone()
        if row and verify_password("Pass1234", row["password"]):
            cursor.execute(
                "UPDATE users SET password = ? WHERE username = 'testuser01'",
                (hash_password("Test@1234"),)
            )
            conn.commit()
        
    conn.close()
 
if __name__ == "__main__":
    init_db()
