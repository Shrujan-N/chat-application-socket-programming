"""Regression tests for the Issue #38 voice-message upload endpoint."""
import os
import tempfile
import uuid
from pathlib import Path

_tmp = tempfile.mkdtemp()
os.environ.setdefault("CHAT_DB_PATH", os.path.join(_tmp, "test_voice_messages.db"))
os.environ.setdefault("JWT_SECRET", "test-secret")

import pytest
from fastapi.testclient import TestClient

import main

client = TestClient(main.app)


def make_headers():
    username = "voice_" + uuid.uuid4().hex[:12]
    password = "Valid1234"
    registered = client.post(
        "/api/register",
        json={"username": username, "email": f"{username}@example.com", "password": password},
    )
    assert registered.status_code == 200, registered.text
    logged_in = client.post("/api/login", json={"username": username, "password": password})
    assert logged_in.status_code == 200, logged_in.text
    return {"Authorization": f"Bearer {logged_in.json()['token']}"}


@pytest.fixture
def upload_dir(monkeypatch, tmp_path):
    directory = tmp_path / "uploads"
    monkeypatch.setattr(main, "UPLOADS_DIR", str(directory))
    return directory


def test_voice_upload_requires_authentication(upload_dir):
    response = client.post(
        "/api/voice-messages/upload",
        files={"file": ("voice.webm", b"\x1a\x45\xdf\xa3audio", "audio/webm")},
    )
    assert response.status_code == 401
    assert not upload_dir.exists() or not list(upload_dir.iterdir())


def test_webm_voice_upload_uses_server_generated_filename(upload_dir):
    headers = make_headers()
    response = client.post(
        "/api/voice-messages/upload",
        headers=headers,
        files={"file": ("../../unsafe-name.webm", b"\x1a\x45\xdf\xa3audio", "audio/webm;codecs=opus")},
    )
    assert response.status_code == 200, response.text
    result = response.json()
    assert result["success"] is True
    assert result["content_type"] == "audio/webm"
    assert result["size"] > 0
    assert result["file_url"].startswith("/uploads/voice-")
    name = result["file_url"].rsplit("/", 1)[-1]
    assert name != "unsafe-name.webm"
    assert Path(upload_dir, name).read_bytes() == b"\x1a\x45\xdf\xa3audio"


def test_voice_upload_rejects_non_audio_content_type(upload_dir):
    response = client.post(
        "/api/voice-messages/upload",
        headers=make_headers(),
        files={"file": ("voice.txt", b"not audio", "text/plain")},
    )
    assert response.status_code == 415


def test_voice_upload_rejects_empty_recordings(upload_dir):
    response = client.post(
        "/api/voice-messages/upload",
        headers=make_headers(),
        files={"file": ("voice.webm", b"", "audio/webm")},
    )
    assert response.status_code == 400


def test_voice_upload_enforces_five_megabyte_limit(upload_dir):
    payload = b"x" * (main.MAX_VOICE_MESSAGE_BYTES + 1)
    response = client.post(
        "/api/voice-messages/upload",
        headers=make_headers(),
        files={"file": ("voice.webm", payload, "audio/webm")},
    )
    assert response.status_code == 413
    assert not upload_dir.exists() or not any(upload_dir.iterdir())
