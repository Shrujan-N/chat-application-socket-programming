
import socket
import threading

HOST = "127.0.0.1"
PORT = 5000


def receive_messages(reader, stop_event):
    try:
        while not stop_event.is_set():
            message = reader.readline()

            if not message:
                if not stop_event.is_set():
                    print("\nDisconnected from the server.")
                break

            print(f"\n{message.strip()}")

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
        print(reader.readline().strip())

        username = input("Username: ").strip()

        if not username:
            print("Username cannot be empty.")
            return

        client.sendall((username + "\n").encode("utf-8"))

        response = reader.readline().strip()
        print(response)

        if response.startswith("ERROR:") or not response:
            return

        receiver = threading.Thread(
            target=receive_messages,
            args=(reader, stop_event),
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
                break

            if message:
                client.sendall((message + "\n").encode("utf-8"))

    except ConnectionRefusedError:
        print("Could not connect. Start the server first.")

    except (ConnectionResetError, OSError) as error:
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
