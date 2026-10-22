
import socket
import threading

HOST = "127.0.0.1"
PORT = 5000


def handle_client(connection, address):
    print(f"Connected: {address}")

    try:
        while True:
            data = connection.recv(1024)

            if not data:
                break

            print(f"Received from {address}: {data.decode('utf-8')}")
    except (ConnectionResetError, OSError):
        pass
    finally:
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
