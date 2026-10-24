import json
import socket
import threading
import time
import uuid

from database import init_db
from security import SESSION_TIMEOUT_MINUTES, TokenError
from auth_service import (
    AuthError, register_account, authenticate, record_event, user_exists,
    resume_session, refresh_session_token, revoke_session,
    request_password_reset, reset_password, RESET_REQUEST_MESSAGE,
)

HOST = "127.0.0.1"
PORT = 5000

# Seconds a connection may stay unauthenticated, and max auth messages per connection.
AUTH_TIMEOUT_SECONDS = 300
MAX_AUTH_MESSAGES = 20

# While a client is active, hand it a fresh session token at most this often (FR-03),
# so the token it saved on disk stays valid for a restart.
SESSION_REFRESH_SECONDS = 60

clients = {}
active_chats = {}
active_groups = {}
groups = {}
user_groups = {}
pending_messages = {}
offline_messages = {}
message_store = {}
clients_lock = threading.RLock()


def send_event(connection, event):
    try:
        connection.sendall(
            (json.dumps(event) + "\n").encode("utf-8")
        )
        return True
    except OSError:
        return False


def send_text(connection, message):
    return send_event(
        connection,
        {"type": "text", "message": message}
    )


def send_auth_result(connection, success, action, message, **extra):
    event = {
        "type": "auth_result",
        "success": success,
        "action": action,
        "message": message
    }
    event.update(extra)
    return send_event(connection, event)


def authenticate_connection(connection, reader, address):
    """
    Registration / login phase. The client sends one JSON event per line:
      {"type": "register", "username": ..., "email": ..., "password": ...}
      {"type": "login", "username": ..., "password": ...}
      {"type": "resume", "token": ...}      (return to a saved session)
      {"type": "forgot_password", "email": ...}
      {"type": "reset_password", "token": ..., "new_password": ...}
      {"type": "quit"}
    Returns (username, session_token, resumed), or (None, None, False) if the
    client leaves or exceeds the allowed number of attempts.
    """
    send_text(connection, "Welcome! Please log in or register.")

    for _ in range(MAX_AUTH_MESSAGES):
        line = reader.readline()

        if not line:
            return None, None, False

        try:
            event = json.loads(line)
        except json.JSONDecodeError:
            event = None

        if not isinstance(event, dict):
            send_auth_result(connection, False, "error", "Invalid message format.")
            continue

        event_type = event.get("type")
        username = event.get("username")
        password = event.get("password")
        username = username if isinstance(username, str) else ""
        password = password if isinstance(password, str) else ""

        try:
            if event_type == "register":
                email = event.get("email")
                register_account(
                    username,
                    email if isinstance(email, str) else "",
                    password
                )
                send_auth_result(
                    connection, True, "register",
                    "Registration successful. You can now log in."
                )

            elif event_type == "login":
                ip = address[0] if address else None
                result = authenticate(username, password, ip)
                send_auth_result(
                    connection, True, "login", "Login successful.",
                    username=result["username"],
                    token=result["token"],
                    expires_in=result["expires_in"]
                )
                return result["username"], result["token"], False

            elif event_type == "resume":
                ip = address[0] if address else None
                token = event.get("token")
                result = resume_session(token if isinstance(token, str) else "", ip)
                send_auth_result(
                    connection, True, "resume", "Session restored.",
                    username=result["username"],
                    token=result["token"],
                    expires_in=result["expires_in"]
                )
                return result["username"], result["token"], True

            elif event_type == "forgot_password":
                ip = address[0] if address else None
                email = event.get("email")
                request_password_reset(email if isinstance(email, str) else "", ip)
                send_auth_result(
                    connection, True, "forgot_password", RESET_REQUEST_MESSAGE
                )

            elif event_type == "reset_password":
                ip = address[0] if address else None
                token = event.get("token")
                new_password = event.get("new_password")
                reset_password(
                    token if isinstance(token, str) else "",
                    new_password if isinstance(new_password, str) else "",
                    ip
                )
                send_auth_result(
                    connection, True, "reset_password",
                    "Password reset successful. You can now log in with your new password."
                )

            elif event_type == "quit":
                return None, None, False

            else:
                send_auth_result(
                    connection, False, "error",
                    "Please log in or register first."
                )

        except AuthError as error:
            send_auth_result(
                connection, False, event_type or "error", error.detail,
                status=error.status_code
            )

    send_text(connection, "Too many attempts. Disconnecting.")
    return None, None, False


