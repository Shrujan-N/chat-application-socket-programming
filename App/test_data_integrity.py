"""Regression tests for Data Integrity (Issue #45)."""
from pathlib import Path
import os
import shutil
import subprocess
import sys
import textwrap

APP_DIR = Path(__file__).resolve().parent
if str(APP_DIR) not in sys.path:
    sys.path.insert(0, str(APP_DIR))

from message_integrity import content_sha256, verify_content_sha256


def test_content_checksum_is_deterministic_for_exact_text():
    assert content_sha256("hello") == content_sha256("hello")
    assert content_sha256("hello") != content_sha256("hello ")


def test_checksum_detects_changed_message_content():
    digest = content_sha256("original message")
    assert verify_content_sha256("original message", digest)
    assert not verify_content_sha256("changed message", digest)


def test_checksum_rejects_missing_or_malformed_digest():
    assert not verify_content_sha256("hello", None)
    assert not verify_content_sha256("hello", "not-a-sha256")
    assert not verify_content_sha256("hello", "0" * 63)


def test_checksum_supports_unicode_content():
    content = "Hello 👋 नमस्ते café"
    assert verify_content_sha256(content, content_sha256(content))
    assert not verify_content_sha256(content + "!", content_sha256(content))


def test_application_wires_hash_validation_and_ordering():
    main_source = (APP_DIR / "main.py").read_text(encoding="utf-8")
    web_db_source = (APP_DIR / "web_database.py").read_text(encoding="utf-8")
    browser_source = (APP_DIR / "static" / "index.html").read_text(encoding="utf-8")
    assert "integrity_hash" in main_source
    assert "assert_message_integrity(rows)" in main_source
    assert "get_recipient_delivery_lock(recipient)" in main_source
    assert '"integrity_hash": "TEXT"' in web_db_source
    assert "hashMessageContent(payload.content)" in browser_source
    assert "integrity_hash: message.integrity_hash" in browser_source
    assert '"integrity_hash": content_sha256(content)' in main_source
    assert "handleIncomingSignalVerified(data)" in browser_source
    assert "await hashMessageContent(data.content) === data.integrity_hash" in browser_source
    assert "SELECT rowid AS id FROM messages WHERE msg_id = ?" in main_source
    assert "COALESCE(MAX(rowid), 0)" in main_source
    assert "WHERE recipient = ? AND rowid > ?" in main_source
    assert "ORDER BY rowid ASC" in main_source


def test_websocket_integrity_and_ordered_sync_in_isolated_app(tmp_path):
    """Run an end-to-end smoke test in a copied App with disposable databases."""
    isolated_app = tmp_path / "App"
    shutil.copytree(
        APP_DIR,
        isolated_app,
        ignore=shutil.ignore_patterns("__pycache__", "chat_app.db", "uploads", "logs"),
    )

    smoke = r"""
import hashlib
from fastapi.testclient import TestClient
import main, web_database
from message_integrity import content_sha256
from fastapi import HTTPException

client = TestClient(main.app)
def register(username, email):
    response = client.post('/api/register', json={
        'username': username, 'email': email, 'password': 'ValidPass123'
    })
    assert response.status_code == 200, response.text
    login = client.post('/api/login', json={
        'username': username, 'password': 'ValidPass123'
    })
    assert login.status_code == 200, login.text
    return login.json()['token']

sender_token = register('integrity_sender', 'integrity_sender@example.com')
recipient_token = register('integrity_receiver', 'integrity_receiver@example.com')
content = 'hello integrity 👋'
digest = content_sha256(content)
with client.websocket_connect(f'/ws/integrity_sender?token={sender_token}') as sender, \
     client.websocket_connect(f'/ws/integrity_receiver?token={recipient_token}') as receiver:
    sender.send_json({
        'type':'send_message', 'msg_id':'integrity-e2e-1',
        'recipient':'integrity_receiver', 'content':content,
        'timestamp':'12:30', 'integrity_hash':digest,
    })
    assert sender.receive_json()['status'] == 'sent'
    received = receiver.receive_json()
    assert received['type'] == 'receive_message'
    assert received['content'] == content and received['integrity_hash'] == digest
    assert sender.receive_json()['status'] == 'delivered'

    sender.send_json({
        'type':'send_message', 'msg_id':'integrity-e2e-bad',
        'recipient':'integrity_receiver', 'content':content,
        'timestamp':'12:31', 'integrity_hash':hashlib.sha256(b'wrong').hexdigest(),
    })
    rejected = sender.receive_json()
    assert rejected['type'] == 'error' and 'integrity' in rejected['message'].lower()

    changed = 'tampered duplicate'
    sender.send_json({
        'type':'send_message', 'msg_id':'integrity-e2e-1',
        'recipient':'integrity_receiver', 'content':changed,
        'timestamp':'12:30', 'integrity_hash':content_sha256(changed),
    })
    assert sender.receive_json()['type'] == 'error'

sync = client.get('/api/messages/sync?since=0', headers={
    'Authorization': f'Bearer {recipient_token}'
})
assert sync.status_code == 200, sync.text
body = sync.json()
assert any(m['msg_id'] == 'integrity-e2e-1' for m in body['messages']), body
history = client.get('/api/messages/history/integrity_sender/integrity_receiver', headers={
    'Authorization': f'Bearer {recipient_token}'
})
assert history.status_code == 200, history.text
assert any(m['msg_id'] == 'integrity-e2e-1' for m in history.json()['history'])

conn = web_database.get_db()
try:
    conn.execute('UPDATE messages SET content=? WHERE msg_id=?', ('corrupted', 'integrity-e2e-1'))
    conn.commit()
finally:
    conn.close()
corrupt = client.get('/api/messages/history/integrity_sender/integrity_receiver', headers={
    'Authorization': f'Bearer {recipient_token}'
})
assert corrupt.status_code == 500, corrupt.text
print('PASS: checksum persistence, authenticated send/receive, and delivery status')
print('PASS: mismatched checksum and conflicting duplicate IDs are rejected')
print('PASS: ordered missed-message sync and history work with a separate web database')
print('PASS: corrupt stored content is detected instead of returned')
"""
    env = os.environ.copy()
    env["CHAT_DB_PATH"] = str(tmp_path / "core.db")
    env["CHAT_WEB_DB_PATH"] = str(tmp_path / "web.db")
    env["PYTHONPATH"] = str(isolated_app)
    result = subprocess.run(
        [sys.executable, "-c", textwrap.dedent(smoke)],
        cwd=isolated_app,
        env=env,
        text=True,
        capture_output=True,
        timeout=60,
    )
    assert result.returncode == 0, result.stdout + "\n" + result.stderr
    assert "PASS: checksum persistence" in result.stdout, result.stdout
    assert "PASS: mismatched checksum" in result.stdout, result.stdout
    assert "PASS: ordered missed-message sync" in result.stdout, result.stdout
    assert "PASS: corrupt stored content" in result.stdout, result.stdout
