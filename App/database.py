import sqlite3
import os

DB_PATH = "chat_app.db"

def get_db():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn

def init_db():
    conn = get_db()
    cursor = conn.cursor()
    
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
    
    # Pre-seed initial users for testing if database is empty
    cursor.execute("SELECT COUNT(*) FROM users")
    if cursor.fetchone()[0] == 0:
        cursor.execute("INSERT INTO users (username, email, password) VALUES ('shrujan', 'shrujan@example.com', 'Pass1234')")
        cursor.execute("INSERT INTO users (username, email, password) VALUES ('testuser01', 'user01@example.com', 'Pass1234')")
        cursor.execute("INSERT INTO users (username, email, password) VALUES ('testuser02', 'user02@example.com', 'Pass1234')")
        conn.commit()
        
    conn.close()

if __name__ == "__main__":
    init_db()