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
    CREATE TABLE IF NOT EXISTS users (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        username TEXT UNIQUE NOT NULL,
        email TEXT UNIQUE NOT NULL,
        password TEXT NOT NULL
    );
    """)

    # Contacts & Friends Table (ADDED FOR ISSUE #17)
    cursor.execute("""
    CREATE TABLE IF NOT EXISTS contacts (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        user_id TEXT NOT NULL,
        contact_id TEXT NOT NULL,
        status TEXT DEFAULT 'accepted',
        UNIQUE(user_id, contact_id)
    );
    """)
    
    # Pre-seed initial users for testing if database is empty
    cursor.execute("SELECT COUNT(*) FROM users")
    if cursor.fetchone()[0] == 0:
        cursor.execute("INSERT INTO users (username, email, password) VALUES ('shrujan', 'shrujan@example.com', 'Pass1234')")
        cursor.execute("INSERT INTO users (username, email, password) VALUES ('testuser01', 'user01@example.com', 'Pass1234')")
        cursor.execute("INSERT INTO users (username, email, password) VALUES ('testuser02', 'user02@example.com', 'Pass1234')")
        
        # Seed default contact relationship (ADDED FOR ISSUE #17)
        cursor.execute("INSERT OR IGNORE INTO contacts (user_id, contact_id) VALUES ('shrujan', 'testuser01')")
        cursor.execute("INSERT OR IGNORE INTO contacts (user_id, contact_id) VALUES ('testuser01', 'shrujan')")
        cursor.execute("INSERT OR IGNORE INTO contacts (user_id, contact_id) VALUES ('shrujan', 'testuser02')")
        cursor.execute("INSERT OR IGNORE INTO contacts (user_id, contact_id) VALUES ('testuser02', 'shrujan')")
        conn.commit()
    
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