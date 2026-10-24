import sqlite3
import os
 
from security import hash_password, verify_password
 
DB_PATH = os.environ.get(
    "CHAT_DB_PATH",
    os.path.join(os.path.dirname(os.path.abspath(__file__)), "chat_app.db"),
)
 
def get_db():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
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