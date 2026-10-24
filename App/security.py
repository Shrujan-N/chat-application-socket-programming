import base64
import hashlib
import hmac
import json
import os
import secrets
import time

SESSION_TIMEOUT_MINUTES = int(os.environ.get("SESSION_TIMEOUT_MINUTES", "30"))


class TokenError(Exception):
    """Raised when a session token is missing, invalid or expired."""


# ---------------------------------------------------------------- passwords
def hash_password(password: str) -> str:
    salt = secrets.token_hex(16)
    iterations = 310000
    password_hash = hashlib.pbkdf2_hmac(
        "sha256", password.encode("utf-8"), salt.encode("utf-8"), iterations
    ).hex()
    return f"pbkdf2_sha256${iterations}${salt}${password_hash}"


def verify_password(password: str, stored_password: str) -> bool:
    # Support legacy plain-text passwords; login upgrades them to a hash.
    if not stored_password.startswith("pbkdf2_sha256$"):
        return hmac.compare_digest(password, stored_password)
    try:
        _, iterations, salt, expected_hash = stored_password.split("$")
        actual_hash = hashlib.pbkdf2_hmac(
            "sha256", password.encode("utf-8"), salt.encode("utf-8"), int(iterations)
        ).hex()
        return hmac.compare_digest(actual_hash, expected_hash)
    except (ValueError, TypeError):
        return False


# ---------------------------------------------------------------------- JWT
def _load_secret() -> bytes:
    env_value = os.environ.get("JWT_SECRET")
    if env_value:
        return env_value.encode()

    path = os.environ.get("JWT_SECRET_FILE", ".jwt_secret")
    try:
        with open(path, "r", encoding="utf-8") as f:
            value = f.read().strip()
            if value:
                return value.encode()
    except OSError:
        pass

    value = secrets.token_hex(32)
    try:
        with open(path, "w", encoding="utf-8") as f:
            f.write(value)
    except OSError:
        pass
    return value.encode()


SECRET_KEY = _load_secret()


def _b64(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode()


def _unb64(text: str) -> bytes:
    return base64.urlsafe_b64decode(text + "=" * (-len(text) % 4))


def _sign(signing_input: str) -> str:
    return _b64(hmac.new(SECRET_KEY, signing_input.encode(), hashlib.sha256).digest())


def create_token(username: str, ttl_seconds: int = None) -> str:
    """Create an HS256 JWT. ttl_seconds defaults to the session timeout."""
    if ttl_seconds is None:
        ttl_seconds = SESSION_TIMEOUT_MINUTES * 60
    now = int(time.time())
    header = _b64(json.dumps({"alg": "HS256", "typ": "JWT"}).encode())
    body = _b64(json.dumps({"sub": username, "iat": now, "exp": now + ttl_seconds}).encode())
    signing_input = f"{header}.{body}"
    return f"{signing_input}.{_sign(signing_input)}"


def decode_token(token: str) -> dict:
    if not token:
        raise TokenError("Not authenticated")
    try:
        header_b64, body_b64, signature = token.split(".")
        if not hmac.compare_digest(_sign(f"{header_b64}.{body_b64}"), signature):
            raise TokenError("Invalid session")
        if json.loads(_unb64(header_b64)).get("alg") != "HS256":
            raise TokenError("Invalid session")
        claims = json.loads(_unb64(body_b64))
    except TokenError:
        raise
    except (ValueError, TypeError):
        raise TokenError("Invalid session")

    if int(claims.get("exp", 0)) <= int(time.time()):
        raise TokenError("Session expired")
    return claims