import os
import sqlite3
from pathlib import Path

DB_PATH = Path(os.environ.get("CHAT_WEB_DB_PATH", os.environ.get("CHAT_DB_PATH", Path(__file__).resolve().parent / "chat_app.db"))).resolve()

def get_db():
    conn = sqlite3.connect(DB_PATH, timeout=10)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys=ON")
    return conn

def init_db():
    with get_db() as conn:
        conn.executescript('''
        CREATE TABLE IF NOT EXISTS users(username TEXT PRIMARY KEY, email TEXT UNIQUE, password TEXT);
        CREATE TABLE IF NOT EXISTS contacts(
          id INTEGER PRIMARY KEY AUTOINCREMENT, user_id TEXT NOT NULL,
          contact_id TEXT NOT NULL, status TEXT NOT NULL DEFAULT 'pending',
          UNIQUE(user_id,contact_id));
        CREATE TABLE IF NOT EXISTS groups(
          group_id TEXT PRIMARY KEY, name TEXT NOT NULL, icon TEXT NOT NULL DEFAULT '≡ƒæÑ',
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

        CREATE TABLE IF NOT EXISTS login_logs (
          id INTEGER PRIMARY KEY AUTOINCREMENT,
          username TEXT NOT NULL,
          event TEXT NOT NULL,
          success INTEGER NOT NULL DEFAULT 0,
          ip_address TEXT,
          timestamp TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS login_security (
          username TEXT PRIMARY KEY,
          failed_attempts INTEGER NOT NULL DEFAULT 0,
          locked_until TEXT
        );
        CREATE TABLE IF NOT EXISTS revoked_sessions (
          sid TEXT PRIMARY KEY,
          username TEXT NOT NULL,
          expires_at INTEGER NOT NULL
        );
        CREATE TABLE IF NOT EXISTS password_resets (
          token_hash TEXT PRIMARY KEY,
          username TEXT NOT NULL,
          created_at INTEGER NOT NULL,
          expires_at INTEGER NOT NULL,
          used INTEGER NOT NULL DEFAULT 0
        );
        CREATE TABLE IF NOT EXISTS password_changes (
          username TEXT PRIMARY KEY,
          changed_at_ms INTEGER NOT NULL
        );
        CREATE TABLE IF NOT EXISTS profiles (
          username TEXT PRIMARY KEY,
          display_name TEXT,
          bio TEXT NOT NULL DEFAULT '',
          avatar TEXT,
          updated_at TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS push_subscriptions (
          endpoint TEXT PRIMARY KEY,
          username TEXT NOT NULL,
          p256dh TEXT NOT NULL,
          auth TEXT NOT NULL,
          user_agent TEXT,
          created_at TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS muted_chats (
          username TEXT NOT NULL,
          muted_user TEXT NOT NULL,
          created_at TEXT NOT NULL,
          PRIMARY KEY (username, muted_user)
        );
        CREATE TABLE IF NOT EXISTS push_log (
          msg_id TEXT NOT NULL,
          endpoint TEXT NOT NULL,
          sent_at INTEGER NOT NULL,
          PRIMARY KEY (msg_id, endpoint)
        );

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
        conn.execute("CREATE INDEX IF NOT EXISTS idx_messages_conversation ON messages(conversation_type,conversation_id,timestamp)")
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