def send_status(username, message_id, status, message=None):
    event = {
        "type": "status",
        "message_id": message_id,
        "status": status
    }

    if message:
        event["message"] = message

    with clients_lock:
        connection = clients.get(username)

    if connection:
        send_event(connection, event)


def remove_queued_message(username, message_id):
    with clients_lock:
        queue = offline_messages.get(username, [])
        offline_messages[username] = [
            event for event in queue
            if event.get("message_id") != message_id
        ]


def notify_group(group, message, exclude=None):
    exclude = exclude or set()

    with clients_lock:
        recipients = [
            clients[member]
            for member in group["members"]
            if member in clients and member not in exclude
        ]

    for connection in recipients:
        send_text(connection, message)


def group_help(connection):
    send_text(
        connection,
        "Group management commands:\n"
        "/group-info name\n"
        "/group-add name username\n"
        "/group-remove name username\n"
        "/group-rename oldname newname\n"
        "/group-icon name emoji\n"
        "/group-admin name username\n"
        "/group-admin-remove name username\n"
        "/group-help\n"
        "Only admins can manage group settings and membership."
    )


def find_group(name):
    return groups.get(name.casefold())


def group_management(connection, username, command):
    parts = command.split()

    if not parts:
        return False

    action = parts[0]

    if action == "/group-help":
        group_help(connection)
        return True

    if action == "/group-info":
        if len(parts) != 2:
            send_text(connection, "Usage: /group-info name")
            return True

        with clients_lock:
            group = find_group(parts[1])

            if not group or username not in group["members"]:
                send_text(
                    connection,
                    "Group not found or you are not a member."
                )
                return True

            details = (
                f"Group: {group['icon']} {group['name']}\n"
                f"Creator: {group['creator']}\n"
                f"Admins: {', '.join(sorted(group['admins']))}\n"
                f"Members: {', '.join(sorted(group['members']))}"
            )

        send_text(connection, details)
        return True

    if action == "/group-add":
        if len(parts) != 3:
            send_text(connection, "Usage: /group-add name username")
            return True

        group_name, target = parts[1], parts[2]

        # Registered accounts can be added even if they have not connected yet.
        target_registered = user_exists(target)

        with clients_lock:
            group = find_group(group_name)

            if not group:
                send_text(connection, "Group not found.")
                return True

            if username not in group["admins"]:
                send_text(connection, "Only group admins can add members.")
                return True

            if not target_registered:
                send_text(
                    connection,
                    "That user is not registered."
                )
                return True

            if target in group["members"]:
                send_text(connection, f"{target} is already a member.")
                return True

            group["members"].add(target)
            user_groups.setdefault(target, set()).add(
                group["name"].casefold()
            )
            target_connection = clients.get(target)
            group_copy = dict(group)
            group_copy["members"] = set(group["members"])

        send_text(connection, f"Added {target} to {group_copy['name']}.")

        if target_connection:
            send_text(
                target_connection,
                f"You were added to group {group_copy['icon']} "
                f"{group_copy['name']} by {username}."
            )

        notify_group(
            group_copy,
            f"{target} was added to the group by {username}.",
            exclude={username, target}
        )
        return True

    if action == "/group-remove":
        if len(parts) != 3:
            send_text(connection, "Usage: /group-remove name username")
            return True

        group_name, target = parts[1], parts[2]

        with clients_lock:
            group = find_group(group_name)

            if not group:
                send_text(connection, "Group not found.")
                return True

            if username not in group["admins"]:
                send_text(connection, "Only group admins can remove members.")
                return True

            if target not in group["members"]:
                send_text(connection, f"{target} is not a member.")
                return True

            if target == group["creator"]:
                send_text(connection, "The group creator cannot be removed.")
                return True

            group["members"].remove(target)
            group["admins"].discard(target)
            user_groups.get(target, set()).discard(
                group["name"].casefold()
            )

            if active_groups.get(target) == group["name"].casefold():
                active_groups.pop(target, None)

            target_connection = clients.get(target)
            group_copy = dict(group)
            group_copy["members"] = set(group["members"])

        send_text(connection, f"Removed {target} from {group_copy['name']}.")

        if target_connection:
            send_text(
                target_connection,
                f"You were removed from group {group_copy['name']}."
            )

        notify_group(
            group_copy,
            f"{target} was removed from the group by {username}."
        )
        return True

    if action == "/group-rename":
        if len(parts) != 3:
            send_text(connection, "Usage: /group-rename oldname newname")
            return True

        old_name, new_name = parts[1], parts[2]
        old_key = old_name.casefold()
        new_key = new_name.casefold()

        if not new_name or len(new_name) > 40:
            send_text(connection, "The new name must be 1–40 characters.")
            return True

        with clients_lock:
            group = groups.get(old_key)

            if not group:
                send_text(connection, "Group not found.")
                return True

            if username not in group["admins"]:
                send_text(connection, "Only group admins can rename a group.")
                return True

            if new_key in groups and new_key != old_key:
                send_text(connection, "A group with that name already exists.")
                return True

            if new_key != old_key:
                groups.pop(old_key)
                groups[new_key] = group

                for member in group["members"]:
                    memberships = user_groups.setdefault(member, set())
                    memberships.discard(old_key)
                    memberships.add(new_key)

                for member, current_key in list(active_groups.items()):
                    if current_key == old_key:
                        active_groups[member] = new_key

            old_display_name = group["name"]
            group["name"] = new_name
            group_copy = dict(group)
            group_copy["members"] = set(group["members"])

        notify_group(
            group_copy,
            f"Group '{old_display_name}' was renamed to "
            f"'{new_name}' by {username}."
        )
        send_text(connection, f"Group renamed to '{new_name}'.")
        return True

    if action == "/group-icon":
        if len(parts) != 3:
            send_text(connection, "Usage: /group-icon name emoji")
            return True

        group_name, icon = parts[1], parts[2]

        with clients_lock:
            group = find_group(group_name)

            if not group:
                send_text(connection, "Group not found.")
                return True

            if username not in group["admins"]:
                send_text(connection, "Only group admins can change the icon.")
                return True

            group["icon"] = icon
            group_copy = dict(group)
            group_copy["members"] = set(group["members"])

        notify_group(
            group_copy,
            f"The icon for group '{group_copy['name']}' was changed to "
            f"{icon} by {username}."
        )
        return True

    if action == "/group-admin":
        if len(parts) != 3:
            send_text(connection, "Usage: /group-admin name username")
            return True

        group_name, target = parts[1], parts[2]

        with clients_lock:
            group = find_group(group_name)

            if not group:
                send_text(connection, "Group not found.")
                return True

            if username not in group["admins"]:
                send_text(connection, "Only group admins can assign admins.")
                return True

            if target not in group["members"]:
                send_text(connection, "That user is not a group member.")
                return True

            group["admins"].add(target)
            group_copy = dict(group)
            group_copy["members"] = set(group["members"])

        notify_group(
            group_copy,
            f"{target} was made a group admin by {username}."
        )
        return True

    if action == "/group-admin-remove":
        if len(parts) != 3:
            send_text(
                connection,
                "Usage: /group-admin-remove name username"
            )
            return True

        group_name, target = parts[1], parts[2]

        with clients_lock:
            group = find_group(group_name)

            if not group:
                send_text(connection, "Group not found.")
                return True

            if username not in group["admins"]:
                send_text(connection, "Only group admins can remove admins.")
                return True

            if target == group["creator"]:
                send_text(connection, "The creator must remain an admin.")
                return True

            if target not in group["admins"]:
                send_text(connection, f"{target} is not an admin.")
                return True

            group["admins"].remove(target)
            group_copy = dict(group)
            group_copy["members"] = set(group["members"])

        notify_group(
            group_copy,
            f"{target} is no longer a group admin. Updated by {username}."
        )
        return True

    return False


