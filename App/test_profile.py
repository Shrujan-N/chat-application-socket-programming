"""Tests for FR-05 Profile Management (web API + TCP server).

Run from the project root:     pytest -v
"""
import asyncio
import base64
import json
import os
import socket
import sqlite3
import struct
import tempfile
import threading
import zlib

_tmp = tempfile.mkdtemp()
os.environ["CHAT_DB_PATH"] = os.path.join(_tmp, "test_profile.db")
os.environ["JWT_SECRET"] = "test-secret"

import pytest
from fastapi.testclient import TestClient

import database
import main
import profile_service
import server
from database import init_db

init_db()
web = TestClient(main.app)


# ------------------------------------------------------------------ helpers
def make_png(extra: int = 0) -> bytes:
    """A real 1x1 PNG, optionally padded with `extra` bytes (to test size limits)."""
    def chunk(kind, data):
        body = kind + data
        return struct.pack(">I", len(data)) + body + struct.pack(">I", zlib.crc32(body) & 0xFFFFFFFF)

    png = (
        b"\x89PNG\r\n\x1a\n"
        + chunk(b"IHDR", struct.pack(">IIBBBBB", 1, 1, 8, 2, 0, 0, 0))
        + chunk(b"IDAT", zlib.compress(b"\x00\xff\x00\x00"))
        + chunk(b"IEND", b"")
    )
    return png + b"\x00" * extra


@pytest.fixture(autouse=True)
def avatar_dir(monkeypatch, tmp_path):
    """Keep uploaded pictures out of the project's real uploads folder."""
    uploads = tmp_path / "uploads"
    monkeypatch.setattr(profile_service, "UPLOADS_DIR", str(uploads))
    monkeypatch.setattr(profile_service, "AVATAR_DIR", str(uploads / "avatars"))
    return uploads / "avatars"


def make_user(name, password="Valid1234"):
    r = web.post("/api/register", json={"username": name, "email": f"{name}@example.com", "password": password})
    assert r.status_code == 200
    token = web.post("/api/login", json={"username": name, "password": password}).json()["token"]
    return {"Authorization": f"Bearer {token}"}


def get_profile(headers, username):
    r = web.get(f"/api/profile/{username}", headers=headers)
    assert r.status_code == 200
    return r.json()["profile"]


def put_profile(headers, **body):
    return web.put("/api/profile", json=body, headers=headers)


def upload(headers, data, name="me.png", content_type="image/png"):
    return web.post("/api/profile/avatar", files={"file": (name, data, content_type)}, headers=headers)


def befriend(a_headers, a, b_headers, b):
    web.post("/api/contacts/add", json={"user_id": a, "contact_username": b}, headers=a_headers)
    web.post("/api/contacts/respond", json={"user_id": b, "requester_id": a, "action": "accept"}, headers=b_headers)


# ------------------------------------------------------------------ schema
def test_init_db_adds_profiles_table_to_an_existing_database(monkeypatch, tmp_path):
    old = tmp_path / "old.db"
    conn = sqlite3.connect(old)
    conn.execute("CREATE TABLE users (username TEXT PRIMARY KEY, email TEXT UNIQUE NOT NULL, password TEXT NOT NULL)")
    conn.execute("INSERT INTO users VALUES ('legacy', 'legacy@example.com', 'x')")
    conn.commit()
    conn.close()

    monkeypatch.setattr(database, "DB_PATH", str(old))
    init_db()
    init_db()  # running twice is harmless

    conn = sqlite3.connect(old)
    tables = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    users = conn.execute("SELECT username FROM users WHERE username='legacy'").fetchall()
    conn.close()
    assert "profiles" in tables and users  # old data kept


# ------------------------------------------------------------- web: defaults
def test_new_user_profile_defaults_to_username():
    h = make_user("pf_default")
    p = get_profile(h, "pf_default")
    assert p["display_name"] == "pf_default" and p["bio"] == "" and p["avatar_url"] is None


def test_unknown_user_profile_is_404():
    h = make_user("pf_unknown_caller")
    assert web.get("/api/profile/nobody_here_x", headers=h).status_code == 404


