import os
 
from security import hash_password, verify_password
 
DB_PATH = os.environ.get(
    "CHAT_DB_PATH",
    os.path.join(os.path.dirname(os.path.abspath(__file__)), "chat_app.db"),
)
 
import sqlite3
from pathlib import Path

DB_PATH = Path(os.environ.get("CHAT_DB_PATH", Path(__file__).resolve().parent / "chat_app.db")).resolve()

def get_db():
    conn = sqlite3.connect(DB_PATH, timeout=10)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys=ON")
    return conn
 
def init_db():
    conn = get_db()
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
 
    with get_db() as conn:
        conn.executescript('''
        CREATE TABLE IF NOT EXISTS users(username TEXT PRIMARY KEY, email TEXT UNIQUE, password TEXT);
        CREATE TABLE IF NOT EXISTS contacts(
          id INTEGER PRIMARY KEY AUTOINCREMENT, user_id TEXT NOT NULL,
          contact_id TEXT NOT NULL, status TEXT NOT NULL DEFAULT 'pending',
          UNIQUE(user_id,contact_id));
        CREATE TABLE IF NOT EXISTS groups(
          group_id TEXT PRIMARY KEY, name TEXT NOT NULL, icon TEXT NOT NULL DEFAULT '👥',
          creator TEXT NOT NULL, created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP);
        CREATE TABLE IF NOT EXISTS group_members(
          group_id TEXT NOT NULL, username TEXT NOT NULL, role TEXT NOT NULL DEFAULT 'member',
          joined_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP, PRIMARY KEY(group_id,username),
          FOREIGN KEY(group_id) REFERENCES groups(group_id) ON DELETE CASCADE);
        CREATE TABLE IF NOT EXISTS messages(
          msg_id TEXT PRIMARY KEY, conversation_type TEXT NOT NULL DEFAULT 'private',
          conversation_id TEXT NOT NULL DEFAULT '', sender TEXT NOT NULL, recipient TEXT,
          group_id TEXT, content TEXT NOT NULL, timestamp TEXT NOT NULL,
          status TEXT NOT NULL DEFAULT 'sent', edited INTEGER NOT NULL DEFAULT 0,
          edited_at TEXT, deleted_everyone INTEGER NOT NULL DEFAULT 0);
        CREATE TABLE IF NOT EXISTS message_receipts(
          msg_id TEXT NOT NULL, username TEXT NOT NULL, delivered_at TEXT, read_at TEXT,
          PRIMARY KEY(msg_id,username), FOREIGN KEY(msg_id) REFERENCES messages(msg_id) ON DELETE CASCADE);
        CREATE TABLE IF NOT EXISTS user_message_deletions(
          msg_id TEXT NOT NULL, username TEXT NOT NULL, deleted_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
          PRIMARY KEY(msg_id,username), FOREIGN KEY(msg_id) REFERENCES messages(msg_id) ON DELETE CASCADE);
        CREATE INDEX IF NOT EXISTS idx_messages_conversation ON messages(conversation_type,conversation_id,timestamp);
        ''')
        columns = {r["name"] for r in conn.execute("PRAGMA table_info(messages)")}
        migrations = {
          "conversation_type": "TEXT NOT NULL DEFAULT 'private'",
          "conversation_id": "TEXT NOT NULL DEFAULT ''",
          "group_id": "TEXT", "edited": "INTEGER NOT NULL DEFAULT 0",
          "edited_at": "TEXT", "deleted_everyone": "INTEGER NOT NULL DEFAULT 0"
        }
        for name, definition in migrations.items():
            if name not in columns:
                conn.execute(f"ALTER TABLE messages ADD COLUMN {name} {definition}")
        for username, email in [
          ("shrujan","shrujan@example.com"),
          ("testuser01","user01@example.com"),
          ("testuser02","user02@example.com")
        ]:
            conn.execute("INSERT OR IGNORE INTO users(username,email,password) VALUES(?,?,?)",
                         (username,email,"demo-only"))
        old = conn.execute("SELECT msg_id,sender,recipient FROM messages WHERE conversation_id='' OR conversation_id IS NULL").fetchall()
        for row in old:
            cid = "private:" + "|".join(sorted([row["sender"], row["recipient"] or ""]))
            conn.execute("UPDATE messages SET conversation_id=? WHERE msg_id=?", (cid,row["msg_id"]))
