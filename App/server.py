
import json
import socket
import threading
import uuid

HOST = "127.0.0.1"
PORT = 5000

clients = {}
active_chats = {}
pending_messages = {}
clients_lock = threading.Lock()


def send_event(connection, event):
    try:
        data = json.dumps(event) + "\n"
        connection.sendall(data.encode("utf-8"))
        return True
    except OSError:
        return False


def send_text(connection, message):
    return send_event(connection, {"type": "text", "message": message})


def handle_client(connection, address):
    username = None
    reader = None

    print(f"Connection received: {address}")

    try:
        reader = connection.makefile("r", encoding="utf-8")
        send_text(connection, "Enter your username:")

        username = reader.readline().strip()
        if not username:
            return

        with clients_lock:
            if username in clients:
                send_text(connection, "ERROR: Username already in use.")
                return
            clients[username] = connection

        send_text(connection, f"Welcome, {username}!")
        print(f"{username} is now online.")

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

            if event_type == "chat":
                message = event.get("message", "").strip()
                if not message:
                    continue

                with clients_lock:
                    target = active_chats.get(username)
                    target_connection = clients.get(target) if target else None

                message_id = event.get("message_id") or str(uuid.uuid4())

                if not target or not target_connection:
                    send_event(connection, {
                        "type": "status",
                        "message_id": message_id,
                        "status": "failed",
                        "message": "No active private chat."
                    })
                    continue

                with clients_lock:
                    pending_messages[message_id] = {
                        "sender": username,
                        "recipient": target
                    }

                delivered_to_socket = send_event(target_connection, {
                    "type": "private_message",
                    "message_id": message_id,
                    "sender": username,
                    "message": message
                })

                if delivered_to_socket:
                    send_event(connection, {
                        "type": "status",
                        "message_id": message_id,
                        "status": "sent"
                    })
                else:
                    with clients_lock:
                        pending_messages.pop(message_id, None)

                    send_event(connection, {
                        "type": "status",
                        "message_id": message_id,
                        "status": "failed",
                        "message": "Could not send the message."
                    })

            elif event_type == "delivered":
                message_id = event.get("message_id")

                with clients_lock:
                    pending = pending_messages.get(message_id)

                    if pending and pending["recipient"] == username:
                        sender_connection = clients.get(pending["sender"])
                        pending_messages.pop(message_id, None)
                    else:
                        sender_connection = None

                if sender_connection:
                    send_event(sender_connection, {
                        "type": "status",
                        "message_id": message_id,
                        "status": "delivered"
                    })

            elif event_type in ("typing", "stop_typing"):
                with clients_lock:
                    target = active_chats.get(username)
                    target_connection = clients.get(target) if target else None

                if target_connection:
                    send_event(target_connection, {
                        "type": event_type,
                        "username": username
                    })

            elif event_type == "command":
                command = event.get("message", "").strip()

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

                elif command.startswith("/chat "):
                    target = command[len("/chat "):].strip()

                    if target == username:
                        send_text(connection, "You cannot chat with yourself.")
                        continue

                    with clients_lock:
                        target_connection = clients.get(target)

                        if target_connection:
                            active_chats[username] = target
                            active_chats[target] = username

                    if not target_connection:
                        send_text(connection, f"{target} is not online.")
                    else:
                        send_text(connection, f"Private chat started with {target}.")
                        send_text(
                            target_connection,
                            f"Private chat started with {username}. "
                            "You can now reply directly."
                        )

                elif command == "/back":
                    with clients_lock:
                        target = active_chats.pop(username, None)
                        if target and active_chats.get(target) == username:
                            active_chats.pop(target, None)

                    send_text(connection, "You left the private chat.")

                    if target:
                        with clients_lock:
                            target_connection = clients.get(target)

                        if target_connection:
                            send_text(
                                target_connection,
                                f"{username} left the private chat."
                            )

                elif command == "/quit":
                    break

                else:
                    send_text(connection, "Unknown command.")

    except (ConnectionResetError, OSError, ValueError) as error:
        print(f"Connection error for {username or address}: {error}")

    finally:
        with clients_lock:
            if username and clients.get(username) is connection:
                del clients[username]

            if username:
                target = active_chats.pop(username, None)
                if target and active_chats.get(target) == username:
                    active_chats.pop(target, None)

            for message_id, pending in list(pending_messages.items()):
                if username in (pending["sender"], pending["recipient"]):
                    del pending_messages[message_id]

        if reader is not None:
            try:
                reader.close()
            except OSError:
                pass

        connection.close()
        print(f"Disconnected: {address}")


def start_server():
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