def test_profile_endpoints_require_a_session():
    assert web.get("/api/profile/testuser01").status_code == 401
    assert web.get("/api/profiles?users=testuser01").status_code == 401
    assert web.put("/api/profile", json={"display_name": "X"}).status_code == 401
    assert web.post("/api/profile/avatar", files={"file": ("a.png", make_png(), "image/png")}).status_code == 401
    assert web.delete("/api/profile/avatar").status_code == 401


# ------------------------------------------------- web: display name and status
def test_update_display_name_and_bio_are_saved_and_visible_to_others():
    alice = make_user("pf_alice")
    bob = make_user("pf_bob")
    r = put_profile(alice, display_name="Alice  O'Neil-Smith", bio="Out for lunch")
    assert r.status_code == 200 and r.json()["success"]
    assert r.json()["profile"]["display_name"] == "Alice O'Neil-Smith"       # extra spaces tidied

    seen_by_bob = get_profile(bob, "pf_alice")
    assert seen_by_bob["display_name"] == "Alice O'Neil-Smith" and seen_by_bob["bio"] == "Out for lunch"

    batch = web.get("/api/profiles?users=pf_alice,pf_bob,not_a_user", headers=bob).json()["profiles"]
    assert {p["username"]: p["display_name"] for p in batch} == {
        "pf_alice": "Alice O'Neil-Smith", "pf_bob": "pf_bob"
    }                                                                           # unknown names skipped


def test_update_only_one_field_keeps_the_other():
    h = make_user("pf_partial")
    put_profile(h, display_name="Partial", bio="Hello")
    put_profile(h, bio="Changed")
    p = get_profile(h, "pf_partial")
    assert p["display_name"] == "Partial" and p["bio"] == "Changed"
    put_profile(h, display_name="Renamed")
    assert get_profile(h, "pf_partial")["bio"] == "Changed"


def test_unicode_names_are_allowed():
    h = make_user("pf_unicode")
    assert put_profile(h, display_name="श्रेया Müller").status_code == 200
    assert get_profile(h, "pf_unicode")["display_name"] == "श्रेया Müller"


def test_bio_can_be_cleared():
    h = make_user("pf_clear")
    put_profile(h, bio="something")
    assert put_profile(h, bio="").status_code == 200
    assert get_profile(h, "pf_clear")["bio"] == ""


@pytest.mark.parametrize("name,expected", [
    ("", "cannot be empty"),
    ("   ", "cannot be empty"),
    ("x" * 51, "at most 50"),
    ("<script>alert(1)</script>", "may only contain"),
    ("Bob & Co", "may only contain"),
    ("Smile 😀", "may only contain"),
    ("Line\nBreak", "line breaks"),
])
def test_invalid_display_name_is_rejected_and_profile_unchanged(name, expected):
    h = make_user("pf_bad_" + str(abs(hash(name)) % 10**8))
    user = web.get("/api/session", headers=h).json()["username"]
    put_profile(h, display_name="Good Name", bio="Good bio")
    r = put_profile(h, display_name=name)
    assert r.status_code == 400 and expected in r.json()["detail"]
    p = get_profile(h, user)
    assert p["display_name"] == "Good Name" and p["bio"] == "Good bio"


@pytest.mark.parametrize("bio,expected", [
    ("x" * 151, "at most 150"),
    ("<b>bold</b>", "< or >"),
    ("two\nlines", "line breaks"),
])
def test_invalid_bio_is_rejected_and_nothing_is_saved(bio, expected):
    h = make_user("pf_badbio_" + str(abs(hash(bio)) % 10**8))
    user = web.get("/api/session", headers=h).json()["username"]
    put_profile(h, display_name="Before", bio="Before bio")
    # a valid name sent together with an invalid bio must not be half-saved
    r = put_profile(h, display_name="After", bio=bio)
    assert r.status_code == 400 and expected in r.json()["detail"]
    p = get_profile(h, user)
    assert p["display_name"] == "Before" and p["bio"] == "Before bio"


def test_update_with_nothing_is_rejected():
    h = make_user("pf_nothing")
    r = put_profile(h)
    assert r.status_code == 400 and "Nothing to update" in r.json()["detail"]


def test_user_can_only_change_their_own_profile():
    a = make_user("pf_owner")
    b = make_user("pf_other")
    put_profile(b, display_name="Mallory")
    assert get_profile(a, "pf_owner")["display_name"] == "pf_owner"   # b's request only touched b
    assert get_profile(a, "pf_other")["display_name"] == "Mallory"


