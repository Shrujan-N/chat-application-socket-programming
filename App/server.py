
import socket
import threading

HOST = "127.0.0.1"
PORT = 5000

clients = {}
active_chats = {}
clients_lock = threading.Lock()


def send_message(connection, message):
    try:
        connection.sendall((message + "\n").encode("utf-8"))
        return True
    except OSError:
        return False


def handle_client(connection, address):
    username = None
    reader = None

    print(f"Connection received: {address}")

    try:
        reader = connection.makefile("r", encoding="utf-8")
        send_message(connection, "Enter your username:")

        username = reader.readline().strip()
        if not username:
            return

        with clients_lock:
            if username in clients:
                send_message(connection, "ERROR: Username already in use.")
                return
            clients[username] = connection

        send_message(connection, f"Welcome, {username}!")
        print(f"{username} is now online.")

        while True:
            message = reader.readline()
            if not message:
                break

            message = message.strip()
            if not message:
                continue

            if message == "/contacts":
                with clients_lock:
                    online_users = [
                        name for name in clients if name != username
                    ]
                send_message(
                    connection,
                    "Online users: " + ", ".join(online_users)
                    if online_users else "No other users are online."
                )

            elif message.startswith("/chat "):
                target = message[len("/chat "):].strip()

                if target == username:
                    send_message(connection, "You cannot chat with yourself.")
                    continue

                with clients_lock:
                    target_connection = clients.get(target)
                    if target_connection:
                        active_chats[username] = target
                        active_chats[target] = username

                if not target_connection:
                    send_message(connection, f"{target} is not online.")
                else:
                    send_message(
                        connection,
                        f"Private chat started with {target}."
                    )
                    send_message(
                        target_connection,
                        f"Private chat started with {username}. "
                        "You can now reply directly."
                    )

            elif message == "/back":
                with clients_lock:
                    target = active_chats.pop(username, None)
                    if target and active_chats.get(target) == username:
                        active_chats.pop(target, None)

                send_message(connection, "You left the private chat.")
                if target:
                    with clients_lock:
                        target_connection = clients.get(target)
                    if target_connection:
                        send_message(
                            target_connection,
                            f"{username} left the private chat."
                        )

            elif message == "/quit":
                break

            else:
                with clients_lock:
                    target = active_chats.get(username)
                    target_connection = clients.get(target) if target else None

                if not target:
                    send_message(
                        connection,
                        "No active private chat. Use /contacts, then /chat username."
                    )
                elif not target_connection:
                    send_message(connection, f"{target} is no longer online.")
                else:
                    delivered = send_message(
                        target_connection,
                        f"[Private] {username}: {message}"
                    )
                    if delivered:
                        send_message(connection, f"[Private] You: {message}")
                    else:
                        send_message(
                            connection,
                            f"Could not send the message to {target}."
                        )

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
