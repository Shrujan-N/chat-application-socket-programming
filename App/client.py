import json
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

            elif event_type == "message_edit":
                message_id = event.get("message_id", "")
                sender = event.get("sender", "Unknown")
                message = event.get("message", "")

                print(
                    f"\n[EDITED] {sender}: {message} "
                    f"(ID: {message_id[:8]})"
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
        "/help",
        "/group-help"
    }

    prefixes = (
        "/edit ",
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
    print("/help                             Show commands")
    print("/quit                             Disconnect")

    print("\n--- Group Management ---")
    print("/group-info name                  View group details")
    print("/group-add name username          Add a member")
    print("/group-remove name username       Remove a member")
    print("/group-rename oldname newname     Rename a group")
    print("/group-icon name emoji            Change group icon")
    print("/group-admin name username        Make a member an admin")
    print("/group-admin-remove name username Remove an admin role")
    print("/group-help                       Show group management help")


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

        username = input("Username: ").strip()

        if not username:
            print("Username cannot be empty.")
            return

        client.sendall((username + "\n").encode("utf-8"))

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

            if message == "/help":
                print_help()
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