def test_profile_changes_are_audited():
    h = make_user("pf_audit")
    put_profile(h, display_name="Audited")
    upload(h, make_png())
    conn = sqlite3.connect(database.DB_PATH)
    events = {r[0] for r in conn.execute("SELECT event FROM login_logs WHERE username='pf_audit'")}
    conn.close()
    assert {"profile_updated", "profile_picture_updated"} <= events


# ------------------------------------------------------------ web: picture
def test_upload_avatar_saves_file_and_returns_url(avatar_dir):
    h = make_user("pf_pic")
    r = upload(h, make_png())
    assert r.status_code == 200 and r.json()["success"]
    url = r.json()["profile"]["avatar_url"]
    assert url.startswith("/uploads/avatars/pf_pic_") and url.endswith(".png")
    assert (avatar_dir / os.path.basename(url)).read_bytes() == make_png()
    assert get_profile(h, "pf_pic")["avatar_url"] == url


def test_replacing_avatar_deletes_the_old_file(avatar_dir):
    h = make_user("pf_replace")
    first = upload(h, make_png()).json()["profile"]["avatar_url"]
    second = upload(h, make_png(5)).json()["profile"]["avatar_url"]
    assert first != second
    assert not (avatar_dir / os.path.basename(first)).exists()
    assert (avatar_dir / os.path.basename(second)).exists()


def test_filename_in_upload_cannot_escape_the_avatar_folder(avatar_dir):
    h = make_user("pf_path")
    r = upload(h, make_png(), name="../../evil.png")
    assert r.status_code == 200
    stored = os.path.basename(r.json()["profile"]["avatar_url"])
    assert stored.startswith("pf_path_") and (avatar_dir / stored).exists()
    assert not (avatar_dir.parent / "evil.png").exists()


def test_oversized_avatar_is_rejected_and_profile_unchanged(monkeypatch, avatar_dir):
    h = make_user("pf_big")
    good = upload(h, make_png()).json()["profile"]["avatar_url"]
    monkeypatch.setattr(profile_service, "MAX_AVATAR_BYTES", 1000)
    r = upload(h, make_png(5000))
    assert r.status_code == 413 and "too large" in r.json()["detail"]
    assert get_profile(h, "pf_big")["avatar_url"] == good
    assert [p.name for p in avatar_dir.iterdir()] == [os.path.basename(good)]


def test_real_two_megabyte_limit():
    h = make_user("pf_2mb")
    assert upload(h, make_png(profile_service.MAX_AVATAR_BYTES - len(make_png()))).status_code == 200
    r = upload(h, make_png(profile_service.MAX_AVATAR_BYTES))
    assert r.status_code == 413 and "maximum size is 2 MB" in r.json()["detail"]


@pytest.mark.parametrize("name,data,ctype", [
    ("notes.png", b"just some text pretending to be a picture", "image/png"),   # wrong content, right name
    ("logo.svg", b"<svg xmlns='http://www.w3.org/2000/svg'><script>1</script></svg>", "image/svg+xml"),
    ("page.html", b"<html><script>alert(1)</script></html>", "text/html"),
    ("empty.png", b"", "image/png"),
])
def test_non_image_uploads_are_rejected(name, data, ctype, avatar_dir):
    h = make_user("pf_nonimg_" + str(abs(hash(name)) % 10**8))
    user = web.get("/api/session", headers=h).json()["username"]
    r = upload(h, data, name=name, content_type=ctype)
    assert r.status_code == 400
    assert "Unsupported image" in r.json()["detail"] or "empty" in r.json()["detail"]
    assert get_profile(h, user)["avatar_url"] is None
    assert not avatar_dir.exists() or not list(avatar_dir.iterdir())


@pytest.mark.parametrize("data,ext", [
    (b"\xff\xd8\xff\xe0" + b"\x00" * 20, "jpg"),
    (b"GIF89a" + b"\x00" * 20, "gif"),
    (b"RIFF\x10\x00\x00\x00WEBPVP8 " + b"\x00" * 20, "webp"),
])
def test_other_image_formats_accepted(data, ext):
    h = make_user(f"pf_fmt_{ext}")
    r = upload(h, data, name=f"x.{ext}", content_type="application/octet-stream")
    assert r.status_code == 200 and r.json()["profile"]["avatar_url"].endswith("." + ext)


