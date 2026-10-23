
import re
import bcrypt
from db import get_connection
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


def validate_registration(email, username, password):
    email = email.strip()
    username = username.strip()

    if not email or not username or not password:
        return False, "All fields are required."

    if not re.fullmatch(r"[^@\s]+@[^@\s]+\.[^@\s]+", email):
        return False, "Please enter a valid email address."

    if len(username) < 3 or len(username) > 50:
        return False, "Username must be 3–50 characters long."

    # Password policy: at least 8 characters, one uppercase,
    # one lowercase, one digit and one special character.
    if (
        len(password) < 8
        or not re.search(r"[A-Z]", password)
        or not re.search(r"[a-z]", password)
        or not re.search(r"\d", password)
        or not re.search(r"[^A-Za-z0-9]", password)
    ):
        return False, (
            "Password must be at least 8 characters and include "
            "uppercase, lowercase, a number and a special character."
        )

    return True, "Registration details are valid."


def register_user(email, username, password):
    valid, message = validate_registration(email, username, password)

    if not valid:
        return False, message

    password_hash = bcrypt.hashpw(
        password.encode("utf-8"),
        bcrypt.gensalt()
    ).decode("utf-8")

    connection = None

    try:
        connection = get_connection()
        cursor = connection.cursor()

        cursor.execute(
            """
            INSERT INTO users (email, username, password_hash)
            VALUES (%s, %s, %s)
            """,
            (email.strip(), username.strip(), password_hash)
        )

        connection.commit()
        return True, "Account created successfully! You can now log in."

    except Exception as error:
        if connection:
            connection.rollback()

        # MySQL duplicate-key error.
        if getattr(error, "errno", None) == 1062:
            return False, "Email or username is already registered."

        print("Registration database error:", error)
        return False, "Registration failed due to a server error."

    finally:
        if connection:
            connection.close()


def login_user(username, password):
    connection = None

    try:
        connection = get_connection()
        cursor = connection.cursor(dictionary=True)

        cursor.execute(
            """
            SELECT username, password_hash
            FROM users
            WHERE username = %s
            """,
            (username.strip(),)
        )

        user = cursor.fetchone()

        if not user:
            return False, "Invalid username or password."

        password_matches = bcrypt.checkpw(
            password.encode("utf-8"),
            user["password_hash"].encode("utf-8")
        )

        if not password_matches:
            return False, "Invalid username or password."

        return True, "Login successful!"

    except Exception as error:
        print("Login database error:", error)
        return False, "Login failed due to a server error."

    finally:
        if connection:
            connection.close()




def handle_client(connection, address):
    username = None
    reader = None

    print(f"Connection received: {address}")

    try:        
        reader = connection.makefile("r", encoding="utf-8")

        # Receive the registration or login request
        auth_line = reader.readline()

        if not auth_line:
            return

        try:
            auth_event = json.loads(auth_line)
        except json.JSONDecodeError:
            send_event(connection, {
                "type": "auth_result",
                "success": False,
                "message": "Invalid authentication request."
            })
            return

        auth_type = auth_event.get("type")

        # Handle registration
        if auth_type == "register":
            success, message = register_user(
                auth_event.get("email", ""),
                auth_event.get("username", ""),
                auth_event.get("password", "")
            )

            send_event(connection, {
                "type": "auth_result",
                "success": success,
                "message": message
            })
            return

        # Handle login
        if auth_type != "login":
            send_event(connection, {
                "type": "auth_result",
                "success": False,
                "message": "Please choose Register or Login."
            })
            return

        username = auth_event.get("username", "").strip()
        password = auth_event.get("password", "")

        if not username or not password:
            send_event(connection, {
                "type": "auth_result",
                "success": False,
                "message": "Username and password are required."
            })
            return

        success, message = login_user(username, password)

        if not success:
            send_event(connection, {
                "type": "auth_result",
                "success": False,
                "message": message
            })
            return

        # Prevent two connections using the same online username
        with clients_lock:
            if username in clients:
                already_online = True
            else:
                clients[username] = connection
                already_online = False

        if already_online:
            send_event(connection, {
                "type": "auth_result",
                "success": False,
                "message": "This user is already online."
            })
            return

        send_event(connection, {
            "type": "auth_result",
            "success": True,
            "message": f"Welcome, {username}!"
        })

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

                if not target or not target_connection:
                    send_event(connection, {
                        "type": "status",
                        "message_id": event.get("message_id"),
                        "status": "failed",
                        "message": "No active private chat."
                    })
                    continue

                message_id = event.get("message_id") or str(uuid.uuid4())

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
