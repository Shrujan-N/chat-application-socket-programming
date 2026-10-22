
import socket
import threading

HOST = "127.0.0.1"
PORT = 5000

clients = {}
clients_lock = threading.Lock()


def send_message(connection, message):
    try:
        connection.sendall((message + "\n").encode("utf-8"))
    except OSError:
        pass


def handle_client(connection, address):
    username = None
    print(f"Connection received: {address}")

    try:
        send_message(connection, "Enter your username:")

        with connection.makefile("r", encoding="utf-8") as reader:
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

                if message == "/contacts":
                    with clients_lock:
                        online_users = [
                            name for name in clients if name != username
                        ]

                    if online_users:
                        send_message(
                            connection,
                            "Online users: " + ", ".join(online_users)
                        )
                    else:
                        send_message(connection, "No other users are online.")

                elif message.startswith("/chat "):
                    target = message[6:].strip()

                    with clients_lock:
                        target_connection = clients.get(target)

                    if target == username:
                        send_message(connection, "You cannot chat with yourself.")
                    elif target_connection:
                        send_message(
                            connection,
                            f"Private chat request sent to {target}."
                        )
                        send_message(
                            target_connection,
                            f"{username} wants to start a private chat with you."
                        )
                    else:
                        send_message(connection, f"{target} is not online.")

                else:
                    send_message(
                        connection,
                        "Unknown command. Use /contacts or /chat username."
                    )

    except (ConnectionResetError, OSError):
        pass
    finally:
        if username:
            with clients_lock:
                if clients.get(username) is connection:
                    del clients[username]
            print(f"{username} went offline.")

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