def handle_chat(username, event):
    message = event.get("message", "").strip()

    if not message:
        return

    if len(message) > 2000:
        send_text(clients[username], "Messages cannot exceed 2000 characters.")
        return

    message_id = event.get("message_id") or str(uuid.uuid4())

    with clients_lock:
        sender_connection = clients.get(username)
        group_key = active_groups.get(username)
        group = groups.get(group_key) if group_key else None

        if group:
            recipients = set(group["members"]) - {username}
            message_event = {
                "type": "group_message",
                "message_id": message_id,
                "group": group["name"],
                "icon": group.get("icon", "👥"),
                "sender": username,
                "message": message
            }
        else:
            target = active_chats.get(username)

            if not target:
                send_event(sender_connection, {
                    "type": "status",
                    "message_id": message_id,
                    "status": "failed",
                    "message": "Select a private chat or group first."
                })
                return

            recipients = {target}
            message_event = {
                "type": "private_message",
                "message_id": message_id,
                "sender": username,
                "message": message
            }

        if not recipients:
            send_event(sender_connection, {
                "type": "status",
                "message_id": message_id,
                "status": "failed",
                "message": "There are no other members in this chat."
            })
            return

        message_store[message_id] = {
            "sender": username,
            "recipients": set(recipients),
            "event": dict(message_event),
            "deleted": False,
            "deleted_for": set()
        }

        pending_messages[message_id] = {
            "sender": username,
            "recipients": set(recipients)
        }

        for recipient in recipients:
            queue = offline_messages.setdefault(recipient, [])

            if not any(
                item.get("message_id") == message_id
                for item in queue
            ):
                queue.append(dict(message_event))

            recipient_connection = clients.get(recipient)

            if recipient_connection:
                send_event(recipient_connection, message_event)

    send_status(username, message_id, "sent")


