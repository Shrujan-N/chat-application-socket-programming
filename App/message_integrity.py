"""Small, shared helpers for checking chat-message content integrity."""
from __future__ import annotations

import hashlib
import hmac
import re

_SHA256_HEX = re.compile(r"^[0-9a-f]{64}$")


def content_sha256(content: str) -> str:
    """Return the SHA-256 digest of the exact UTF-8 message content."""
    if not isinstance(content, str):
        raise TypeError("Message content must be text")
    return hashlib.sha256(content.encode("utf-8")).hexdigest()


def verify_content_sha256(content: str, digest: object) -> bool:
    """Return True only for a well-formed digest matching the exact content."""
    if not isinstance(digest, str) or not _SHA256_HEX.fullmatch(digest):
        return False
    return hmac.compare_digest(content_sha256(content), digest)
