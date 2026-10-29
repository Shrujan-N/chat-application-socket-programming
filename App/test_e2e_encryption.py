"""Automated tests for Basic End-to-End Encryption (NFR-04, Issue #44)."""

from __future__ import annotations

import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import uuid

_TMP_DIR = tempfile.mkdtemp(prefix="chat_e2e_test_")
os.environ.setdefault("CHAT_DB_PATH", os.path.join(_TMP_DIR, "test_e2e.db"))
os.environ.setdefault("JWT_SECRET", "test-e2e-secret")

APP_DIR = Path(__file__).resolve().parent
if str(APP_DIR) not in sys.path:
    sys.path.insert(0, str(APP_DIR))

from fastapi.testclient import TestClient

import e2e_service
import main
from message_integrity import content_sha256
import push_service
from web_database import get_db


client = TestClient(main.app)

SAMPLE_ALICE_PUBLIC_JWK = json.dumps({
    "kty": "EC",
    "crv": "P-256",
    "x": "f83OJ3D2xF1Bg8vub9tLe1gHMzV76e8Tus9uPHvRVEU",
    "y": "x_FEzRu9m36HLN_tue659LNpXW6pCyStikYjKIWI5a0",
    "ext": True,
})


def _make_user(prefix: str = "e2e_u") -> tuple[str, dict[str, str], str]:
    username = f"{prefix}_{uuid.uuid4().hex[:8]}"
    reg = client.post(
        "/api/register",
        json={
            "username": username,
            "email": f"{username}@example.com",
            "password": "ValidPass123",
        },
    )
    assert reg.status_code == 200, reg.text
    login = client.post(
        "/api/login",
        json={"username": username, "password": "ValidPass123"},
    )
    assert login.status_code == 200, login.text
    token = login.json()["token"]
    return username, {"Authorization": f"Bearer {token}"}, token


def test_e2e_key_endpoints_require_authentication():
    assert client.post("/api/e2e/keys", json={"public_key": SAMPLE_ALICE_PUBLIC_JWK}).status_code == 401
    assert client.get("/api/e2e/keys/someuser").status_code == 401


def test_public_key_upload_retrieval_and_private_key_rejection():
    alice, alice_headers, _ = _make_user("alice")
    bob, bob_headers, _ = _make_user("bob")

    # Before upload, has_key is False
    missing = client.get(f"/api/e2e/keys/{alice}", headers=bob_headers)
    assert missing.status_code == 200
    assert missing.json()["has_key"] is False
    assert missing.json()["public_key"] is None

    # Uploading a valid ECDH P-256 public key succeeds
    saved = client.post(
        "/api/e2e/keys",
        headers=alice_headers,
        json={"public_key": SAMPLE_ALICE_PUBLIC_JWK},
    )
    assert saved.status_code == 200, saved.text
    assert saved.json()["success"] is True

    # Bob can fetch Alice's public key
    fetched = client.get(f"/api/e2e/keys/{alice}", headers=bob_headers)
    assert fetched.status_code == 200
    body = fetched.json()
    assert body["has_key"] is True
    parsed = json.loads(body["public_key"])
    assert parsed["kty"] == "EC" and parsed["crv"] == "P-256"
    assert "d" not in parsed

    # Private key material ('d') MUST be rejected by the server
    leaked_private = json.dumps({
        "kty": "EC",
        "crv": "P-256",
        "x": "f83OJ3D2xF1Bg8vub9tLe1gHMzV76e8Tus9uPHvRVEU",
        "y": "x_FEzRu9m36HLN_tue659LNpXW6pCyStikYjKIWI5a0",
        "d": "jpsQnnGQmL-YBIffH1136cspYG6-0iY7X1fCE9-E9LI",
    })
    rejected = client.post(
        "/api/e2e/keys",
        headers=alice_headers,
        json={"public_key": leaked_private},
    )
    assert rejected.status_code == 400
    assert "private key" in rejected.json()["detail"].lower()

    # Invalid curve / malformed JSON rejected
    bad_json = client.post("/api/e2e/keys", headers=alice_headers, json={"public_key": "not-json"})
    assert bad_json.status_code == 400


