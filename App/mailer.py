"""Outgoing email for password recovery (FR-04).

Configure real delivery with environment variables:
    SMTP_HOST      e.g. smtp.gmail.com        (if unset, emails are printed to the
    SMTP_PORT      587 (STARTTLS) or 465 (SSL)  server console - development mode)
    SMTP_USER      login name
    SMTP_PASSWORD  password / app password
    SMTP_FROM      sender address (defaults to SMTP_USER)
"""
import os
import smtplib
import ssl
from email.message import EmailMessage


def send_email(to: str, subject: str, body: str) -> None:
    host = os.environ.get("SMTP_HOST")

    if not host:
        # Development mode: no mail server configured, show the email in the console.
        print("\n" + "=" * 60)
        print("[DEV EMAIL - set SMTP_HOST to send real email]")
        print(f"To:      {to}")
        print(f"Subject: {subject}")
        print("-" * 60)
        print(body)
        print("=" * 60 + "\n", flush=True)
        return

    port = int(os.environ.get("SMTP_PORT", "587"))
    user = os.environ.get("SMTP_USER", "")
    password = os.environ.get("SMTP_PASSWORD", "")
    sender = os.environ.get("SMTP_FROM") or user or "no-reply@localhost"

    message = EmailMessage()
    message["From"] = sender
    message["To"] = to
    message["Subject"] = subject
    message.set_content(body)

    context = ssl.create_default_context()

    if port == 465:
        with smtplib.SMTP_SSL(host, port, context=context, timeout=15) as smtp:
            if user:
                smtp.login(user, password)
            smtp.send_message(message)
    else:
        with smtplib.SMTP(host, port, timeout=15) as smtp:
            smtp.starttls(context=context)
            if user:
                smtp.login(user, password)
            smtp.send_message(message)


def send_reset_email(to: str, username: str, link: str, token: str, minutes: int) -> None:
    body = (
        f"Hello {username},\n\n"
        "We received a request to reset your chat account password.\n\n"
        f"Open this link to choose a new password (valid for {minutes} minutes, "
        "usable once):\n"
        f"{link}\n\n"
        "Using the command-line client? Choose \"Reset password\" and enter this code:\n"
        f"{token}\n\n"
        "If you did not request this, you can ignore this email - your password "
        "will not change."
    )
    send_email(to, "Reset your chat account password", body)