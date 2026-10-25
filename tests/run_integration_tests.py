"""Run the chat application's API and WebSocket integration tests.

Usage from the repository root:
    python tests/run_integration_tests.py

The test server uses an isolated temporary database and uploads directory.
"""
from __future__ import annotations

import asyncio
import json
import os
import shutil
import socket
import subprocess
import sys
import tempfile
import time
from pathlib import Path

import httpx
import websockets

ROOT = Path(__file__).resolve().parents[1]
APP_SOURCE = ROOT / "App"


def check(name: str, condition: bool) -> None:
    if not condition:
        raise AssertionError(f"FAIL: {name}")
    print(f"PASS: {name}", flush=True)


def free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


def wait_for_server(base_url: str, process: subprocess.Popen[str]) -> None:
    deadline = time.monotonic() + 15
    while time.monotonic() < deadline:
        if process.poll() is not None:
            output = process.stdout.read() if process.stdout else ""
            raise RuntimeError(f"Test server exited early.\n{output}")
        try:
            response = httpx.get(f"{base_url}/health", timeout=0.5)
            if response.status_code == 200:
                return
        except httpx.HTTPError:
            time.sleep(0.1)
    output = process.stdout.read() if process.stdout else ""
    raise RuntimeError(f"Test server did not start.\n{output}")


async def websocket_tests(base_url: str, port: int, group_id: str) -> None:
    ws_base = f"ws://127.0.0.1:{port}/ws/"
    async with websockets.connect(ws_base + "testuser01") as sender, websockets.connect(
        ws_base + "testuser02"
    ) as recipient:
        await sender.send(json.dumps({
            "type": "send_message", "msg_id": "private-001",
            "recipient": "testuser02", "content": "hello private", "timestamp": "12:00",
        }))
        event = json.loads(await asyncio.wait_for(sender.recv(), 3))
        check("private sender sent status", event.get("type") == "status_update" and event.get("status") == "sent")
        event = json.loads(await asyncio.wait_for(recipient.recv(), 3))
        check("private message delivery", event.get("type") == "receive_message" and event.get("content") == "hello private")
        event = json.loads(await asyncio.wait_for(sender.recv(), 3))
        check("delivered receipt", event.get("status") == "delivered")

        await recipient.send(json.dumps({"type": "read_ack", "msg_id": "private-001"}))
        event = json.loads(await asyncio.wait_for(sender.recv(), 3))
        check("read receipt", event.get("status") == "read")

        await sender.send(json.dumps({"type": "edit_message", "msg_id": "private-001", "content": "edited private"}))
        first = json.loads(await asyncio.wait_for(sender.recv(), 3))
        second = json.loads(await asyncio.wait_for(recipient.recv(), 3))
        check("message edit broadcast", first.get("type") == "message_edit" and second.get("content") == "edited private")

        await sender.send(json.dumps({"type": "delete_message", "msg_id": "private-001", "scope": "everyone"}))
        first = json.loads(await asyncio.wait_for(sender.recv(), 3))
        second = json.loads(await asyncio.wait_for(recipient.recv(), 3))
        check("delete-for-everyone broadcast", first.get("type") == "message_delete" and second.get("scope") == "everyone")

        await sender.send(json.dumps({
            "type": "send_message", "msg_id": "private-002", "recipient": "testuser02",
            "content": "delete for me test", "timestamp": "12:02",
        }))
        event = json.loads(await asyncio.wait_for(sender.recv(), 3))
        check("second private message accepted", event.get("status") == "sent")
        event = json.loads(await asyncio.wait_for(recipient.recv(), 3))
        check("second private message delivered", event.get("msg_id") == "private-002")
        event = json.loads(await asyncio.wait_for(sender.recv(), 3))
        check("second private delivery receipt", event.get("status") == "delivered")
        await recipient.send(json.dumps({"type": "delete_message", "msg_id": "private-002", "scope": "everyone"}))
        event = json.loads(await asyncio.wait_for(recipient.recv(), 3))
        check("non-sender cannot delete for everyone", event.get("type") == "error")
        await recipient.send(json.dumps({"type": "delete_message", "msg_id": "private-002", "scope": "me"}))
        event = json.loads(await asyncio.wait_for(recipient.recv(), 3))
        check("delete for me event", event.get("type") == "message_delete" and event.get("scope") == "me")

        await sender.send(json.dumps({"type": "send_message", "msg_id": "group-001", "group_id": group_id, "content": "group hello"}))
        event = json.loads(await asyncio.wait_for(sender.recv(), 3))
        check("group sender sent status", event.get("type") == "status_update" and event.get("status") == "sent")
        event = json.loads(await asyncio.wait_for(recipient.recv(), 3))
        check("group message delivery", event.get("type") == "receive_message" and event.get("group_id") == group_id)
        event = json.loads(await asyncio.wait_for(sender.recv(), 3))
        check("group delivered receipt", event.get("status") == "delivered")

        await sender.send(json.dumps({"type": "typing", "recipient": "testuser02"}))
        event = json.loads(await asyncio.wait_for(recipient.recv(), 3))
        check("typing indicator event", event.get("type") == "typing" and event.get("username") == "testuser01")

        await sender.send(json.dumps({"type": "send_message", "msg_id": "group-001", "group_id": group_id, "content": "duplicate"}))
        event = json.loads(await asyncio.wait_for(sender.recv(), 3))
        check("duplicate message ID handled without disconnect", event.get("type") == "error")

        await recipient.send(json.dumps({"type": "send_message", "msg_id": "invalid-group", "group_id": "not-a-member", "content": "blocked"}))
        event = json.loads(await asyncio.wait_for(recipient.recv(), 3))
        check("unauthorized group send rejected safely", event.get("type") == "error")

    # Test offline delivery after the original sender has disconnected.
    async with websockets.connect(ws_base + "testuser01") as sender:
        await sender.send(json.dumps({
            "type": "send_message", "msg_id": "offline-001", "recipient": "testuser02",
            "content": "offline delivery", "timestamp": "12:05",
        }))
        event = json.loads(await asyncio.wait_for(sender.recv(), 3))
        check("offline message accepted", event.get("status") == "sent")
    async with websockets.connect(ws_base + "testuser02") as recipient:
        event = json.loads(await asyncio.wait_for(recipient.recv(), 3))
        check("offline message delivered on reconnect", event.get("type") == "receive_message" and event.get("msg_id") == "offline-001")


