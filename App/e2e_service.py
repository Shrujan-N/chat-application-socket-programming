"""Basic End-to-End Encryption (E2E) public-key directory helpers (NFR-04, Issue #44).

The server never generates private keys, derives shared secrets, or decrypts
message content. It only stores each user's public ECDH key (JWK) and relays
encrypted ciphertext (`e2e:dh:...` or `e2e:aes:...`) between clients.
"""

from __future__ import annotations

from datetime import datetime, timezone
import json
from typing import Optional

from web_database import get_db


E2E_DH_PREFIX = "e2e:dh:"
E2E_AES_PREFIX = "e2e:aes:"
MAX_PUBLIC_KEY_LENGTH = 2048
ENCRYPTED_MENTION_PREVIEW = "🔒 Encrypted message"


def is_encrypted_content(content: object) -> bool:
    """Return True when `content` carries a client-encrypted E2E ciphertext envelope."""
    return isinstance(content, str) and (
        content.startswith(E2E_DH_PREFIX) or content.startswith(E2E_AES_PREFIX)
    )


def validate_public_key_jwk(raw_key: object) -> str:
    """Validate a browser-exported P-256 ECDH public JWK and reject private keys."""
    if not isinstance(raw_key, str) or not raw_key.strip():
        raise ValueError("Public key is required.")
    if len(raw_key) > MAX_PUBLIC_KEY_LENGTH:
        raise ValueError("Public key exceeds maximum allowed size.")

    try:
        jwk = json.loads(raw_key)
    except ValueError as exc:
        raise ValueError("Public key must be valid JSON.") from exc

    if not isinstance(jwk, dict):
        raise ValueError("Public key must be a JWK JSON object.")
    if "d" in jwk:
        raise ValueError("Private key material ('d') must never be uploaded to the server.")
    if jwk.get("kty") != "EC" or jwk.get("crv") != "P-256":
        raise ValueError("Public key must use EC P-256.")
    if not isinstance(jwk.get("x"), str) or not jwk["x"].strip():
        raise ValueError("Public key is missing EC coordinate 'x'.")
    if not isinstance(jwk.get("y"), str) or not jwk["y"].strip():
        raise ValueError("Public key is missing EC coordinate 'y'.")

    normalized = {
        "kty": "EC",
        "crv": "P-256",
        "x": jwk["x"].strip(),
        "y": jwk["y"].strip(),
        "ext": True,
    }
    return json.dumps(normalized, separators=(",", ":"))


def save_public_key(username: str, raw_public_key: object) -> dict[str, str]:
    """Store or update the authenticated user's public ECDH key."""
    normalized_key = validate_public_key_jwk(raw_public_key)
    updated_at = datetime.now(timezone.utc).isoformat()
    conn = get_db()
    try:
        conn.execute(
            """
            INSERT INTO user_e2e_keys (username, public_key, updated_at)
            VALUES (?, ?, ?)
            ON CONFLICT(username) DO UPDATE SET
                public_key = excluded.public_key,
                updated_at = excluded.updated_at
            """,
            (username, normalized_key, updated_at),
        )
        conn.commit()
    finally:
        conn.close()
    return {
        "username": username,
        "public_key": normalized_key,
        "updated_at": updated_at,
    }


def get_public_key(username: str) -> Optional[dict[str, str]]:
    """Fetch a user's stored public ECDH key, or None if not registered yet."""
    conn = get_db()
    try:
        row = conn.execute(
            "SELECT username, public_key, updated_at FROM user_e2e_keys WHERE username = ?",
            (username,),
        ).fetchone()
        return dict(row) if row is not None else None
    finally:
        conn.close()


def filter_client_mentions(
    raw_mentions: object,
    group_members: list[str],
    *,
    sender: str,
) -> list[str]:
    """Keep only valid group members (excluding sender) for encrypted group messages."""
    if not isinstance(raw_mentions, list):
        return []
    canonical_members = {member.lower(): member for member in group_members}
    sender_lower = (sender or "").lower()
    resolved: list[str] = []
    seen: set[str] = set()
    for item in raw_mentions:
        if not isinstance(item, str):
            continue
        key = item.strip().lower()
        if not key or key == sender_lower or key in seen:
            continue
        canonical = canonical_members.get(key)
        if canonical is not None:
            seen.add(key)
            resolved.append(canonical)
    return resolved