def test_remove_avatar(avatar_dir):
    h = make_user("pf_remove")
    url = upload(h, make_png()).json()["profile"]["avatar_url"]
    r = web.delete("/api/profile/avatar", headers=h)
    assert r.status_code == 200 and r.json()["profile"]["avatar_url"] is None
    assert not (avatar_dir / os.path.basename(url)).exists()
    again = web.delete("/api/profile/avatar", headers=h)
    assert again.status_code == 400 and "no profile picture" in again.json()["detail"]


def test_avatar_change_keeps_name_and_bio():
    h = make_user("pf_keep")
    put_profile(h, display_name="Keeper", bio="stay")
    upload(h, make_png())
    web.delete("/api/profile/avatar", headers=h)
    p = get_profile(h, "pf_keep")
    assert p["display_name"] == "Keeper" and p["bio"] == "stay"


# ------------------------------------------- web: immediately visible to contacts
class FakeSocket:
    def __init__(self):
        self.frames = []

    async def send_json(self, frame):
        self.frames.append(frame)


def test_contacts_online_get_the_change_immediately_but_strangers_do_not():
    a, b, c = make_user("pf_live_a"), make_user("pf_live_b"), make_user("pf_live_c")
    befriend(a, "pf_live_a", b, "pf_live_b")

    sockets = {n: FakeSocket() for n in ("pf_live_a", "pf_live_b", "pf_live_c")}
    main.manager.active_connections.update(sockets)
    try:
        assert put_profile(a, display_name="Live A", bio="busy").status_code == 200
        upload(a, make_png())
        web.delete("/api/profile/avatar", headers=a)
    finally:
        for name in sockets:
            main.manager.disconnect(name)

    for name in ("pf_live_a", "pf_live_b"):                  # myself (other tab) and my contact
        frames = sockets[name].frames
        assert [f["type"] for f in frames] == ["profile_update"] * 3
        assert frames[0]["profile"]["username"] == "pf_live_a"
        assert frames[0]["profile"]["display_name"] == "Live A" and frames[0]["profile"]["bio"] == "busy"
        assert frames[1]["profile"]["avatar_url"] and frames[2]["profile"]["avatar_url"] is None
    assert sockets["pf_live_c"].frames == []                  # not a contact


def test_rejected_change_sends_nothing():
    a, b = make_user("pf_quiet_a"), make_user("pf_quiet_b")
    befriend(a, "pf_quiet_a", b, "pf_quiet_b")
    socket_b = FakeSocket()
    main.manager.active_connections["pf_quiet_b"] = socket_b
    try:
        assert put_profile(a, display_name="<bad>").status_code == 400
    finally:
        main.manager.disconnect("pf_quiet_b")
    assert socket_b.frames == []


def test_original_contact_endpoints_still_work():
    a, b = make_user("pf_orig_a"), make_user("pf_orig_b")
    befriend(a, "pf_orig_a", b, "pf_orig_b")
    assert web.get("/api/contacts/pf_orig_a", headers=a).json() == {"contacts": ["pf_orig_b"]}
    assert web.get("/api/users", headers=a).status_code == 200


# --------------------------------------------------------------------- TCP
@pytest.fixture(scope="module")
def address():
    listener = socket.socket()
    listener.bind(("127.0.0.1", 0))
    listener.listen()
    stop = threading.Event()

    def accept_loop():
        listener.settimeout(0.2)
        while not stop.is_set():
            try:
                conn, addr = listener.accept()
            except (socket.timeout, OSError):
                continue
            threading.Thread(target=server.handle_client, args=(conn, addr), daemon=True).start()

    threading.Thread(target=accept_loop, daemon=True).start()
    yield listener.getsockname()
    stop.set()
    listener.close()


class Conn:
    def __init__(self, addr):
        self.sock = socket.create_connection(addr, timeout=5)
        self.reader = self.sock.makefile("r", encoding="utf-8")
        self.recv()  # welcome line

    def send(self, **event):
        self.sock.sendall((json.dumps(event) + "\n").encode())

    def recv(self):
        line = self.reader.readline()
        return json.loads(line) if line else None

    def recv_type(self, wanted):
        while True:
            e = self.recv()
            if e is None or e.get("type") == wanted:
                return e

    def command(self, text):
        self.send(type="command", message=text)

    def close(self):
        self.sock.close()


