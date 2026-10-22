
import socket

HOST = "127.0.0.1"
PORT = 5000


def start_client():
    client = socket.socket(socket.AF_INET, socket.SOCK_STREAM)

    try:
        client.connect((HOST, PORT))
        print("Connected to the chat server.")
    except ConnectionRefusedError:
        print("Could not connect. Start the server first.")
    finally:
        client.close()


if __name__ == "__main__":
    start_client()
