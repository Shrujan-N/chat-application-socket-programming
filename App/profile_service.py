"""Profile management (FR-05), shared by main.py (web) and server.py (TCP).

A profile has a display name, a short status/bio and an optional picture.
Every change is validated first; if anything is invalid an AuthError is raised
and nothing is saved, so the profile remains unchanged.
"""
import os
import re
import secrets
import unicodedata
from datetime import datetime, timezone

from auth_service import AuthError, record_event
from database import get_db

DISPLAY_NAME_MAX = 50
BIO_MAX = 150
MAX_AVATAR_BYTES = int(os.environ.get("MAX_AVATAR_BYTES", str(2 * 1024 * 1024)))  # 2 MB

APP_DIR = os.path.dirname(os.path.abspath(__file__))
UPLOADS_DIR = os.environ.get("CHAT_UPLOADS_DIR", os.path.join(APP_DIR, "uploads"))
AVATAR_DIR = os.path.join(UPLOADS_DIR, "avatars")
AVATAR_URL_PREFIX = "/uploads/avatars/"

# Allowed in a display name: letters, combining marks (needed for Indic and many
# other scripts), decimal digits, plus space . _ ' -
_NAME_PUNCTUATION = set(" ._'-")
_CONTROL_CHARS = re.compile(r"[\x00-\x1f\x7f]")


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


# ------------------------------------------------------------- validation
def _name_char_ok(ch: str) -> bool:
    if ch in _NAME_PUNCTUATION:
        return True
    category = unicodedata.category(ch)
    return category[0] in ("L", "M") or category == "Nd"


def validate_display_name(value) -> str:
    if not isinstance(value, str):
        raise AuthError(400, "Display name must be text.")
    if _CONTROL_CHARS.search(value):
        raise AuthError(400, "Display name cannot contain line breaks or control characters.")
    name = " ".join(value.split())
    if not name:
        raise AuthError(400, "Display name cannot be empty.")
    if len(name) > DISPLAY_NAME_MAX:
        raise AuthError(400, f"Display name must be at most {DISPLAY_NAME_MAX} characters.")
    if not all(_name_char_ok(ch) for ch in name):
        raise AuthError(
            400,
            "Display name may only contain letters, numbers, spaces and the "
            "characters . _ ' - (no symbols such as < > & \" or emoji)."
        )
    return name


def validate_bio(value) -> str:
    if not isinstance(value, str):
        raise AuthError(400, "Status must be text.")
    if _CONTROL_CHARS.search(value):
        raise AuthError(400, "Status cannot contain line breaks or control characters.")
    bio = value.strip()
    if len(bio) > BIO_MAX:
        raise AuthError(400, f"Status must be at most {BIO_MAX} characters (yours is {len(bio)}).")
    if "<" in bio or ">" in bio:
        raise AuthError(400, "Status cannot contain the characters < or >.")
    return bio


def detect_image_type(data: bytes):
    """Return the file extension for a PNG/JPEG/GIF/WebP image, judged by its
    content (not by the file name or the browser-supplied type), else None."""
    if data.startswith(b"\x89PNG\r\n\x1a\n"):
        return "png"
    if data.startswith(b"\xff\xd8\xff"):
        return "jpg"
    if data[:6] in (b"GIF87a", b"GIF89a"):
        return "gif"
    if data[:4] == b"RIFF" and data[8:12] == b"WEBP":
        return "webp"
    return None


# ---------------------------------------------------------------- queries
def _require_user(cursor, username: str) -> None:
    cursor.execute("SELECT 1 FROM users WHERE username = ?", (username,))
    if cursor.fetchone() is None:
        raise AuthError(404, "User not found")


def _row_to_profile(username: str, row) -> dict:
    display_name = row["display_name"] if row and row["display_name"] else username
    return {
        "username": username,
        "display_name": display_name,
        "bio": (row["bio"] or "") if row else "",
        "avatar_url": (AVATAR_URL_PREFIX + row["avatar"]) if row and row["avatar"] else None,
        "updated_at": row["updated_at"] if row else None,
    }


def get_profile(username: str) -> dict:
    conn = get_db()
    try:
        cursor = conn.cursor()
        _require_user(cursor, username)
        cursor.execute(
            "SELECT display_name, bio, avatar, updated_at FROM profiles WHERE username = ?",
            (username,)
        )
        return _row_to_profile(username, cursor.fetchone())
    finally:
        conn.close()


def get_profiles(usernames) -> list:
    """Profiles for several users at once (unknown names are skipped)."""
    names = []
    for name in usernames:
        name = (name or "").strip()
        if name and name not in names:
            names.append(name)
    names = names[:200]
    if not names:
        return []

    conn = get_db()
    try:
        marks = ",".join("?" * len(names))
        existing = {r["username"] for r in conn.execute(
            f"SELECT username FROM users WHERE username IN ({marks})", names)}
        rows = {r["username"]: r for r in conn.execute(
            f"SELECT username, display_name, bio, avatar, updated_at FROM profiles "
            f"WHERE username IN ({marks})", names)}
        return [_row_to_profile(n, rows.get(n)) for n in names if n in existing]
    finally:
        conn.close()