def find_message_id(prefix):
    matches = [
        message_id for message_id in message_store
        if message_id.startswith(prefix)
    ]

    if len(matches) != 1:
        return None

    return matches[0]


def handle_edit(connection, username, command):
    parts = command.split(maxsplit=2)

    if len(parts) != 3:
        send_text(connection, "Usage: /edit message_id new message content")
        return

    message_id_prefix = parts[1]
    new_content = parts[2].strip()

    if not new_content:
        send_text(connection, "Edited message cannot be empty.")
        return

    if len(new_content) > 2000:
        send_text(connection, "Messages cannot exceed 2000 characters.")
        return

    with clients_lock:
        message_id = find_message_id(message_id_prefix)

        if message_id is None:
            send_text(
                connection,
                "Message ID not found or ambiguous. Use the full ID."
            )
            return

        record = message_store[message_id]

        if record.get("deleted"):
            send_text(connection, "This message has been deleted.")
            return

        if record.get("deleted_for") and username in record["deleted_for"]:
            send_text(connection, "This message is deleted from your view.")
            return

        if record["sender"] != username:
            send_text(connection, "You can only edit your own messages.")
            return

        record["event"]["message"] = new_content
        record["event"]["edited"] = True

        recipients = set(record["recipients"])

        for recipient in recipients:
            for queued_event in offline_messages.get(recipient, []):
                if queued_event.get("message_id") == message_id:
                    queued_event["message"] = new_content
                    queued_event["edited"] = True

        recipient_connections = [
            clients[recipient]
            for recipient in recipients
            if recipient in clients
        ]

        updated_event = {
            "type": "message_edit",
            "message_id": message_id,
            "sender": username,
            "message": new_content,
            "edited": True
        }

    for recipient_connection in recipient_connections:
        send_event(recipient_connection, updated_event)

    send_text(connection, f"Message {message_id[:8]} edited successfully.")


