import base64
import getpass
import json
import os
import socket
import threading
import uuid

from prompt_toolkit import PromptSession

HOST = "127.0.0.1"
PORT = 5000

pending_status = {}
status_lock = threading.Lock()
send_lock = threading.Lock()
typing_lock = threading.Lock()
typing_timer = None
is_typing = False
session_token = None
session_user = None

# FR-03: the session is saved here so closing and reopening the client within
# the session validity window returns the user to their account.
SESSION_FILE = os.environ.get(
    "CHAT_SESSION_FILE",
    os.path.join(os.path.dirname(os.path.abspath(__file__)), ".chat_session")
)
session_file_lock = threading.Lock()

# FR-05: largest profile picture the server accepts (checked here too so a huge
# file is never read or sent).
MAX_AVATAR_BYTES = int(os.environ.get("MAX_AVATAR_BYTES", str(2 * 1024 * 1024)))


def save_session(username, token):
    """Remember the session token on disk (owner-only permissions)."""
    global session_token, session_user

    session_token = token
    session_user = username

    if not username or not token:
        return

    try:
        with session_file_lock:
            fd = os.open(SESSION_FILE, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
            with os.fdopen(fd, "w", encoding="utf-8") as f:
                json.dump({"username": username, "token": token}, f)
    except OSError:
        pass  # not being able to save only means the user logs in next time


def load_session():
    try:
        with open(SESSION_FILE, "r", encoding="utf-8") as f:
            data = json.load(f)
        if data.get("token") and data.get("username"):
            return data
    except (OSError, ValueError, AttributeError):
        pass
    return None


def clear_session():
    """Forget the saved session (logout, or the server rejected it)."""
    global session_token, session_user

    session_token = None
    session_user = None

    try:
        with session_file_lock:
            os.remove(SESSION_FILE)
    except OSError:
        pass


def send_event(client, event):
    data = json.dumps(event) + "\n"

    with send_lock:
        client.sendall(data.encode("utf-8"))


def stop_typing(client):
    global is_typing, typing_timer

    with typing_lock:
        if typing_timer is not None:
            typing_timer.cancel()
            typing_timer = None

        if is_typing:
            is_typing = False

            try:
                send_event(client, {"type": "stop_typing"})
            except OSError:
                pass


def typing_timeout(client):
    global is_typing, typing_timer

    with typing_lock:
        typing_timer = None

        if is_typing:
            is_typing = False

            try:
                send_event(client, {"type": "stop_typing"})
            except OSError:
                pass


def handle_text_change(buffer, client):
    global is_typing, typing_timer

    current_text = buffer.text

    with typing_lock:
        if typing_timer is not None:
            typing_timer.cancel()
            typing_timer = None

        if not current_text or current_text.startswith("/"):
            if is_typing:
                is_typing = False

                try:
                    send_event(client, {"type": "stop_typing"})
                except OSError:
                    pass

            return

        if not is_typing:
            is_typing = True

            try:
                send_event(client, {"type": "typing"})
            except OSError:
                is_typing = False
                return

        typing_timer = threading.Timer(
            1.5,
            typing_timeout,
            args=(client,)
        )
        typing_timer.daemon = True
        typing_timer.start()


def receive_messages(reader, client, stop_event):
    try:
        while not stop_event.is_set():
            line = reader.readline()

            if not line:
                if not stop_event.is_set():
                    print("\nDisconnected from the server.")
                break

            try:
                event = json.loads(line)
            except json.JSONDecodeError:
                print(f"\n{line.strip()}")
                continue

            event_type = event.get("type")

            if event_type == "text":
                print(f"\n{event.get('message', '')}")

            elif event_type in ("private_message", "group_message"):
                message_id = event.get("message_id")
                sender = event.get("sender", "Unknown")
                message = event.get("message", "")
                edited_label = " [EDITED]" if event.get("edited") else ""

                if event_type == "group_message":
                    group_name = event.get("group", "Unknown")
                    icon = event.get("icon", "👥")

                    print(
                        f"\n[{icon} Group: {group_name}] "
                        f"{sender}: {message}{edited_label}"
                    )
                else:
                    print(
                        f"\n[Private] {sender}: "
                        f"{message}{edited_label}"
                    )

                if message_id:
                    try:
                        send_event(client, {
                            "type": "delivered",
                            "message_id": message_id
                        })
                    except OSError:
                        pass

            elif event_type == "session":
                # Sliding session: server issued a fresh token while we are active.
                save_session(session_user, event.get("token"))

            elif event_type == "logged_out":
                clear_session()
                print(f"\n{event.get('message', 'You have been logged out.')}")
                break

            elif event_type == "profile":
                # Result of one of your own profile changes.
                profile = event.get("profile", {})
                print(
                    f"\n{event.get('message', 'Profile updated.')}\n"
                    f"Display name: {profile.get('display_name', '')}\n"
                    f"Status: {profile.get('bio') or '(empty)'}\n"
                    f"Picture: {'set' if profile.get('avatar_url') else 'none'}"
                )

            elif event_type == "profile_update":
                # Someone you can see changed their profile.
                profile = event.get("profile", {})
                print(
                    f"\n[Profile] {profile.get('username', 'Someone')} updated their profile: "
                    f"{profile.get('display_name', '')} - "
                    f"{profile.get('bio') or '(no status)'}"
                    f"{' [picture set]' if profile.get('avatar_url') else ''}"
                )

            elif event_type == "message_edit":
                message_id = event.get("message_id", "")
                sender = event.get("sender", "Unknown")
                message = event.get("message", "")

                print(
                    f"\n[EDITED] {sender}: {message} "
                    f"(ID: {message_id[:8]})"
                )

            elif event_type == "message_delete":
                message_id = event.get("message_id", "")
                scope = event.get("scope", "me")

                if scope == "everyone":
                    print(
                        f"\n[DELETED] Message {message_id[:8]} "
                        "was deleted for everyone."
                    )
                else:
                    print(
                        f"\n[DELETED] Message {message_id[:8]} "
                        "was removed from your view."
                    )

            elif event_type == "typing":
                print(
                    f"\n{event.get('username', 'Someone')} is typing..."
                )

            elif event_type == "stop_typing":
                print(
                    f"\n{event.get('username', 'Someone')} "
                    "stopped typing."
                )

            elif event_type == "status":
                message_id = event.get("message_id")
                status = event.get("status", "unknown")

                if message_id:
                    with status_lock:
                        pending_status[message_id] = status

                    short_id = message_id[:8]
                else:
                    short_id = "Unknown"

                if status == "sent":
                    print(f"\nMessage {short_id}: Sent ✓")

                elif status == "delivered":
                    print(f"\nMessage {short_id}: Delivered ✓✓")

                elif status == "failed":
                    print(
                        f"\nMessage {short_id}: Failed — "
                        f"{event.get('message', 'Delivery failed')}"
                    )

            else:
                print(f"\nUnknown server event: {event}")

    except (OSError, ValueError):
        pass


def is_command(message):
    exact_commands = {
        "/contacts",
        "/back",
        "/groups",
        "/quit",
        "/logout",
        "/help",
        "/group-help",
        "/profile"
    }

    prefixes = (
        "/profile ",
        "/edit ",
        "/delete ",
        "/chat ",
        "/group ",
        "/group-create ",
        "/group-info ",
        "/group-add ",
        "/group-remove ",
        "/group-rename ",
        "/group-icon ",
        "/group-admin ",
        "/group-admin-remove "
    )

    return message in exact_commands or message.startswith(prefixes)


def print_help():
    print("\n--- Chat Commands ---")
    print("/contacts                         View online users")
    print("/chat username                    Start a private chat")
    print("/group-create name user1,user2    Create a group")
    print("/groups                           List your groups")
    print("/group name                       Enter a group")
    print("/back                             Leave the active chat")
    print("/edit message_id new content      Edit your own message")
    print("/delete message_id me             Delete a message from your view")
    print("/delete message_id everyone       Delete your message for everyone")
    print("/profile                          View your profile")
    print("/profile view username            View someone's profile")
    print("/profile name new display name    Change your display name")
    print("/profile bio your status          Change your status/bio")
    print("/profile clear-bio                Clear your status")
    print("/profile-avatar path/to/image     Set your profile picture (PNG/JPEG/GIF/WebP)")
    print("/profile remove-avatar            Remove your profile picture")
    print("/help                             Show commands")
    print("/quit                             Disconnect (you stay logged in)")
    print("/logout                           Log out and forget the saved session")

    print("\n--- Group Management ---")
    print("/group-info name                  View group details")
    print("/group-add name username          Add a member")
    print("/group-remove name username       Remove a member")
    print("/group-rename oldname newname     Rename a group")
    print("/group-icon name emoji            Change group icon")
    print("/group-admin name username        Make a member an admin")
    print("/group-admin-remove name username Remove an admin role")
    print("/group-help                       Show group management help")


def read_event(reader):
    """Read one JSON line from the server; None means the connection closed."""
    line = reader.readline()

    if not line:
        return None

    try:
        return json.loads(line)
    except json.JSONDecodeError:
        return {"type": "text", "message": line.strip()}


def try_resume(client, reader):
    """
    Use the saved session token to return to the account without re-entering
    credentials. Returns the username, or None if there is no valid session
    (the saved session is then discarded and the normal login menu is shown).
    """
    saved = load_session()

    if not saved:
        return None

    try:
        send_event(client, {"type": "resume", "token": saved["token"]})
    except OSError:
        return None

    result = read_event(reader)

    if result is None:
        return None

    if (
        result.get("type") == "auth_result"
        and result.get("success")
        and result.get("action") == "resume"
    ):
        save_session(result.get("username"), result.get("token"))
        print(f"Welcome back, {result.get('username')}! Your session was restored.")
        return result.get("username")

    clear_session()
    print(
        f"Saved session is no longer valid ({result.get('message', 'expired')}). "
        "Please log in."
    )
    return None


def authenticate(client, reader):
    """
    Registration / login menu. Registering returns to the menu so the user
    logs in next. Returns the username once logged in, or None to exit.
    """
    global session_token

    while True:
        print("\n1) Log in")
        print("2) Register")
        print("3) Quit")
        print("4) Forgot password")
        print("5) Reset password (I have a reset code)")

        try:
            choice = input("Choose an option: ").strip().lower()
        except (EOFError, KeyboardInterrupt):
            choice = "3"

        if choice in ("3", "q", "quit"):
            try:
                send_event(client, {"type": "quit"})
            except OSError:
                pass
            return None

        if choice in ("2", "register"):
            username = input("Username: ").strip()
            email = input("Email: ").strip()
            password = getpass.getpass("Password: ")
            send_event(client, {
                "type": "register",
                "username": username,
                "email": email,
                "password": password
            })

        elif choice in ("4", "forgot", "forgot password"):
            email = input("Registered email: ").strip()
            send_event(client, {
                "type": "forgot_password",
                "email": email
            })

        elif choice in ("5", "reset", "reset password"):
            token = input("Reset code from the email: ").strip()
            new_password = getpass.getpass("New password: ")
            confirm = getpass.getpass("Confirm new password: ")

            if new_password != confirm:
                print("Passwords do not match.")
                continue

            send_event(client, {
                "type": "reset_password",
                "token": token,
                "new_password": new_password
            })

        elif choice in ("1", "login", "log in"):
            username = input("Username: ").strip()
            password = getpass.getpass("Password: ")
            send_event(client, {
                "type": "login",
                "username": username,
                "password": password
            })

        else:
            print("Invalid option. Enter 1, 2, 3, 4 or 5.")
            continue

        result = read_event(reader)

        if result is None:
            print("The server closed the connection.")
            return None

        print(result.get("message", result))

        if (
            result.get("type") == "auth_result"
            and result.get("success")
            and result.get("action") == "login"
        ):
            save_session(result.get("username"), result.get("token"))
            return result.get("username")


def send_avatar(client, path_text):
    """Read a local image and send it to the server as the profile picture."""
    path = os.path.expanduser(path_text.strip().strip('"').strip("'"))

    if not path:
        print("Usage: /profile-avatar path/to/image")
        return

    try:
        size = os.path.getsize(path)
    except OSError:
        print(f"Profile not changed: cannot read '{path}'.")
        return

    if size > MAX_AVATAR_BYTES:
        print(
            f"Profile not changed: image is too large ({size / (1024 * 1024):.1f} MB). "
            f"The maximum size is {MAX_AVATAR_BYTES / (1024 * 1024):.0f} MB."
        )
        return

    try:
        with open(path, "rb") as f:
            data = f.read()
    except OSError:
        print(f"Profile not changed: cannot read '{path}'.")
        return

    try:
        send_event(client, {
            "type": "profile_avatar",
            "data": base64.b64encode(data).decode("ascii")
        })
    except OSError:
        print("Could not send the picture.")


def start_client():
    client = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    reader = None
    stop_event = threading.Event()

    try:
        client.connect((HOST, PORT))
        reader = client.makefile("r", encoding="utf-8")

        print("Connected to the chat server.")

        initial_line = reader.readline()

        if not initial_line:
            print("The server closed the connection.")
            return

        initial_response = json.loads(initial_line)
        print(initial_response.get("message", initial_response))

        username = try_resume(client, reader)

        if not username:
            username = authenticate(client, reader)

        if not username:
            return

        # The server sends "Welcome, <name>!" or "ERROR: ..." next.
        response_line = reader.readline()

        if not response_line:
            print("The server closed the connection.")
            return

        response = json.loads(response_line)
        response_message = response.get("message", response)
        print(response_message)

        if (
            isinstance(response_message, str)
            and response_message.startswith("ERROR:")
        ):
            return

        session = PromptSession()
        session.default_buffer.on_text_changed += (
            lambda buffer: handle_text_change(buffer, client)
        )

        receiver = threading.Thread(
            target=receive_messages,
            args=(reader, client, stop_event),
            daemon=True
        )
        receiver.start()

        print_help()
        print(
            "\nOffline messages are queued in server memory "
            "until acknowledged."
        )

        while True:
            try:
                message = session.prompt("You: ").strip()
            except (EOFError, KeyboardInterrupt):
                message = "/quit"

            stop_typing(client)

            if message.lower() == "/quit":
                try:
                    send_event(client, {
                        "type": "command",
                        "message": "/quit"
                    })
                except OSError:
                    pass

                break

            if message.lower() == "/logout":
                try:
                    send_event(client, {
                        "type": "command",
                        "message": "/logout"
                    })
                except OSError:
                    pass

                clear_session()
                print("You have been logged out.")
                break

            if message == "/help":
                print_help()
                continue

            if message.lower() == "/profile-avatar" or message.lower().startswith("/profile-avatar "):
                send_avatar(client, message[len("/profile-avatar"):])
                continue

            if is_command(message):
                try:
                    send_event(client, {
                        "type": "command",
                        "message": message
                    })
                except OSError:
                    print("Could not send command.")
                    break

            elif message:
                message_id = str(uuid.uuid4())

                with status_lock:
                    pending_status[message_id] = "sending"

                print(f"Message ID: {message_id}")
                print("Sending message...")

                try:
                    send_event(client, {
                        "type": "chat",
                        "message_id": message_id,
                        "message": message
                    })
                except OSError:
                    print("Could not send message.")
                    break

    except ConnectionRefusedError:
        print("Could not connect. Start the server first.")

    except (ConnectionResetError, OSError, ValueError) as error:
        print(f"Connection error: {error}")

    finally:
        stop_event.set()
        stop_typing(client)

        try:
            client.shutdown(socket.SHUT_RDWR)
        except OSError:
            pass

        try:
            client.close()
        except OSError:
            pass

        if reader is not None:
            try:
                reader.close()
            except OSError:
                pass

        print("Disconnected from the chat server.")


if __name__ == "__main__":
    start_client()