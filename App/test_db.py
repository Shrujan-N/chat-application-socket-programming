
from db import get_connection

try:
    connection = get_connection()

    if connection.is_connected():
        print("MySQL connected successfully!")

    connection.close()

except Exception as error:
    print("Connection failed:", error)
