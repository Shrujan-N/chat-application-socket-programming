
import socket
import threading

HOST = "127.0.0.1"
PORT = 5000


def receive_messages(client):
    reader = client.makefile("r", encoding="utf-8")

    try:
        while True:
            message = reader.readline()

            if not message:
                print("\nDisconnected from the server.")
                break

            print(f"\n{message.strip()}")
            print("You: ", end="", flush=True)
    except OSError:
        pass
    finally:
        reader.close()


def start_client():
    client = socket.socket(socket.AF_INET, socket.SOCK_STREAM)

    try:
        client.connect((HOST, PORT))
        print("Connected to the chat server.")

        reader = client.makefile("r", encoding="utf-8")
        prompt = reader.readline()
        print(prompt.strip())

        username = input("Username: ").strip()

        if not username:
            print("Username cannot be empty.")
            return

        client.sendall((username + "\n").encode("utf-8"))

        response = reader.readline().strip()
        print(response)

        if response.startswith("ERROR:"):
            return

        receiver = threading.Thread(
            target=receive_messages,
            args=(client,),
            daemon=True
        )
        receiver.start()

        print("\nCommands:")
        print("  /contacts       - View online users")
        print("  /chat username  - Initiate a private chat")
        print("  /quit           - Disconnect")

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
        client.close()
        print("Disconnected from the chat server.")


if __name__ == "__main__":
    start_client()