def contacts_of(username: str) -> list:
    """Users who have an accepted contact relationship with `username`."""
    conn = get_db()
    try:
        rows = conn.execute(
            """
            SELECT contact_id AS other FROM contacts WHERE user_id = ? AND status = 'accepted'
            UNION
            SELECT user_id AS other FROM contacts WHERE contact_id = ? AND status = 'accepted'
            """,
            (username, username)
        ).fetchall()
        return [r["other"] for r in rows]
    finally:
        conn.close()


# ---------------------------------------------------------------- updates
def _save(cursor, username, display_name, bio, avatar):
    cursor.execute(
        """
        INSERT INTO profiles (username, display_name, bio, avatar, updated_at)
        VALUES (?, ?, ?, ?, ?)
        ON CONFLICT(username) DO UPDATE SET
            display_name = excluded.display_name,
            bio = excluded.bio,
            avatar = excluded.avatar,
            updated_at = excluded.updated_at
        """,
        (username, display_name, bio, avatar, _utc_now())
    )


def update_profile(username: str, display_name=None, bio=None, ip=None) -> dict:
    """Change the display name and/or status. Both are validated before
    anything is written, so one invalid field leaves the profile untouched."""
    if display_name is None and bio is None:
        raise AuthError(400, "Nothing to update: provide a display name or a status.")

    new_name = validate_display_name(display_name) if display_name is not None else None
    new_bio = validate_bio(bio) if bio is not None else None

    conn = get_db()
    try:
        cursor = conn.cursor()
        _require_user(cursor, username)
        cursor.execute(
            "SELECT display_name, bio, avatar FROM profiles WHERE username = ?", (username,)
        )
        current = cursor.fetchone()
        _save(
            cursor, username,
            new_name if new_name is not None else (current["display_name"] if current else None),
            new_bio if new_bio is not None else (current["bio"] if current else ""),
            current["avatar"] if current else None,
        )
        conn.commit()
    finally:
        conn.close()

    record_event(username, "profile_updated", True, ip)
    return get_profile(username)


def _avatar_path(filename):
    # Only ever touch files directly inside AVATAR_DIR.
    safe = os.path.basename(filename or "")
    return os.path.join(AVATAR_DIR, safe) if safe else None


def _delete_avatar_file(filename):
    path = _avatar_path(filename)
    if path:
        try:
            os.remove(path)
        except OSError:
            pass


def set_avatar(username: str, data: bytes, ip=None) -> dict:
    """Validate and store a new profile picture."""
    if not data:
        raise AuthError(400, "The image file is empty.")
    if len(data) > MAX_AVATAR_BYTES:
        raise AuthError(
            413,
            f"Image is too large ({len(data) / (1024 * 1024):.1f} MB). "
            f"The maximum size is {MAX_AVATAR_BYTES / (1024 * 1024):.0f} MB."
        )
    extension = detect_image_type(data)
    if extension is None:
        raise AuthError(400, "Unsupported image. Use a PNG, JPEG, GIF or WebP picture.")

    conn = get_db()
    try:
        cursor = conn.cursor()
        _require_user(cursor, username)
        cursor.execute(
            "SELECT display_name, bio, avatar FROM profiles WHERE username = ?", (username,)
        )
        current = cursor.fetchone()

        os.makedirs(AVATAR_DIR, exist_ok=True)
        safe_user = re.sub(r"[^A-Za-z0-9_]", "_", username)
        filename = f"{safe_user}_{secrets.token_hex(8)}.{extension}"
        path = os.path.join(AVATAR_DIR, filename)
        temp_path = path + ".tmp"
        try:
            with open(temp_path, "wb") as f:
                f.write(data)
            os.replace(temp_path, path)
            _save(
                cursor, username,
                current["display_name"] if current else None,
                current["bio"] if current else "",
                filename,
            )
            conn.commit()
        except Exception:
            conn.rollback()
            for leftover in (temp_path, path):
                try:
                    os.remove(leftover)
                except OSError:
                    pass
            raise
        old_avatar = current["avatar"] if current else None
    finally:
        conn.close()

    if old_avatar and old_avatar != filename:
        _delete_avatar_file(old_avatar)

    record_event(username, "profile_picture_updated", True, ip)
    return get_profile(username)


def remove_avatar(username: str, ip=None) -> dict:
    conn = get_db()
    try:
        cursor = conn.cursor()
        _require_user(cursor, username)
        cursor.execute(
            "SELECT display_name, bio, avatar FROM profiles WHERE username = ?", (username,)
        )
        current = cursor.fetchone()
        if not current or not current["avatar"]:
            raise AuthError(400, "You have no profile picture to remove.")
        old_avatar = current["avatar"]
        _save(cursor, username, current["display_name"], current["bio"], None)
        conn.commit()
    finally:
        conn.close()

    _delete_avatar_file(old_avatar)
    record_event(username, "profile_picture_removed", True, ip)
    return get_profile(username)