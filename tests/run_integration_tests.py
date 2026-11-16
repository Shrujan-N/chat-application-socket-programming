
"""Authenticated API and WebSocket integration tests.

Run from the repository root:
    python tests/run_integration_tests.py

Uses an isolated temporary SQLite database.
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
from urllib.parse import quote

import httpx
import websockets


ROOT = Path(__file__).resolve().parents[1]
APP_SOURCE = ROOT / "App"

TEST_CREDENTIALS = {
    "testuser01": "Test@1234",
    "testuser02": "Pass1234",
    "shrujan": "Pass1234",
}


def check(name: str, condition: bool) -> None:
    if not condition:
        raise AssertionError(f"FAIL: {name}")
    print(f"PASS: {name}", flush=True)


def free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


def read_server_log(log_file) -> str:
    """Read all server output from the redirected log file."""
    log_file.flush()
    log_file.seek(0)
    return log_file.read()


def stop_process(
    process: subprocess.Popen[str],
    log_file,
) -> str:
    """Stop Uvicorn and return its captured log-file contents."""
    if process.poll() is None:
        process.terminate()

    try:
        process.wait(timeout=5)
    except subprocess.TimeoutExpired:
        process.kill()
        process.wait(timeout=5)

    return read_server_log(log_file)


def wait_for_server(
    base_url: str,
    process: subprocess.Popen[str],
    log_file,
) -> None:
    deadline = time.monotonic() + 25

    while time.monotonic() < deadline:
        if process.poll() is not None:
            output = stop_process(process, log_file)
            raise RuntimeError(
                f"Test server exited during startup.\n{output}"
            )

        try:
            response = httpx.get(
                f"{base_url}/health/live",
                timeout=1,
            )

            if (
                response.status_code == 200
                and response.json() == {"status": "alive"}
            ):
                print("Test server is ready.", flush=True)
                return

        except (httpx.HTTPError, ValueError):
            pass

        time.sleep(0.2)

    output = stop_process(process, log_file)

    raise RuntimeError(
        f"Test server did not become ready.\n{output}"
    )


def login_test_users(
    client: httpx.Client,
) -> dict[str, str]:
    """Log in using accounts seeded by App/database.py."""

    tokens = {}

    for username, password in TEST_CREDENTIALS.items():
        response = client.post(
            "/api/login",
            json={
                "username": username,
                "password": password,
            },
        )

        check(
            f"login {username}",
            response.status_code == 200,
        )

        data = response.json()
        token = data.get("token")

        check(
            f"token issued for {username}",
            isinstance(token, str) and bool(token),
        )

        tokens[username] = token

    return tokens


def use_user(
    client: httpx.Client,
    username: str,
    tokens: dict[str, str],
) -> None:
    client.headers["Authorization"] = (
        f"Bearer {tokens[username]}"
    )


def api_tests(
    base_url: str,
) -> tuple[str, dict[str, str]]:

    with httpx.Client(
        base_url=base_url,
        timeout=10,
    ) as client:

        check(
            "home page",
            client.get("/").status_code == 200,
        )

        live = client.get("/health/live")

        check(
            "liveness endpoint",
            live.status_code == 200
            and live.json() == {"status": "alive"},
        )

        ready = client.get("/health/ready")

        check(
            "database readiness",
            ready.status_code == 200
            and ready.json().get("status") == "ready",
        )

        # Authenticate using the pre-seeded accounts.
        tokens = login_test_users(client)

        use_user(client, "testuser01", tokens)

        # Verify registered users.
        users_response = client.get("/api/users")

        check(
            "registered users endpoint",
            users_response.status_code == 200
            and set(TEST_CREDENTIALS).issubset(
                set(users_response.json().get("users", []))
            ),
        )

        # Send contact request.
        response = client.post(
            "/api/contacts/add",
            json={
                "user_id": "testuser01",
                "contact_username": "testuser02",
            },
        )

        check(
            "contact request",
            response.status_code == 200
            and response.json().get("success") is True,
        )

        # Duplicate contact request.
        duplicate = client.post(
            "/api/contacts/add",
            json={
                "user_id": "testuser01",
                "contact_username": "testuser02",
            },
        )

        check(
            "duplicate contact request rejected",
            duplicate.status_code == 200
            and duplicate.json().get("success") is False,
        )

        # Self-contact request.
        self_contact = client.post(
            "/api/contacts/add",
            json={
                "user_id": "testuser01",
                "contact_username": "testuser01",
            },
        )

        check(
            "self-contact rejected",
            self_contact.status_code == 200
            and self_contact.json().get("success") is False,
        )

        # Accept contact request as recipient.
        use_user(client, "testuser02", tokens)

        pending = client.get(
            "/api/contacts/pending/testuser02"
        )

        check(
            "pending request visible",
            pending.status_code == 200
            and "testuser01"
            in pending.json().get("pending", []),
        )

        accepted = client.post(
            "/api/contacts/respond",
            json={
                "user_id": "testuser02",
                "requester_id": "testuser01",
                "action": "accept",
            },
        )

        check(
            "accept contact request",
            accepted.status_code == 200
            and accepted.json().get("success") is True,
        )

        # Verify accepted contact.
        use_user(client, "testuser01", tokens)

        contacts = client.get(
            "/api/contacts/testuser01"
        )

        check(
            "accepted contact visible",
            contacts.status_code == 200
            and "testuser02"
            in contacts.json().get("contacts", []),
        )

        # Private history.
        history = client.get(
            "/api/messages/history/testuser01/testuser02"
        )

        check(
            "private history endpoint",
            history.status_code == 200,
        )

        unknown_history = client.get(
            "/api/messages/history/testuser01/missing-user"
        )

        check(
            "unknown user history returns empty history",
            unknown_history.status_code == 200
            and unknown_history.json().get("history") == [],
        )

        # Create group. Creator is derived from the authenticated session.
        group_response = client.post(
            "/api/groups",
            json={
                "name": "Integration Test Group",
                "members": ["testuser02"],
            },
        )

        check(
            "create group",
            group_response.status_code == 200
            and bool(
                group_response.json()
                .get("group", {})
                .get("group_id")
            ),
        )

        group_id = group_response.json()["group"]["group_id"]

        # List groups for authenticated user.
        group_listing = client.get("/api/groups")

        check(
            "group listing",
            group_listing.status_code == 200
            and any(
                item.get("group_id") == group_id
                for item in group_listing.json().get("groups", [])
            ),
        )

        # Group members.
        members = client.get(
            f"/api/groups/{group_id}/members"
        )

        check(
            "group member list",
            members.status_code == 200
            and len(members.json().get("members", [])) == 2,
        )

        # Group history.
        group_history = client.get(
            f"/api/groups/{group_id}/history"
        )

        check(
            "group history endpoint",
            group_history.status_code == 200,
        )

        # Verify non-member cannot access group.
        use_user(client, "shrujan", tokens)

        forbidden = client.get(
            f"/api/groups/{group_id}/members"
        )

        check(
            "non-member group access rejected",
            forbidden.status_code == 403,
        )

        # Restore creator identity.
        use_user(client, "testuser01", tokens)

        # Promote group admin.
        promoted = client.post(
            f"/api/groups/{group_id}/admins",
            json={"username": "testuser02"},
        )

        check(
            "promote group admin",
            promoted.status_code == 200,
        )

        # Demote group admin.
        demoted = client.delete(
            f"/api/groups/{group_id}/admins/testuser02"
        )

        check(
            "demote group admin",
            demoted.status_code == 200,
        )

        # Demote an already demoted user.
        missing_admin = client.delete(
            f"/api/groups/{group_id}/admins/testuser02"
        )

        check(
            "demote missing admin rejected",
            missing_admin.status_code == 404,
        )

        # File upload.
        upload = client.post(
            "/api/upload",
            files={
                "file": (
                    "integration-test.txt",
                    b"hello world",
                    "text/plain",
                )
            },
        )

        check(
            "file upload",
            upload.status_code == 200,
        )

        file_url = upload.json().get("file_url", "")

        check(
            "upload response contains file URL",
            bool(file_url),
        )

        # File retrieval.
        download = client.get(file_url)

        check(
            "uploaded file retrieval",
            download.status_code == 200
            and download.content == b"hello world",
        )

        # Empty search.
        empty_search = client.get(
            "/api/search",
            params={"q": "  "},
        )

        check(
            "empty search",
            empty_search.status_code == 200
            and empty_search.json()
            == {"contacts": [], "messages": []},
        )

        # Search endpoint.
        search = client.get(
            "/api/search",
            params={"q": "hello"},
        )

        check(
            "search endpoint",
            search.status_code == 200,
        )

        return group_id, tokens


async def receive_json(
    websocket,
    timeout: float = 5,
) -> dict:
    raw = await asyncio.wait_for(
        websocket.recv(),
        timeout=timeout,
    )

    return json.loads(raw)


async def websocket_tests(
    port: int,
    group_id: str,
    tokens: dict[str, str],
) -> None:

    ws_base = f"ws://127.0.0.1:{port}/ws/"

    user1_url = (
        ws_base
        + "testuser01?token="
        + quote(tokens["testuser01"], safe="")
    )

    user2_url = (
        ws_base
        + "testuser02?token="
        + quote(tokens["testuser02"], safe="")
    )

    async with websockets.connect(user1_url) as sender:
        async with websockets.connect(user2_url) as recipient:

            # Send private message.
            await sender.send(json.dumps({
                "type": "send_message",
                "msg_id": "private-001",
                "recipient": "testuser02",
                "content": "hello private",
                "timestamp": "12:00",
            }))

            event = await receive_json(sender)

            check(
                "private sender sent status",
                event.get("type") == "status_update"
                and event.get("status") == "sent",
            )

            event = await receive_json(recipient)

            check(
                "private message delivery",
                event.get("type") == "receive_message"
                and event.get("content") == "hello private",
            )

            event = await receive_json(sender)

            check(
                "delivered receipt",
                event.get("status") == "delivered",
            )

            # Read receipt.
            await recipient.send(json.dumps({
                "type": "read_ack",
                "msg_id": "private-001",
                "sender": "testuser01",
            }))

            event = await receive_json(sender)

            check(
                "read receipt",
                event.get("status") == "read",
            )

            # Edit message.
            await sender.send(json.dumps({
                "type": "edit_message",
                "msg_id": "private-001",
                "content": "edited private",
            }))

            sender_event = await receive_json(sender)
            recipient_event = await receive_json(recipient)

            check(
                "message edit broadcast",
                sender_event.get("type") == "message_edit"
                and recipient_event.get("content") == "edited private",
            )

            # Delete for everyone.
            await sender.send(json.dumps({
                "type": "delete_message",
                "msg_id": "private-001",
                "scope": "everyone",
            }))

            sender_event = await receive_json(sender)
            recipient_event = await receive_json(recipient)

            check(
                "delete-for-everyone broadcast",
                sender_event.get("type") == "message_delete"
                and recipient_event.get("scope") == "everyone",
            )

            # Send second private message.
            await sender.send(json.dumps({
                "type": "send_message",
                "msg_id": "private-002",
                "recipient": "testuser02",
                "content": "delete for me test",
                "timestamp": "12:02",
            }))

            event = await receive_json(sender)

            check(
                "second private message accepted",
                event.get("status") == "sent",
            )

            event = await receive_json(recipient)

            check(
                "second private message delivered",
                event.get("msg_id") == "private-002",
            )

            event = await receive_json(sender)

            check(
                "second private delivery receipt",
                event.get("status") == "delivered",
            )

            # Recipient cannot delete sender's message for everyone.
            await recipient.send(json.dumps({
                "type": "delete_message",
                "msg_id": "private-002",
                "scope": "everyone",
            }))

            event = await receive_json(recipient)

            check(
                "non-sender cannot delete for everyone",
                event.get("type") == "error",
            )

            # Delete for me.
            await recipient.send(json.dumps({
                "type": "delete_message",
                "msg_id": "private-002",
                "scope": "me",
            }))

            event = await receive_json(recipient)

            check(
                "delete for me event",
                event.get("type") == "message_delete"
                and event.get("scope") == "me",
            )

            # Send group message.
            await sender.send(json.dumps({
                "type": "send_group_message",
                "msg_id": "group-001",
                "group_id": group_id,
                "content": "group hello",
                "timestamp": "2026-11-16T12:03:00Z",
            }))

            event = await receive_json(sender)

            check(
                "group sender sent status",
                event.get("type") == "status_update"
                and event.get("status") == "sent",
            )

            event = await receive_json(recipient)

            check(
                "group message delivery",
                event.get("type") == "receive_message"
                and event.get("group_id") == group_id,
            )

            # Typing indicator.
            await sender.send(json.dumps({
                "type": "typing",
                "recipient": "testuser02",
            }))

            event = await receive_json(recipient)

            check(
                "typing indicator event",
                event.get("type") == "typing"
                and event.get("username") == "testuser01",
            )

            # Duplicate message ID.
            await sender.send(json.dumps({
                "type": "send_group_message",
                "msg_id": "group-001",
                "group_id": group_id,
                "content": "duplicate",
                "timestamp": "2026-11-16T12:03:01Z",
            }))

            event = await receive_json(sender)

            check(
                "duplicate message ID handled",
                event.get("type") == "error",
            )

            # Unauthorized group message.
            await recipient.send(json.dumps({
                "type": "send_group_message",
                "msg_id": "invalid-group",
                "group_id": "not-a-member",
                "content": "blocked",
                "timestamp": "2026-11-16T12:03:02Z",
            }))

            event = await receive_json(recipient)

            check(
                "unauthorized group send rejected",
                event.get("type") == "error",
            )

    # Offline message delivery.
    async with websockets.connect(user1_url) as sender:

        await sender.send(json.dumps({
            "type": "send_message",
            "msg_id": "offline-001",
            "recipient": "testuser02",
            "content": "offline delivery",
            "timestamp": "12:05",
        }))

        event = await receive_json(sender)

        check(
            "offline message accepted",
            event.get("status") == "sent",
        )

    # Offline messages are retrieved through the authenticated sync API.
    # The server does not push the backlog automatically on WebSocket connect.
    with httpx.Client(
        base_url=f"http://127.0.0.1:{port}",
        headers={"Authorization": f"Bearer {tokens['testuser02']}"},
        timeout=10,
    ) as client:
        sync = client.get("/api/messages/sync", params={"since": 0})
        synced_messages = sync.json().get("messages", []) if sync.status_code == 200 else []

        check(
            "offline message delivered through reconnect sync",
            sync.status_code == 200
            and any(
                item.get("msg_id") == "offline-001"
                and item.get("content") == "offline delivery"
                for item in synced_messages
            ),
        )


def post_websocket_tests(
    base_url: str,
    tokens: dict[str, str],
) -> None:

    with httpx.Client(
        base_url=base_url,
        timeout=10,
    ) as client:

        use_user(client, "testuser01", tokens)

        response = client.get(
            "/api/messages/history/testuser01/testuser02"
        )

        check(
            "post-WebSocket history endpoint",
            response.status_code == 200,
        )

        history = response.json().get("history", [])

        deleted_message = next(
            (
                item for item in history
                if item.get("msg_id") == "private-001"
            ),
            None,
        )

        check(
            "message history persisted",
            deleted_message is not None,
        )

        check(
            "deleted-for-everyone message redacted in history",
            deleted_message is not None
            and bool(deleted_message.get("deleted_everyone")),
        )

        # Check recipient history.
        use_user(client, "testuser02", tokens)

        recipient_response = client.get(
            "/api/messages/history/testuser02/testuser01"
        )

        check(
            "recipient history endpoint",
            recipient_response.status_code == 200,
        )

        recipient_history = recipient_response.json().get(
            "history", []
        )

        check(
            "delete-for-me hidden from recipient history",
            all(
                item.get("msg_id") != "private-002"
                for item in recipient_history
            ),
        )

        # Sender retains their own copy.
        use_user(client, "testuser01", tokens)

        sender_response = client.get(
            "/api/messages/history/testuser01/testuser02"
        )

        sender_history = sender_response.json().get(
            "history", []
        )

        check(
            "delete-for-me preserves sender history",
            any(
                item.get("msg_id") == "private-002"
                for item in sender_history
            ),
        )

        # Search for group message.
        search = client.get(
            "/api/search/complete",
            params={"q": "group hello"},
        )

        check(
            "group message search endpoint",
            search.status_code == 200,
        )

        check(
            "message search returns group message",
            any(
                item.get("msg_id") == "group-001"
                and item.get("conversation_type") == "group"
                and item.get("group_id") is not None
                for item in search.json().get("messages", [])
            ),
        )

        # Remove contact.
        remove = client.post(
            "/api/contacts/remove",
            json={
                "user_id": "testuser01",
                "contact_username": "testuser02",
            },
        )

        check(
            "remove contact",
            remove.status_code == 200
            and remove.json().get("success") is True,
        )


def main() -> None:

    if not APP_SOURCE.is_dir():
        raise FileNotFoundError(
            f"Application directory not found: {APP_SOURCE}"
        )

    with tempfile.TemporaryDirectory(
        prefix="chat-web-tests-"
    ) as temporary:

        temp_dir = Path(temporary)
        app_dir = temp_dir / "App"

        shutil.copytree(
            APP_SOURCE,
            app_dir,
            ignore=shutil.ignore_patterns(
                "__pycache__",
                "*.pyc",
                "chat_app.db",
                "uploads",
                "logs",
                ".jwt_secret",
            ),
        )

        (app_dir / "uploads").mkdir(exist_ok=True)

        database_path = temp_dir / "integration-test.db"
        port = free_port()

        env = os.environ.copy()
        env["CHAT_DB_PATH"] = str(database_path)
        env["CHAT_WEB_DB_PATH"] = str(database_path)
        env["PYTHONPATH"] = str(app_dir)
        env["JWT_SECRET"] = "integration-test-secret-change-me"

        server_log_path = temp_dir / "uvicorn.log"
        server_log = server_log_path.open(
            "w+",
            encoding="utf-8",
        )

        process = subprocess.Popen(
            [
                sys.executable,
                "-m",
                "uvicorn",
                "main:app",
                "--host",
                "127.0.0.1",
                "--port",
                str(port),
            ],
            cwd=app_dir,
            env=env,
            stdout=server_log,
            stderr=subprocess.STDOUT,
            text=True,
        )

        base_url = f"http://127.0.0.1:{port}"

        try:
            wait_for_server(base_url, process, server_log)

            group_id, tokens = api_tests(base_url)

            asyncio.run(
                websocket_tests(
                    port,
                    group_id,
                    tokens,
                )
            )

            post_websocket_tests(
                base_url,
                tokens,
            )

        except Exception:
            output = stop_process(process, server_log)

            if output:
                print(
                    "\n--- TEST SERVER OUTPUT ---\n" + output,
                    flush=True,
                )

            raise

        finally:
            if process.poll() is None:
                output = stop_process(process, server_log)

                if output:
                    print(
                        "\n--- TEST SERVER OUTPUT ---\n" + output,
                        flush=True,
                    )

            server_log.close()

        print(
            "\nALL INTEGRATION TESTS PASSED",
            flush=True,
        )


if __name__ == "__main__":
    main()
