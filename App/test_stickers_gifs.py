"""Tests for the Issue #40 GIF and sticker integration."""
import os
import tempfile
import uuid
from pathlib import Path

_tmp = tempfile.mkdtemp()
os.environ.setdefault("CHAT_DB_PATH", os.path.join(_tmp, "test_stickers_gifs.db"))
os.environ.setdefault("JWT_SECRET", "issue40-test-secret")

from fastapi.testclient import TestClient
import main

web = TestClient(main.app)


def make_auth_headers():
    suffix = uuid.uuid4().hex[:10]
    username = f"gifuser{suffix}"
    password = "Valid1234"
    registration = web.post(
        "/api/register",
        json={"username": username, "email": f"{username}@example.com", "password": password},
    )
    assert registration.status_code == 200, registration.text
    login = web.post("/api/login", json={"username": username, "password": password})
    assert login.status_code == 200, login.text
    return {"Authorization": f"Bearer {login.json()['token']}"}


def test_gif_config_requires_authentication():
    response = web.get("/api/gifs/config")
    assert response.status_code == 401
    assert response.json()["detail"] == "Authentication required"


def test_gif_config_is_disabled_when_key_is_missing(monkeypatch):
    monkeypatch.setattr(main, "GIPHY_API_KEY", "")
    response = web.get("/api/gifs/config", headers=make_auth_headers())
    assert response.status_code == 200
    assert response.json() == {"provider": "giphy", "enabled": False, "api_key": None}


def test_gif_config_returns_configured_key_only_to_authenticated_client(monkeypatch):
    monkeypatch.setattr(main, "GIPHY_API_KEY", "test-public-key")
    response = web.get("/api/gifs/config", headers=make_auth_headers())
    assert response.status_code == 200
    assert response.json() == {
        "provider": "giphy",
        "enabled": True,
        "api_key": "test-public-key",
    }


def test_picker_has_gif_and_sticker_search_and_attribution():
    page = (Path(main.STATIC_DIR) / "index.html").read_text(encoding="utf-8")
    assert 'id="gif-picker-dialog"' in page
    assert 'onclick="setGifKind(\'gif\')"' in page
    assert 'onclick="setGifKind(\'sticker\')"' in page
    assert "https://api.giphy.com/v1/${endpointType}/search" in page
    assert "Powered by GIPHY" in page


def test_picker_validates_external_media_hosts_and_uses_outbox():
    page = (Path(main.STATIC_DIR) / "index.html").read_text(encoding="utf-8")
    assert "function normalizeGiphyAssetUrl" in page
    assert "url.protocol !== 'https:'" in page
    assert "giphy\\.com" in page
    assert "queueOutgoingMessage(payload)" in page
    assert "transmitQueuedMessage(payload)" in page
