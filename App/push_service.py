"""Push notifications (FR-19), used by main.py (web).

A browser that has allowed notifications registers a *push subscription*.
When a new message arrives for a user who is offline, or whose chat page is not
in front, the server sends a Web Push message to every subscribed device of that
user.  The service worker (static/sw.js) turns it into an OS notification with a
preview of the message, so the app does not need to stay open.

Rules (from the requirement):
  * no notification for muted chats / contacts
  * a message never produces the same notification twice
    (one push per message per device, enforced in the database)

Setup:
    pip install pywebpush
If pywebpush is not installed, push stays switched off and the chat works as before.

Optional environment variables:
    VAPID_KEY_FILE        where the server's private signing key is kept
                          (default: .vapid_private.pem next to this file; it is
                          created on first use - keep it, because changing it
                          invalidates every existing subscription)
    VAPID_SUBJECT         contact for the push services, e.g. mailto:you@example.com
    PUSH_TTL_SECONDS      how long a push service keeps an undelivered push (default 3600)
    PUSH_ALLOWED_HOSTS    extra push-service host suffixes, comma separated
    PUSH_ALLOW_ANY_ENDPOINT=1   skip the host allow-list (development only)
"""
import base64
import html
import json
import os
import re
import threading
import time
from datetime import datetime, timezone
from urllib.parse import urlparse

import profile_service
from auth_service import AuthError, record_event
from database import get_db

try:
    from pywebpush import webpush, WebPushException
except ImportError:  # push stays off; everything else keeps working
    webpush = None

    class WebPushException(Exception):
        response = None


APP_DIR = os.path.dirname(os.path.abspath(__file__))
VAPID_KEY_FILE = os.environ.get("VAPID_KEY_FILE", os.path.join(APP_DIR, ".vapid_private.pem"))
VAPID_SUBJECT = os.environ.get("VAPID_SUBJECT", "mailto:admin@example.com")
PUSH_TTL_SECONDS = int(os.environ.get("PUSH_TTL_SECONDS", "3600"))

PREVIEW_MAX = 100
MAX_SUBSCRIPTIONS_PER_USER = 10
PUSH_LOG_KEEP_SECONDS = 24 * 3600

# Subscriptions may only point at the real browser push services, otherwise a
# user could make the server send requests to any address.
_DEFAULT_HOST_SUFFIXES = (
    "googleapis.com",        # Chrome / Edge / Opera (FCM)
    "mozilla.com",           # Firefox
    "windows.com",           # Edge (WNS)
    "apple.com",             # Safari
)
_EXTRA_HOST_SUFFIXES = tuple(
    s.strip().lower().lstrip(".")
    for s in os.environ.get("PUSH_ALLOWED_HOSTS", "").split(",") if s.strip()
)
_BASE64URL = re.compile(r"[A-Za-z0-9_\-]+={0,2}")
_TAGS = re.compile(r"<[^>]*>")
_FILE_LINK = re.compile(r"📎\s*Download\s+([^<]+)")


class PushGone(Exception):
    """The push service says this subscription no longer exists (404/410)."""


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


# ---------------------------------------------------------------- VAPID key
_key_lock = threading.Lock()
_public_key_cache = None