def handle_delete(connection, username, command):
    parts = command.split(maxsplit=2)

    if len(parts) != 3 or parts[2].casefold() not in {"me", "everyone"}:
        send_text(connection, "Usage: /delete message_id me|everyone")
        return

    message_id_prefix = parts[1]
    scope = parts[2].casefold()

    with clients_lock:
        message_id = find_message_id(message_id_prefix)

        if message_id is None:
            send_text(
                connection,
                "Message ID not found or ambiguous. Use the full ID."
            )
            return

        record = message_store[message_id]

        if record.get("deleted"):
            send_text(connection, "This message has already been deleted.")
            return

        sender = record["sender"]
        recipients = set(record["recipients"])
        participants = recipients | {sender}

        if username not in participants:
            send_text(connection, "You are not a participant in this message.")
            return

        if scope == "everyone":
            if username != sender:
                send_text(
                    connection,
                    "Only the original sender can delete for everyone."
                )
                return

            record["deleted"] = True
            record["deleted_for"] = set(participants)
            record["event"]["message"] = "This message was deleted."
            record["event"]["deleted"] = True

            for participant in participants:
                offline_messages[participant] = [
                    event
                    for event in offline_messages.get(participant, [])
                    if event.get("message_id") != message_id
                ]

            connections = [
                clients[participant]
                for participant in participants
                if participant in clients
            ]

            delete_event = {
                "type": "message_delete",
                "message_id": message_id,
                "sender": sender,
                "scope": "everyone"
            }
        else:
            record.setdefault("deleted_for", set()).add(username)

            offline_messages[username] = [
                event
                for event in offline_messages.get(username, [])
                if event.get("message_id") != message_id
            ]

            user_connection = clients.get(username)
            connections = [user_connection] if user_connection else []

            delete_event = {
                "type": "message_delete",
                "message_id": message_id,
                "sender": sender,
                "scope": "me"
            }

    for recipient_connection in connections:
        send_event(recipient_connection, delete_event)

    if scope == "everyone":
        send_text(connection, f"Message {message_id[:8]} deleted for everyone.")
    else:
        send_text(connection, f"Message {message_id[:8]} deleted for you.")


def handle_delivery(username, message_id):
    if not message_id:
        return

    remove_queued_message(username, message_id)

    with clients_lock:
        pending = pending_messages.get(message_id)

        if not pending or username not in pending["recipients"]:
            return

        pending["recipients"].discard(username)
        sender = pending["sender"]

        if pending["recipients"]:
            return

        pending_messages.pop(message_id, None)

    send_status(sender, message_id, "delivered")