def tcp_user(addr, name):
    c = Conn(addr)
    c.send(type="register", username=name, email=f"{name}@example.com", password="Valid1234")
    assert c.recv()["success"]
    c.send(type="login", username=name, password="Valid1234")
    assert c.recv()["success"]
    assert "Welcome" in c.recv_type("text")["message"]
    return c


def test_tcp_view_and_change_name_and_status(address):
    c = tcp_user(address, "pf_tcp1")
    c.command("/profile")
    assert "Display name: pf_tcp1" in c.recv_type("text")["message"]

    c.command("/profile name Tina T.")
    r = c.recv_type("profile")
    assert r["profile"]["display_name"] == "Tina T." and "updated" in r["message"]

    c.command("/profile bio at the library")
    assert c.recv_type("profile")["profile"]["bio"] == "at the library"

    c.command("/profile")
    text = c.recv_type("text")["message"]
    assert "Display name: Tina T." in text and "Status: at the library" in text

    c.command("/profile clear-bio")
    assert c.recv_type("profile")["profile"]["bio"] == ""
    c.close()


def test_tcp_invalid_input_rejected_and_unchanged(address):
    c = tcp_user(address, "pf_tcp2")
    c.command("/profile name Keep Me")
    c.recv_type("profile")

    c.command("/profile name <script>")
    assert "Profile not changed" in c.recv_type("text")["message"]
    c.command("/profile bio " + "x" * 151)
    assert "at most 150" in c.recv_type("text")["message"]
    c.command("/profile bio <b>hi</b>")
    assert "< or >" in c.recv_type("text")["message"]

    assert profile_service.get_profile("pf_tcp2")["display_name"] == "Keep Me"
    assert profile_service.get_profile("pf_tcp2")["bio"] == ""
    c.close()


def test_tcp_view_other_user_and_unknown_user(address):
    a = tcp_user(address, "pf_tcp3")
    tcp_user(address, "pf_tcp4").close()
    a.command("/profile view pf_tcp4")
    assert "Profile for pf_tcp4" in a.recv_type("text")["message"]
    a.command("/profile view nobody_here_x")
    assert "User not found" in a.recv_type("text")["message"]
    a.close()


def test_tcp_avatar_upload_rejections_and_removal(address, avatar_dir, monkeypatch):
    c = tcp_user(address, "pf_tcp5")

    c.send(type="profile_avatar", data=base64.b64encode(make_png()).decode())
    r = c.recv_type("profile")
    assert r["profile"]["avatar_url"] and "picture updated" in r["message"]
    stored = r["profile"]["avatar_url"]

    c.send(type="profile_avatar", data=base64.b64encode(b"not an image").decode())
    assert "Unsupported image" in c.recv_type("text")["message"]
    c.send(type="profile_avatar", data="***not base64***")
    assert "not valid" in c.recv_type("text")["message"]
    monkeypatch.setattr(profile_service, "MAX_AVATAR_BYTES", 1000)
    c.send(type="profile_avatar", data=base64.b64encode(make_png(5000)).decode())
    assert "too large" in c.recv_type("text")["message"]
    assert profile_service.get_profile("pf_tcp5")["avatar_url"] == stored   # still the good one

    c.command("/profile remove-avatar")
    assert c.recv_type("profile")["profile"]["avatar_url"] is None
    c.close()


def test_tcp_chat_partner_sees_the_change_immediately(address):
    a = tcp_user(address, "pf_tcp6")
    b = tcp_user(address, "pf_tcp7")
    a.command("/chat pf_tcp7")
    a.recv_type("text")
    b.recv_type("text")

    a.command("/profile name Sixth One")
    b_event = b.recv_type("profile_update")
    assert b_event["profile"]["username"] == "pf_tcp6" and b_event["profile"]["display_name"] == "Sixth One"
    a.close(); b.close()


def test_tcp_original_commands_still_work(address):
    c = tcp_user(address, "pf_tcp8")
    c.command("/groups")
    assert "not in any groups" in c.recv_type("text")["message"]
    c.command("/nonsense")
    assert "Unknown command" in c.recv_type("text")["message"]
    c.close()