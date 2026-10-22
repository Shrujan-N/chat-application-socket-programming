
import json
import socket
import threading
import uuid

HOST = "127.0.0.1"
PORT = 5000

pending_status = {}
status_lock = threading.Lock()


def send_event(client, event):
    data = json.dumps(event) + "\n"
    client.sendall(data.encode("utf-8"))


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

            elif event_type == "private_message":
                message_id = event.get("message_id")
                sender = event.get("sender", "Unknown")
                message = event.get("message", "")

                print(f"\n[Private] {sender}: {message}")

                send_event(client, {
                    "type": "delivered",
                    "message_id": message_id
                })

            elif event_type == "status":
                message_id = event.get("message_id")
                status = event.get("status", "unknown")

                with status_lock:
                    pending_status[message_id] = status

                if status == "sent":
                    print(f"\nMessage {message_id[:8]}: Sent ✓")
                elif status == "delivered":
                    print(f"\nMessage {message_id[:8]}: Delivered ✓✓")
                elif status == "failed":
                    print(
                        f"\nMessage {message_id[:8]}: Failed — "
                        f"{event.get('message', 'Delivery failed')}"
                    )

            else:
                print(f"\nUnknown server event: {event}")

    except (OSError, ValueError):
        pass


def start_client():
    client = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    reader = None
    stop_event = threading.Event()

    try:
        client.connect((HOST, PORT))
        reader = client.makefile("r", encoding="utf-8")

        print("Connected to the chat server.")
        initial_response = json.loads(reader.readline())
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
        print(response.get("message", response))

        if response.get("message", "").startswith("ERROR:"):
            return

        receiver = threading.Thread(
            target=receive_messages,
            args=(reader, client, stop_event),
            daemon=True
        )
        receiver.start()

        print("\nCommands:")
        print("/contacts - View online users")
        print("/chat username - Start a private chat")
        print("/back - Leave the private chat")
        print("/quit - Disconnect")

        while True:
            message = input("You: ").strip()

            if message.lower() == "/quit":
                send_event(client, {
                    "type": "command",
                    "message": "/quit"
                })
                break

            if message in ("/contacts", "/back") or message.startswith("/chat "):
                send_event(client, {
                    "type": "command",
                    "message": message
                })

            elif message:
                message_id = str(uuid.uuid4())

                with status_lock:
                    pending_status[message_id] = "sending"

                print(f"Message {message_id[:8]}: Sending…")

                send_event(client, {
                    "type": "chat",
                    "message_id": message_id,
                    "message": message
                })

    except ConnectionRefusedError:
        print("Could not connect. Start the server first.")
    except (ConnectionResetError, OSError, ValueError) as error:
        print(f"Connection error: {error}")
    finally:
        stop_event.set()

        try:
            client.shutdown(socket.SHUT_RDWR)
        except OSError:
            pass

        client.close()

        if reader is not None:
            try:
                reader.close()
            except OSError:
                pass

        print("Disconnected from the chat server.")


if __name__ == "__main__":
    start_client()