def test_server_stores_only_ciphertext_in_database_and_relays_intact():
    alice, alice_headers, alice_token = _make_user("enc_sender")
    bob, bob_headers, bob_token = _make_user("enc_receiver")

    secret_plaintext = "SuperSecretExamTopic12345"
    # Simulated client-encrypted envelope; plaintext is NOT inside this string
    ciphertext = "e2e:dh:AAECAwQFBgcICQoL:9v8xK2pL7mN4qR1tW6zY3bC0dF5gH8jM=="
    assert secret_plaintext not in ciphertext
    msg_id = "e2e-dm-" + uuid.uuid4().hex

    with client.websocket_connect(f"/ws/{alice}?token={alice_token}") as ws_a, \
         client.websocket_connect(f"/ws/{bob}?token={bob_token}") as ws_b:
        ws_a.send_json({
            "type": "send_message",
            "msg_id": msg_id,
            "sender": alice,
            "recipient": bob,
            "content": ciphertext,
            "timestamp": "07:30 PM",
            "integrity_hash": content_sha256(ciphertext),
        })
        sent_ack = ws_a.receive_json()
        incoming = ws_b.receive_json()

    assert sent_ack["status"] == "sent"
    assert incoming["type"] == "receive_message"
    assert incoming["content"] == ciphertext
    assert incoming["integrity_hash"] == content_sha256(ciphertext)

    # Inspect raw SQLite database: only ciphertext is stored
    conn = get_db()
    try:
        row = conn.execute(
            "SELECT content FROM messages WHERE msg_id = ?", (msg_id,)
        ).fetchone()
        assert row is not None
        assert row["content"] == ciphertext
        assert secret_plaintext not in row["content"]
    finally:
        conn.close()

    # History API returns exact ciphertext for client-side decryption
    hist = client.get(f"/api/messages/history/{alice}/{bob}", headers=bob_headers)
    assert hist.status_code == 200
    stored_item = next(m for m in hist.json()["history"] if m["msg_id"] == msg_id)
    assert stored_item["content"] == ciphertext


def test_encrypted_group_mentions_filter_members_and_hide_ciphertext_preview():
    alice, alice_headers, alice_token = _make_user("grp_enc_a")
    bob, bob_headers, bob_token = _make_user("grp_enc_b")
    outsider, _, _ = _make_user("grp_enc_out")

    created = client.post(
        "/api/groups",
        headers=alice_headers,
        json={"name": "E2E Study Group", "icon": "🔒", "members": [bob]},
    )
    assert created.status_code == 200, created.text
    group_id = created.json()["group"]["group_id"]

    ciphertext = "e2e:aes:MTIzNDU2Nzg5MDEy:c2VjcmV0LWdyb3VwLWNpcGhlcnRleHQ="
    msg_id = "e2e-grp-" + uuid.uuid4().hex

    with client.websocket_connect(f"/ws/{alice}?token={alice_token}") as ws_a, \
         client.websocket_connect(f"/ws/{bob}?token={bob_token}") as ws_b:
        ws_a.send_json({
            "type": "send_group_message",
            "msg_id": msg_id,
            "group_id": group_id,
            "content": ciphertext,
            "timestamp": "07:35 PM",
            "mentioned_users": [bob, outsider, alice],
            "integrity_hash": content_sha256(ciphertext),
        })
        assert ws_a.receive_json()["status"] == "sent"
        group_msg = ws_b.receive_json()
        mention_evt = ws_b.receive_json()

    # Outsider and self-mention are stripped; only Bob remains
    assert group_msg["mentioned_users"] == [bob]
    assert mention_evt["type"] == "mention_notification"
    assert mention_evt["content_preview"] == e2e_service.ENCRYPTED_MENTION_PREVIEW
    assert "e2e:aes:" not in mention_evt["content_preview"]

    pending = client.get("/api/mentions", headers=bob_headers).json()["notifications"]
    stored_notif = next(n for n in pending if n["msg_id"] == msg_id)
    assert stored_notif["content_preview"] == e2e_service.ENCRYPTED_MENTION_PREVIEW


def test_push_preview_masks_encrypted_ciphertext():
    assert push_service.make_preview("e2e:dh:abc:xyz") == "🔒 New encrypted message"
    assert push_service.make_preview("e2e:aes:abc:xyz") == "🔒 New encrypted message"
    assert push_service.make_preview("Normal legacy text") == "Normal legacy text"


