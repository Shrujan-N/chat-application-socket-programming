"""Responsive layout regression tests for Issue #49."""
from pathlib import Path

PAGE = (Path(__file__).parent / "static" / "index.html").read_text(encoding="utf-8")


def test_viewport_is_configured_for_mobile_devices():
    assert '<meta name="viewport" content="width=device-width, initial-scale=1.0">' in PAGE


def test_mobile_contacts_and_chat_navigation_exists():
    assert 'id="mobile-chat-nav"' in PAGE
    assert 'id="mobile-chat-back"' in PAGE
    assert 'onclick="showMobileContacts()"' in PAGE
    assert "function showMobileContacts()" in PAGE


def test_selecting_a_contact_switches_to_mobile_chat_view():
    assert "window.matchMedia('(max-width: 700px)')" in PAGE
    assert "document.body.classList.add('mobile-chat-open')" in PAGE
    assert "document.body.classList.remove('mobile-chat-open')" in PAGE
    assert "body.mobile-chat-open #sidebar" in PAGE
    assert "body.mobile-chat-open #chat-area" in PAGE


def test_mobile_layout_handles_dynamic_viewport_and_safe_areas():
    assert "height: 100dvh" in PAGE
    assert "env(safe-area-inset-top" in PAGE
    assert "env(safe-area-inset-bottom" in PAGE


def test_touch_and_mobile_input_sizing_are_supported():
    assert "min-height: 44px" in PAGE
    assert "input, select, textarea { font-size: 16px; }" in PAGE
    assert "touch-action: manipulation" in PAGE