def handle_command(connection, username, command):
    if command.startswith("/edit "):
        handle_edit(connection, username, command)
        return

    if command.startswith("/delete "):
        handle_delete(connection, username, command)
        return

    if group_management(connection, username, command):
        return

    if command == "/contacts":
        with clients_lock:
            online_users = [
                name for name in clients if name != username
            ]

        send_text(
            connection,
            "Online users: " + ", ".join(online_users)
            if online_users else "No other users are online."
        )
        return

    if command.startswith("/group-create "):
        parts = command.split(maxsplit=2)

        if len(parts) < 3:
            send_text(
                connection,
                "Usage: /group-create name username1,username2"
            )
            return

        group_name = parts[1].strip()
        requested_members = [
            name.strip()
            for name in parts[2].split(",")
            if name.strip()
        ]
        group_key = group_name.casefold()

        if (
            not group_name
            or len(group_name) > 40
            or not requested_members
        ):
            send_text(
                connection,
                "Provide a group name (max 40 characters) and members."
            )
            return

        if len(set(requested_members)) != len(requested_members):
            send_text(connection, "The member list contains duplicates.")
            return

        if username in requested_members:
            send_text(
                connection,
                "Do not include your own username; you are added automatically."
            )
            return

        with clients_lock:
            if group_key in groups:
                send_text(connection, "That group name already exists.")
                return

            offline = [
                name for name in requested_members
                if name not in clients
            ]

            if offline:
                send_text(
                    connection,
                    "These users are not online: " + ", ".join(offline)
                )
                return

            members = set(requested_members)
            members.add(username)

            groups[group_key] = {
                "name": group_name,
                "creator": username,
                "members": members,
                "admins": {username},
                "icon": "👥"
            }

            for member in members:
                user_groups.setdefault(member, set()).add(group_key)

            member_connections = {
                member: clients.get(member)
                for member in members if member != username
            }

        send_text(
            connection,
            f"Group '{group_name}' created. You are the admin. "
            f"Use /group {group_name} to chat."
        )

        for member, member_connection in member_connections.items():
            if member_connection:
                send_text(
                    member_connection,
                    f"You were added to group '{group_name}' by {username}."
                )
        return

    if command == "/groups":
        with clients_lock:
            names = []

            for key in user_groups.get(username, set()):
                group = groups.get(key)

                if group:
                    role = (
                        " (admin)"
                        if username in group["admins"]
                        else ""
                    )
                    names.append(
                        f"{group.get('icon', '👥')} "
                        f"{group['name']}{role}"
                    )

        send_text(
            connection,
            "Your groups: " + ", ".join(names)
            if names else "You are not in any groups."
        )
        return

    if command.startswith("/group "):
        group_name = command[len("/group "):].strip()
        group_key = group_name.casefold()

        with clients_lock:
            group = groups.get(group_key)
            is_member = bool(
                group and username in group["members"]
            )

            if is_member:
                active_groups[username] = group_key
                target = active_chats.pop(username, None)

                if target and active_chats.get(target) == username:
                    active_chats.pop(target, None)

        if not is_member:
            send_text(
                connection,
                f"You are not a member of group '{group_name}'."
            )
        else:
            send_text(
                connection,
                f"Switched to group chat "
                f"'{group['icon']} {group['name']}'."
            )
        return

    if command.startswith("/chat "):
        target = command[len("/chat "):].strip()

        if not target or target == username:
            send_text(connection, "Enter another user's username.")
            return

        # Accounts now come from registration, so the target must exist.
        if not user_exists(target):
            send_text(connection, f"User '{target}' is not registered.")
            return

        with clients_lock:
            active_groups.pop(username, None)
            previous = active_chats.get(username)

            if previous and active_chats.get(previous) == username:
                active_chats.pop(previous, None)

            active_chats[username] = target
            active_chats[target] = username
            target_connection = clients.get(target)

        send_text(connection, f"Private chat selected with {target}.")

        if target_connection:
            send_text(
                target_connection,
                f"Private chat started with {username}."
            )
        return

    if command == "/back":
        with clients_lock:
            group_key = active_groups.pop(username, None)

            if group_key:
                group = groups.get(group_key)
                group_name = group["name"] if group else group_key
                send_text(
                    connection,
                    f"You left the active group chat '{group_name}'."
                )
                return

            target = active_chats.pop(username, None)

            if target and active_chats.get(target) == username:
                active_chats.pop(target, None)
                target_connection = clients.get(target)
            else:
                target_connection = None

        send_text(connection, "You left the private chat.")

        if target_connection:
            send_text(
                target_connection,
                f"{username} left the private chat."
            )
        return

    if command == "/quit":
        raise ConnectionAbortedError

    send_text(
        connection,
        "Unknown command. Use /help or /group-help."
    )