def test_real_browser_e2e_js_ecdh_and_aes_gcm_roundtrip_via_node():
    node_bin = shutil.which("node")
    if not node_bin:
        return

    e2e_js_path = (APP_DIR / "static" / "e2e.js").as_posix()
    script = f"""
const fs = require('fs');
const vm = require('vm');
const {{ webcrypto }} = require('crypto');

const keyDirectory = new Map();

function makeBrowserContext(name) {{
    const storage = new Map();
    const ctx = {{
        crypto: webcrypto,
        TextEncoder,
        TextDecoder,
        Uint8Array,
        btoa: (str) => Buffer.from(str, 'binary').toString('base64'),
        atob: (b64) => Buffer.from(b64, 'base64').toString('binary'),
        console,
        localStorage: {{
            getItem: (k) => (storage.has(k) ? storage.get(k) : null),
            setItem: (k, v) => storage.set(k, String(v)),
            removeItem: (k) => storage.delete(k),
        }},
        fetch: async (url, opts = {{}}) => {{
            if (opts.method === 'POST' && url === '/api/e2e/keys') {{
                const body = JSON.parse(opts.body);
                keyDirectory.set(name, body.public_key);
                return {{ ok: true, json: async () => ({{ success: true }}) }};
            }}
            if (url.startsWith('/api/e2e/keys/')) {{
                const target = decodeURIComponent(url.split('/').pop());
                const pub = keyDirectory.get(target) || null;
                return {{
                    ok: true,
                    json: async () => ({{ username: target, has_key: Boolean(pub), public_key: pub }}),
                }};
            }}
            throw new Error('Unexpected fetch: ' + url);
        }},
    }};
    ctx.window = ctx;
    ctx.globalThis = ctx;
    vm.createContext(ctx);
    vm.runInContext(fs.readFileSync('{e2e_js_path}', 'utf8'), ctx);
    return ctx;
}}

(async () => {{
    const aliceBrowser = makeBrowserContext('alice');
    const bobBrowser = makeBrowserContext('bob');

    await aliceBrowser.E2E.init('alice');
    await bobBrowser.E2E.init('bob');

    // 1. Direct 1-to-1 ECDH + AES-GCM round-trip
    const secret = 'Hello Bob! E2E test 👋 12345';
    const encrypted = await aliceBrowser.E2E.encryptText(secret, 'bob', null);
    if (!encrypted.startsWith('e2e:dh:')) throw new Error('Expected ECDH prefix, got: ' + encrypted);
    if (encrypted.includes('Hello Bob')) throw new Error('Plaintext leaked in ciphertext!');

    const decryptedByBob = await bobBrowser.E2E.decryptText(encrypted, 'alice', null);
    if (decryptedByBob !== secret) throw new Error('Bob decrypted wrong text: ' + decryptedByBob);

    // Alice can also decrypt her own sent message in chat history
    const decryptedByAlice = await aliceBrowser.E2E.decryptText(encrypted, 'bob', null);
    if (decryptedByAlice !== secret) throw new Error('Alice history decrypt failed: ' + decryptedByAlice);

    // 2. Tampering with ciphertext is caught by AES-GCM authentication tag
    const tampered = encrypted.slice(0, -4) + 'AAAA';
    const tamperedResult = await bobBrowser.E2E.decryptText(tampered, 'alice', null);
    if (!tamperedResult.includes('unable to decrypt')) {{
        throw new Error('Tampered ciphertext was not rejected: ' + tamperedResult);
    }}

    // 3. Group chat AES-GCM round-trip
    const groupPlain = 'Group exam notes at 8pm!';
    const groupCipher = await aliceBrowser.E2E.encryptText(groupPlain, null, 'grp-demo-1');
    if (!groupCipher.startsWith('e2e:aes:')) throw new Error('Expected group AES prefix');
    const groupOut = await bobBrowser.E2E.decryptText(groupCipher, null, 'grp-demo-1');
    if (groupOut !== groupPlain) throw new Error('Group decrypt mismatch: ' + groupOut);

    console.log('NODE_WEBCRYPTO_E2E_OK');
}})().catch(err => {{
    console.error(err);
    process.exit(1);
}});
"""
    proc = subprocess.run([node_bin, "-e", script], capture_output=True, text=True, timeout=15)
    assert proc.returncode == 0, proc.stderr + proc.stdout
    assert "NODE_WEBCRYPTO_E2E_OK" in proc.stdout


def test_browser_wires_e2e_script_and_preserves_integrity_hooks():
    html = (APP_DIR / "static" / "index.html").read_text(encoding="utf-8")
    assert '<script src="/static/e2e.js"></script>' in html
    assert "E2E.init(currentUser)" in html
    assert "E2E.encryptText(" in html
    assert "E2E.decryptMessageObject(" in html
    # Ensure Issue #45 integrity hooks remain intact
    assert "hashMessageContent(payload.content)" in html
    assert "await hashMessageContent(data.content) === data.integrity_hash" in html


if __name__ == "__main__":
    tests = [
        test_e2e_key_endpoints_require_authentication,
        test_public_key_upload_retrieval_and_private_key_rejection,
        test_server_stores_only_ciphertext_in_database_and_relays_intact,
        test_encrypted_group_mentions_filter_members_and_hide_ciphertext_preview,
        test_push_preview_masks_encrypted_ciphertext,
        test_real_browser_e2e_js_ecdh_and_aes_gcm_roundtrip_via_node,
        test_browser_wires_e2e_script_and_preserves_integrity_hooks,
    ]
    print("Running App/test_e2e_encryption.py tests...")
    for fn in tests:
        fn()
        print(f"  [PASS] {fn.__name__}")
    print(f"\nALL {len(tests)} E2E ENCRYPTION TESTS PASSED!")