def main() -> None:
    with tempfile.TemporaryDirectory(prefix="chat-web-tests-") as temporary:
        temp = Path(temporary)
        app_dir = temp / "App"
        shutil.copytree(APP_SOURCE, app_dir, ignore=shutil.ignore_patterns("__pycache__", "chat_app.db", "uploads"))
        (app_dir / "uploads").mkdir(exist_ok=True)
        db_path = temp / "integration-test.db"
        port = free_port()
        env = os.environ.copy()
        env["CHAT_DB_PATH"] = str(db_path)
        env["PYTHONPATH"] = str(app_dir)
        process = subprocess.Popen(
            [sys.executable, "-m", "uvicorn", "main:app", "--host", "127.0.0.1", "--port", str(port)],
            cwd=app_dir,
            env=env,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
        )
        base_url = f"http://127.0.0.1:{port}"
        try:
            wait_for_server(base_url, process)
            with httpx.Client(base_url=base_url, timeout=5) as client:
                check("home page", client.get("/").status_code == 200)
                check("health endpoint", client.get("/health").json() == {"status": "ok"})
                check("seed users endpoint", len(client.get("/api/users").json()["users"]) >= 3)

                check("contact request", client.post("/api/contacts/add", json={"user_id": "testuser01", "contact_username": "testuser02"}).status_code == 200)
                check("duplicate contact request rejected", client.post("/api/contacts/add", json={"user_id": "testuser01", "contact_username": "testuser02"}).status_code == 409)
                check("self-contact rejected", client.post("/api/contacts/add", json={"user_id": "testuser01", "contact_username": "testuser01"}).status_code == 400)
                check("pending request visible", "testuser01" in client.get("/api/contacts/pending/testuser02").json()["pending"])
                check("accept contact request", client.post("/api/contacts/respond", json={"user_id": "testuser02", "requester_id": "testuser01", "action": "accept"}).status_code == 200)
                check("accepted contact visible", "testuser02" in client.get("/api/contacts/testuser01").json()["contacts"])
                check("private history endpoint", client.get("/api/messages/history/testuser01/testuser02").status_code == 200)
                check("unknown user history rejected", client.get("/api/messages/history/testuser01/missing-user").status_code == 404)

                group_response = client.post("/api/groups", json={"creator": "testuser01", "name": "Test Group", "members": ["testuser02"]})
                check("create group", group_response.status_code == 200)
                group_id = group_response.json()["group_id"]
                check("group listing", len(client.get("/api/groups/testuser01").json()["groups"]) == 1)
                check("group member list", len(client.get(f"/api/groups/{group_id}/members", params={"username": "testuser01"}).json()["members"]) == 2)
                check("group history endpoint", client.get(f"/api/groups/{group_id}/history", params={"username": "testuser01"}).status_code == 200)
                check("non-member group access rejected", client.get(f"/api/groups/{group_id}/members", params={"username": "shrujan"}).status_code == 403)
                check("rename group", client.post(f"/api/groups/{group_id}/rename", json={"username": "testuser01", "value": "Renamed"}).status_code == 200)
                check("change group icon", client.post(f"/api/groups/{group_id}/icon", json={"username": "testuser01", "value": "🔥"}).status_code == 200)
                check("promote group admin", client.post(f"/api/groups/{group_id}/admins", json={"username": "testuser01", "target": "testuser02"}).status_code == 200)
                check("demote group admin", client.delete(f"/api/groups/{group_id}/admins/testuser02", params={"username": "testuser01"}).status_code == 200)
                check("demote missing admin rejected", client.delete(f"/api/groups/{group_id}/admins/testuser02", params={"username": "testuser01"}).status_code == 404)
                check("blank group rename rejected", client.post(f"/api/groups/{group_id}/rename", json={"username": "testuser01", "value": "  "}).status_code == 400)

                upload = client.post("/api/upload", files={"file": ("hello.txt", b"hello world", "text/plain")})
                check("file upload", upload.status_code == 200)
                check("uploaded file retrieval", client.get(upload.json()["file_url"]).content == b"hello world")
                check("empty search", client.get("/api/search", params={"username": "testuser01", "q": "  "}).json() == {"contacts": [], "messages": []})
                check("search SQL query", client.get("/api/search", params={"username": "testuser01", "q": "hello"}).status_code == 200)

            asyncio.run(websocket_tests(base_url, port, group_id))

            with httpx.Client(base_url=base_url, timeout=5) as client:
                history = client.get("/api/messages/history/testuser01/testuser02").json()["history"]
                check("message history persisted", any(item["msg_id"] == "private-001" for item in history))
                check("deleted-for-everyone message redacted in history", next(item for item in history if item["msg_id"] == "private-001")["deleted_everyone"] is True)
                check("delete-for-me hidden from recipient history", all(item["msg_id"] != "private-002" for item in client.get("/api/messages/history/testuser02/testuser01").json()["history"]))
                check("delete-for-me preserves sender history", any(item["msg_id"] == "private-002" for item in history))
                search = client.get("/api/search", params={"username": "testuser01", "q": "group hello"}).json()
                check("message search returns group message", any(item["msg_id"] == "group-001" for item in search["messages"]))
                check("remove contact", client.post("/api/contacts/remove", json={"user_id": "testuser01", "contact_username": "testuser02"}).status_code == 200)

        finally:
            process.terminate()
            try:
                process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait(timeout=5)
        print("\nALL INTEGRATION TESTS PASSED", flush=True)


if __name__ == "__main__":
    main()