def _load_or_create_private_key():
    from cryptography.hazmat.primitives import serialization
    from cryptography.hazmat.primitives.asymmetric import ec

    def load():
        with open(VAPID_KEY_FILE, "rb") as f:
            return serialization.load_pem_private_key(f.read(), password=None)

    try:
        return load()
    except FileNotFoundError:
        pass

    key = ec.generate_private_key(ec.SECP256R1())
    pem = key.private_bytes(
        serialization.Encoding.PEM,
        serialization.PrivateFormat.PKCS8,
        serialization.NoEncryption(),
    )
    try:
        fd = os.open(VAPID_KEY_FILE, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    except FileExistsError:      # another process created it first
        return load()
    with os.fdopen(fd, "wb") as f:
        f.write(pem)
    return key


def get_public_key() -> str:
    """The server's public key (base64url, uncompressed point) for the browser."""
    global _public_key_cache
    with _key_lock:
        if _public_key_cache is None:
            from cryptography.hazmat.primitives import serialization
            key = _load_or_create_private_key()
            raw = key.public_key().public_bytes(
                serialization.Encoding.X962,
                serialization.PublicFormat.UncompressedPoint,
            )
            _public_key_cache = base64.urlsafe_b64encode(raw).rstrip(b"=").decode()
        return _public_key_cache


def is_enabled() -> bool:
    """True when pywebpush is installed and the signing key is usable."""
    if webpush is None:
        return False
    try:
        get_public_key()
        return True
    except Exception as error:
        print(f"Push notifications disabled: {error}")
        return False


# ---------------------------------------------------------- subscriptions
def _endpoint_allowed(endpoint: str) -> bool:
    if os.environ.get("PUSH_ALLOW_ANY_ENDPOINT") == "1":
        return True
    host = (urlparse(endpoint).hostname or "").lower()
    for suffix in _DEFAULT_HOST_SUFFIXES + _EXTRA_HOST_SUFFIXES:
        if host == suffix or host.endswith("." + suffix):
            return True
    return False


def _check_subscription(endpoint, keys) -> tuple:
    if (not isinstance(endpoint, str) or not endpoint
            or len(endpoint) > 2048 or not endpoint.startswith("https://")):
        raise AuthError(400, "Invalid push subscription.")
    if not _endpoint_allowed(endpoint):
        raise AuthError(400, "This push service is not supported.")
    if not isinstance(keys, dict):
        raise AuthError(400, "Invalid push subscription.")
    p256dh, auth = keys.get("p256dh"), keys.get("auth")
    for value in (p256dh, auth):
        if (not isinstance(value, str) or not value or len(value) > 256
                or not _BASE64URL.fullmatch(value)):
            raise AuthError(400, "Invalid push subscription.")
    return p256dh, auth


def save_subscription(username: str, endpoint, keys, user_agent=None, ip=None) -> None:
    """Remember this device for `username`. Subscribing again with the same
    endpoint just refreshes it (and moves it to the user who is logged in now)."""
    p256dh, auth = _check_subscription(endpoint, keys)

    conn = get_db()
    try:
        cursor = conn.cursor()
        cursor.execute("SELECT 1 FROM users WHERE username = ?", (username,))
        if cursor.fetchone() is None:
            raise AuthError(404, "User not found")
        cursor.execute(
            """
            INSERT INTO push_subscriptions (endpoint, username, p256dh, auth, user_agent, created_at)
            VALUES (?, ?, ?, ?, ?, ?)
            ON CONFLICT(endpoint) DO UPDATE SET
                username = excluded.username,
                p256dh = excluded.p256dh,
                auth = excluded.auth,
                user_agent = excluded.user_agent,
                created_at = excluded.created_at
            """,
            (endpoint, username, p256dh, auth, (user_agent or "")[:300], _utc_now())
        )
        # Keep only the newest few devices per user.
        cursor.execute(
            """
            DELETE FROM push_subscriptions
            WHERE username = ? AND endpoint NOT IN (
                SELECT endpoint FROM push_subscriptions
                WHERE username = ? ORDER BY created_at DESC, rowid DESC LIMIT ?
            )
            """,
            (username, username, MAX_SUBSCRIPTIONS_PER_USER)
        )
        conn.commit()
    finally:
        conn.close()

    record_event(username, "push_subscribed", True, ip)


def remove_subscription(username: str, endpoint, ip=None) -> bool:
    if not isinstance(endpoint, str) or not endpoint:
        raise AuthError(400, "Invalid push subscription.")
    conn = get_db()
    try:
        cursor = conn.cursor()
        cursor.execute(
            "DELETE FROM push_subscriptions WHERE endpoint = ? AND username = ?",
            (endpoint, username)
        )
        removed = cursor.rowcount > 0
        conn.commit()
    finally:
        conn.close()

    if removed:
        record_event(username, "push_unsubscribed", True, ip)
    return removed


def _drop_endpoint(endpoint: str) -> None:
    conn = get_db()
    try:
        conn.execute("DELETE FROM push_subscriptions WHERE endpoint = ?", (endpoint,))
        conn.commit()
    finally:
        conn.close()


def _subscriptions_of(username: str) -> list:
    conn = get_db()
    try:
        rows = conn.execute(
            "SELECT endpoint, p256dh, auth FROM push_subscriptions WHERE username = ?",
            (username,)
        ).fetchall()
        return [dict(r) for r in rows]
    finally:
        conn.close()


# -------------------------------------------------------------------- mute
def list_muted(username: str) -> list:
    conn = get_db()
    try:
        rows = conn.execute(
            "SELECT muted_user FROM muted_chats WHERE username = ? ORDER BY muted_user",
            (username,)
        ).fetchall()
        return [r["muted_user"] for r in rows]
    finally:
        conn.close()


def is_muted(recipient: str, sender: str) -> bool:
    conn = get_db()
    try:
        row = conn.execute(
            "SELECT 1 FROM muted_chats WHERE username = ? AND muted_user = ?",
            (recipient, sender)
        ).fetchone()
        return row is not None
    finally:
        conn.close()


def set_muted(username: str, other, muted: bool) -> list:
    """Mute or unmute the chat with `other`. Returns the user's muted list."""
    other = (other or "").strip() if isinstance(other, str) else ""
    if not other:
        raise AuthError(400, "Username required")
    if other == username:
        raise AuthError(400, "You cannot mute yourself.")

    conn = get_db()
    try:
        cursor = conn.cursor()
        cursor.execute("SELECT 1 FROM users WHERE username = ?", (other,))
        if cursor.fetchone() is None:
            raise AuthError(404, "User not found")
        if muted:
            cursor.execute(
                "INSERT OR IGNORE INTO muted_chats (username, muted_user, created_at) VALUES (?, ?, ?)",
                (username, other, _utc_now())
            )
        else:
            cursor.execute(
                "DELETE FROM muted_chats WHERE username = ? AND muted_user = ?",
                (username, other)
            )
        conn.commit()
    finally:
        conn.close()
    return list_muted(username)


# ----------------------------------------------------------------- payload
def make_preview(content) -> str:
    """Plain-text preview for the notification body (never HTML)."""
    text = content if isinstance(content, str) else ""
    if text.startswith("e2e:dh:") or text.startswith("e2e:aes:"):
        return "🔒 New encrypted message"

    if "<img" in text.lower():
        return "📷 Photo"

    file_match = _FILE_LINK.search(text)
    if file_match:
        return "📎 " + file_match.group(1).strip()[:PREVIEW_MAX]

    text = html.unescape(_TAGS.sub("", text))
    text = " ".join(text.split())
    if not text:
        return "New message"
    if len(text) > PREVIEW_MAX:
        text = text[:PREVIEW_MAX - 1].rstrip() + "…"
    return text


def _sender_label(username: str):
    try:
        profile = profile_service.get_profile(username)
        return profile.get("display_name") or username, profile.get("avatar_url")
    except Exception:
        return username, None


def build_message_payload(msg_id: str, sender: str, content) -> dict:
    title, icon = _sender_label(sender)
    return {
        "type": "message",
        "title": title,
        "body": make_preview(content),
        "tag": f"msg-{msg_id}",       # same tag = the browser shows it only once
        "sender": sender,
        "msg_id": msg_id,
        "icon": icon,
    }


# ----------------------------------------------------------------- sending
def _send(subscription: dict, payload: dict) -> None:
    """Deliver one push message. Raises PushGone if the device is no longer valid."""
    if webpush is None:
        raise RuntimeError("pywebpush is not installed")
    try:
        webpush(
            subscription_info={
                "endpoint": subscription["endpoint"],
                "keys": {"p256dh": subscription["p256dh"], "auth": subscription["auth"]},
            },
            data=json.dumps(payload),
            vapid_private_key=VAPID_KEY_FILE,
            vapid_claims={"sub": VAPID_SUBJECT},
            ttl=PUSH_TTL_SECONDS,
            headers={"Urgency": "high"},
            timeout=10,
        )
    except WebPushException as error:
        status = getattr(getattr(error, "response", None), "status_code", None)
        if status in (404, 410):
            raise PushGone() from error
        raise


def _claim(msg_id: str, endpoint: str) -> bool:
    """True the first time this message is about to be pushed to this device."""
    now = int(time.time())
    conn = get_db()
    try:
        cursor = conn.cursor()
        cursor.execute(
            "INSERT OR IGNORE INTO push_log (msg_id, endpoint, sent_at) VALUES (?, ?, ?)",
            (msg_id, endpoint, now)
        )
        claimed = cursor.rowcount == 1
        if claimed:
            cursor.execute("DELETE FROM push_log WHERE sent_at < ?", (now - PUSH_LOG_KEEP_SECONDS,))
        conn.commit()
        return claimed
    finally:
        conn.close()


def _deliver(subscriptions: list, payload: dict, msg_id=None) -> int:
    sent = 0
    for subscription in subscriptions:
        if msg_id is not None and not _claim(msg_id, subscription["endpoint"]):
            continue                                  # this device already got it
        try:
            _send(subscription, payload)
            sent += 1
        except PushGone:
            _drop_endpoint(subscription["endpoint"])  # uninstalled / permission revoked
        except Exception as error:
            print(f"Push to one device failed: {error}")
    return sent


def notify_new_message(msg_id: str, sender: str, recipient: str, content) -> int:
    """Push a new private message to the recipient's devices.

    Returns how many devices were notified. Nothing is sent when push is off,
    the recipient muted the sender, they are not contacts, or the message was
    already pushed to a device.
    """
    if not msg_id or not sender or not recipient or sender == recipient:
        return 0
    if not is_enabled():
        return 0
    if is_muted(recipient, sender):
        return 0
    if sender not in profile_service.contacts_of(recipient):
        return 0

    subscriptions = _subscriptions_of(recipient)
    if not subscriptions:
        return 0

    payload = build_message_payload(msg_id, sender, content)
    return _deliver(subscriptions, payload, msg_id=msg_id)


def send_test(username: str) -> int:
    """Send a test notification to every device of `username`."""
    if not is_enabled():
        return 0
    subscriptions = _subscriptions_of(username)
    payload = {
        "type": "test",
        "title": "Notifications are on",
        "body": "You will now be notified about new messages.",
        "tag": "push-test",
    }
    return _deliver(subscriptions, payload)