def handle_client(connection, address):
    username = None
    reader = None
    session_token = None
    owns_session = False
    last_refresh = time.time()

    print(f"Connection received: {address}")

    try:
        reader = connection.makefile("r", encoding="utf-8")

        # --- Registration / login phase ---
        connection.settimeout(AUTH_TIMEOUT_SECONDS)
        username, session_token, resumed = authenticate_connection(connection, reader, address)
        last_refresh = time.time()

        if not username:
            return

        stale_connection = None

        with clients_lock:
            if username in clients:
                if not resumed:
                    send_text(connection, "ERROR: Username already in use.")
                    username = None  # do not clean up the other session's state
                    return

                # A valid saved session was resumed (e.g. the app was closed and
                # reopened before the server noticed the old socket closing).
                # The old connection belongs to the same session, so replace it.
                stale_connection = clients[username]

            clients[username] = connection
            owns_session = True
            user_groups.setdefault(username, set())
            offline_messages.setdefault(username, [])

        if stale_connection is not None:
            send_text(
                stale_connection,
                "This session was resumed from another connection."
            )
            try:
                stale_connection.shutdown(socket.SHUT_RDWR)
            except OSError:
                pass

        # Idle timeout: the session ends after SESSION_TIMEOUT_MINUTES without activity.
        connection.settimeout(SESSION_TIMEOUT_MINUTES * 60)

        send_text(connection, f"Welcome, {username}!")
        print(f"{username} is now online.")

        with clients_lock:
            queued = [
                dict(event)
                for event in offline_messages.get(username, [])
                if not message_store.get(
                    event.get("message_id"), {}
                ).get("deleted")
                and username not in message_store.get(
                    event.get("message_id"), {}
                ).get("deleted_for", set())
            ]

        for event in queued:
            if not send_event(connection, event):
                break

        while True:
            line = reader.readline()

            if not line:
                break

            try:
                event = json.loads(line)
            except json.JSONDecodeError:
                send_text(connection, "Invalid message format.")
                continue

            event_type = event.get("type")

            # Sliding session: keep the client's saved token fresh while it is active.
            if time.time() - last_refresh >= SESSION_REFRESH_SECONDS:
                last_refresh = time.time()
                try:
                    renewed = refresh_session_token(session_token)
                    session_token = renewed["token"]
                    send_event(connection, {
                        "type": "session",
                        "token": renewed["token"],
                        "expires_in": renewed["expires_in"]
                    })
                except TokenError:
                    send_text(
                        connection,
                        "Session expired. Please log in again."
                    )
                    break

            if event_type == "chat":
                handle_chat(username, event)

            elif event_type == "delivered":
                handle_delivery(username, event.get("message_id"))

            elif event_type in ("typing", "stop_typing"):
                with clients_lock:
                    target = active_chats.get(username)
                    target_connection = (
                        clients.get(target) if target else None
                    )

                if target_connection:
                    send_event(target_connection, {
                        "type": event_type,
                        "username": username
                    })

            elif event_type == "command":
                if event.get("message", "").strip() == "/logout":
                    # Explicit logout ends the saved session as well.
                    revoke_session(session_token)
                    send_event(connection, {
                        "type": "logged_out",
                        "message": "You have been logged out."
                    })
                    break

                try:
                    handle_command(
                        connection,
                        username,
                        event.get("message", "").strip()
                    )
                except ConnectionAbortedError:
                    break

    except socket.timeout:
        if username:
            send_text(
                connection,
                "Session expired due to inactivity. Please log in again."
            )
        else:
            send_text(connection, "Login timed out. Please reconnect.")
        print(f"Timed out: {username or address}")

    except (ConnectionResetError, OSError, ValueError) as error:
        print(f"Connection error for {username or address}: {error}")

    finally:
        with clients_lock:
            # A connection replaced by a resumed session no longer owns the user's state.
            owns_session = bool(username) and clients.get(username) is connection

            if owns_session:
                del clients[username]

            if owns_session:
                target = active_chats.pop(username, None)

                if target and active_chats.get(target) == username:
                    active_chats.pop(target, None)

                active_groups.pop(username, None)

        if owns_session:
            try:
                record_event(username, "logout", True, address[0] if address else None)
            except Exception as error:
                print(f"Could not log logout for {username}: {error}")

        if reader:
            try:
                reader.close()
            except OSError:
                pass

        try:
            connection.close()
        except OSError:
            pass

        print(f"Disconnected: {address}")


def start_server():
    init_db()

    server = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    server.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    server.bind((HOST, PORT))
    server.listen()

    print(f"Chat server listening on {HOST}:{PORT}")

    try:
        while True:
            connection, address = server.accept()
            threading.Thread(
                target=handle_client,
                args=(connection, address),
                daemon=True
            ).start()

    except KeyboardInterrupt:
        print("\nServer stopped.")

    finally:
        server.close()


if __name__ == "__main__":
    start_server()