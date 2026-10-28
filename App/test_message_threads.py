"""Regression tests for FR-24 message replies and quoted navigation metadata."""
import asyncio
import os
import tempfile

_tmp = tempfile.mkdtemp(prefix="chat_threads_tests_")
os.environ["CHAT_DB_PATH"] = os.path.join(_tmp, "threads_test.db")
os.environ.pop("CHAT_WEB_DB_PATH", None)
os.environ["JWT_SECRET"] = "test-message-threads-secret"
os.environ["CHAT_UPLOADS_DIR"] = os.path.join(_tmp, "uploads")

import main


def _save(msg_id, sender, recipient, content, timestamp, reply_to_msg_id=None):
    return main.persist_websocket_message(
        msg_id, sender, recipient, content, timestamp, None, reply_to_msg_id
    )


def test_reply_reference_is_persisted_and_preview_is_returned():
    _save("thread-parent-1", "thread_a", "thread_b", "Original message", "10:00")
    _save("thread-reply-1", "thread_b", "thread_a", "A reply", "10:01", "thread-parent-1")
    connection = main.get_db()
    try:
        row = connection.execute(
            "SELECT reply_to_msg_id FROM messages WHERE msg_id = ?", ("thread-reply-1",)
        ).fetchone()
    finally:
        connection.close()
    assert row["reply_to_msg_id"] == "thread-parent-1"
    preview = main.get_message_reply_preview("thread-parent-1", "thread_b", "thread_a")
    assert preview == {
        "msg_id": "thread-parent-1",
        "sender": "thread_a",
        "content": "Original message",
        "timestamp": "10:00",
    }


def test_reply_target_must_belong_to_same_conversation():
    _save("thread-parent-2", "thread_a", "thread_b", "Private context", "10:02")
    assert main.get_message_reply_preview("thread-parent-2", "thread_c", "thread_b") is None
    assert main.get_message_reply_preview("does-not-exist", "thread_a", "thread_b") is None


def test_conversation_history_contains_reply_preview():
    _save("thread-parent-3", "thread_a", "thread_b", "The question", "10:03")
    _save("thread-reply-3", "thread_b", "thread_a", "The answer", "10:04", "thread-parent-3")
    result = asyncio.run(main.get_chat_history("thread_a", "thread_b"))
    reply = next(item for item in result["history"] if item["msg_id"] == "thread-reply-3")
    assert reply["reply_to_msg_id"] == "thread-parent-3"
    assert reply["reply_preview"]["content"] == "The question"


def test_existing_messages_without_reply_remain_compatible():
    _save("thread-no-reply", "thread_a", "thread_b", "Regular message", "10:05")
    result = asyncio.run(main.get_chat_history("thread_a", "thread_b"))
    message = next(item for item in result["history"] if item["msg_id"] == "thread-no-reply")
    assert message["reply_to_msg_id"] is None
    assert message["reply_preview"] is None
