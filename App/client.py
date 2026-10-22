
import socket

HOST = "127.0.0.1"
PORT = 5000


def start_client():
    client = socket.socket(socket.AF_INET, socket.SOCK_STREAM)

    try:
        client.connect((HOST, PORT))
        print("Connected to the chat server.")
        print("Type a message and press Enter. Type /quit to disconnect.")

        while True:
            message = input("You: ")

            if message.strip().lower() == "/quit":
                break

            if message.strip():
                client.sendall(message.encode("utf-8"))

    except ConnectionRefusedError:
        print("Could not connect. Start the server first.")
    except (ConnectionResetError, OSError) as error:
        print(f"Connection error: {error}")
    finally:
        client.close()
        print("Disconnected from the chat server.")


if __name__ == "__main__":
    start_client()
