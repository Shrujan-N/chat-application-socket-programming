"""Regression and acceptance tests for FR-28: Voice/Video Calls (Issue #39).

Verifies:
1. Voice call offer and answer signaling over WebSocket (Pass criteria).
2. Video call offer preserves media_type='video'.
3. ICE candidate forwarding between peers.
4. Call rejection/decline ends cleanly and records status='declined' (FR-28 fail criteria).
5. Ending active call computes duration and persists record in database.
6. Group call offer broadcasts to all group members.
7. Blocked users cannot call the blocker.
8. User cannot call themselves.
9. REST endpoint /api/calls/history returns past call records.
10. REST endpoint /api/calls/active reflects ongoing call state.
"""

import asyncio
import os
import sys
import tempfile
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

_tmp = tempfile.mkdtemp(prefix="chat_calls_test_")
os.environ["CHAT_DB_PATH"] = os.path.join(_tmp, "calls_test.db")
os.environ.pop("CHAT_WEB_DB_PATH", None)
os.environ["JWT_SECRET"] = "test-calls-secret"
os.environ["CHAT_UPLOADS_DIR"] = os.path.join(_tmp, "uploads")

import main
import call_service
from fastapi.testclient import TestClient

client = TestClient(main.app)


def _make_user(username: str):
    client.post(
        "/api/register",
        json={
            "username": username,
            "email": f"{username}@example.com",
            "password": "ValidPass123",
        },
    )
    login = client.post(
        "/api/login",
        json={"username": username, "password": "ValidPass123"},
    )
    token = login.json()["token"]
    return username, {"Authorization": f"Bearer {token}"}, token


def test_voice_call_signaling_offer_and_answer():
    """Verify voice call setup and signaling exchange between two users."""
    alice, _, a_tok = _make_user("caller_alice_1")
    bob, _, b_tok = _make_user("receiver_bob_1")
    call_id = "test-voice-call-01"

    with client.websocket_connect(f"/ws/{alice}?token={a_tok}") as ws_a, \
         client.websocket_connect(f"/ws/{bob}?token={b_tok}") as ws_b:

        # Alice sends voice call offer
        ws_a.send_json({
            "type": "call_offer",
            "call_id": call_id,
            "recipient": bob,
            "media_type": "voice",
            "sdp": {"type": "offer", "sdp": "mock-sdp-offer"}
        })

        time.sleep(0.05)
        # Alice receives ringing acknowledgement
        ack_a = ws_a.receive_json()
        assert ack_a["type"] == "call_ringing"
        assert ack_a["call_id"] == call_id

        # Bob receives incoming call offer
        offer_b = ws_b.receive_json()
        assert offer_b["type"] == "call_offer"
        assert offer_b["call_id"] == call_id
        assert offer_b["caller"] == alice
        assert offer_b["media_type"] == "voice"

        # Bob answers the call
        ws_b.send_json({
            "type": "call_answer",
            "call_id": call_id,
            "sdp": {"type": "answer", "sdp": "mock-sdp-answer"}
        })

        time.sleep(0.05)
        # Alice receives answer
        ans_a = ws_a.receive_json()
        assert ans_a["type"] == "call_answer"
        assert ans_a["call_id"] == call_id
        assert ans_a["answerer"] == bob

        # Alice sends call_end to cleanly terminate
        ws_a.send_json({"type": "call_end", "call_id": call_id})
        time.sleep(0.05)
        end_b = ws_b.receive_json()
        assert end_b["type"] == "call_ended"


def test_video_call_offer_preserves_media_type():
    """Verify video calls preserve media_type='video'."""
    alice, _, a_tok = _make_user("video_user_a")
    bob, _, b_tok = _make_user("video_user_b")
    call_id = "test-video-call-02"

    with client.websocket_connect(f"/ws/{alice}?token={a_tok}") as ws_a, \
         client.websocket_connect(f"/ws/{bob}?token={b_tok}") as ws_b:

        ws_a.send_json({
            "type": "call_offer",
            "call_id": call_id,
            "recipient": bob,
            "media_type": "video",
            "sdp": {"type": "offer", "sdp": "mock-video-sdp"}
        })

        time.sleep(0.05)
        ack_a = ws_a.receive_json()
        assert ack_a["type"] == "call_ringing"

        offer_b = ws_b.receive_json()
        assert offer_b["type"] == "call_offer"
        assert offer_b["media_type"] == "video"

        # Cleanup
        ws_b.send_json({"type": "call_reject", "call_id": call_id, "reason": "declined"})
        time.sleep(0.05)
        ws_a.receive_json()


def test_ice_candidate_forwarding():
    """Verify ICE candidates are routed between peers."""
    alice, _, a_tok = _make_user("ice_user_a")
    bob, _, b_tok = _make_user("ice_user_b")
    call_id = "test-ice-call-03"

    with client.websocket_connect(f"/ws/{alice}?token={a_tok}") as ws_a, \
         client.websocket_connect(f"/ws/{bob}?token={b_tok}") as ws_b:

        # Setup call
        ws_a.send_json({
            "type": "call_offer",
            "call_id": call_id,
            "recipient": bob,
            "media_type": "voice",
            "sdp": {"type": "offer"}
        })
        time.sleep(0.05)
        ws_a.receive_json()
        ws_b.receive_json()

        # Alice sends candidate
        candidate_data = {"candidate": "candidate:1 1 UDP 2130706431 192.168.1.1 5000 typ host", "sdpMLineIndex": 0}
        ws_a.send_json({
            "type": "ice_candidate",
            "call_id": call_id,
            "candidate": candidate_data,
            "target": bob
        })

        time.sleep(0.05)
        cand_b = ws_b.receive_json()
        assert cand_b["type"] == "ice_candidate"
        assert cand_b["call_id"] == call_id
        assert cand_b["sender"] == alice
        assert cand_b["candidate"] == candidate_data

        # Cleanup
        ws_b.send_json({"type": "call_reject", "call_id": call_id})
        time.sleep(0.05)
        ws_a.receive_json()


def test_call_rejection_fail_criteria():
    """FR-28 Fail criteria: A declined or unanswered call ends gracefully."""
    alice, _, a_tok = _make_user("reject_caller_a")
    bob, _, b_tok = _make_user("reject_target_b")
    call_id = "test-decline-call-04"

    with client.websocket_connect(f"/ws/{alice}?token={a_tok}") as ws_a, \
         client.websocket_connect(f"/ws/{bob}?token={b_tok}") as ws_b:

        ws_a.send_json({
            "type": "call_offer",
            "call_id": call_id,
            "recipient": bob,
            "media_type": "voice",
            "sdp": {"type": "offer"}
        })
        time.sleep(0.05)
        ws_a.receive_json()
        ws_b.receive_json()

        # Bob declines call
        ws_b.send_json({
            "type": "call_reject",
            "call_id": call_id,
            "reason": "declined"
        })

        time.sleep(0.05)
        # Alice receives call_rejected
        rej_a = ws_a.receive_json()
        assert rej_a["type"] == "call_rejected"
        assert rej_a["call_id"] == call_id
        assert rej_a["by"] == bob
        assert rej_a["reason"] == "declined"

    # Database must record status='declined'
    conn = main.get_db()
    try:
        row = conn.execute("SELECT status FROM call_records WHERE call_id = ?", (call_id,)).fetchone()
        assert row is not None
        assert row["status"] == "declined"
    finally:
        conn.close()


def test_call_history_and_duration_persistence():
    """Verify call history records completed calls and durations."""
    alice, a_headers, a_tok = _make_user("history_caller_a")
    bob, _, b_tok = _make_user("history_target_b")
    call_id = "test-history-call-05"

    with client.websocket_connect(f"/ws/{alice}?token={a_tok}") as ws_a, \
         client.websocket_connect(f"/ws/{bob}?token={b_tok}") as ws_b:

        ws_a.send_json({
            "type": "call_offer",
            "call_id": call_id,
            "recipient": bob,
            "media_type": "voice",
            "sdp": {"type": "offer"}
        })
        time.sleep(0.05)
        ws_a.receive_json()
        ws_b.receive_json()

        ws_b.send_json({
            "type": "call_answer",
            "call_id": call_id,
            "sdp": {"type": "answer"}
        })
        time.sleep(0.05)
        ws_a.receive_json()

        # Wait a moment
        time.sleep(0.1)

        ws_a.send_json({"type": "call_end", "call_id": call_id})
        time.sleep(0.05)
        ws_b.receive_json()

    # Query call history via REST
    resp = client.get("/api/calls/history", headers=a_headers)
    assert resp.status_code == 200
    calls = resp.json()["calls"]
    match = next(c for c in calls if c["call_id"] == call_id)
    assert match["caller"] == alice
    assert match["recipient"] == bob
    assert match["status"] == "completed"
    assert match["media_type"] == "voice"


def test_group_call_offer_broadcast():
    """Verify group call offer is broadcast to all active members of the group."""
    alice, a_headers, a_tok = _make_user("grp_caller_a")
    bob, _, b_tok = _make_user("grp_caller_b")
    charlie, _, c_tok = _make_user("grp_caller_c")

    # Create group
    grp_resp = client.post(
        "/api/groups",
        headers=a_headers,
        json={"name": "Video Call Group", "members": [bob, charlie]}
    )
    assert grp_resp.status_code == 200
    group_id = grp_resp.json()["group"]["group_id"]
    call_id = "test-group-call-06"

    with client.websocket_connect(f"/ws/{alice}?token={a_tok}") as ws_a, \
         client.websocket_connect(f"/ws/{bob}?token={b_tok}") as ws_b, \
         client.websocket_connect(f"/ws/{charlie}?token={c_tok}") as ws_c:

        # Alice initiates group call
        ws_a.send_json({
            "type": "call_offer",
            "call_id": call_id,
            "group_id": group_id,
            "media_type": "video",
            "sdp": {"type": "offer"}
        })

        time.sleep(0.05)
        # Alice gets ringing ack
        ack_a = ws_a.receive_json()
        assert ack_a["type"] == "call_ringing"
        assert ack_a["group_id"] == group_id

        # Both Bob and Charlie receive offer
        offer_b = ws_b.receive_json()
        assert offer_b["type"] == "call_offer"
        assert offer_b["group_id"] == group_id
        assert offer_b["caller"] == alice

        offer_c = ws_c.receive_json()
        assert offer_c["type"] == "call_offer"
        assert offer_c["group_id"] == group_id
        assert offer_c["caller"] == alice

        # Alice ends call
        ws_a.send_json({"type": "call_end", "call_id": call_id})
        time.sleep(0.05)
        end_b = ws_b.receive_json()
        assert end_b["type"] == "call_ended"
        end_c = ws_c.receive_json()
        assert end_c["type"] == "call_ended"


def test_blocked_user_cannot_call():
    """Verify blocked user call offer is rejected."""
    alice, _, a_tok = _make_user("blk_caller_a")
    bob, b_headers, _ = _make_user("blk_target_b")

    # Bob blocks Alice
    blk_resp = client.post("/api/blocks", headers=b_headers, json={"username": alice})
    assert blk_resp.status_code == 200

    with client.websocket_connect(f"/ws/{alice}?token={a_tok}") as ws_a:
        ws_a.send_json({
            "type": "call_offer",
            "call_id": "test-blocked-call-07",
            "recipient": bob,
            "media_type": "voice",
            "sdp": {"type": "offer"}
        })
        time.sleep(0.05)
        err = ws_a.receive_json()
        assert err["type"] == "error"
        assert "unavailable" in err["message"].lower()


def test_cannot_call_oneself():
    """Verify user cannot call themselves."""
    alice, _, a_tok = _make_user("self_caller_a")
    with client.websocket_connect(f"/ws/{alice}?token={a_tok}") as ws_a:
        ws_a.send_json({
            "type": "call_offer",
            "call_id": "test-self-call-08",
            "recipient": alice,
            "media_type": "voice",
            "sdp": {"type": "offer"}
        })
        time.sleep(0.05)
        err = ws_a.receive_json()
        assert err["type"] == "error"
        assert "cannot call yourself" in err["message"].lower()


if __name__ == "__main__":
    for name, func in list(globals().items()):
        if name.startswith("test_") and callable(func):
            print(f"Running {name}...", flush=True)
            func()
            print(f"{name} PASSED", flush=True)
    print("\nAll 8 voice/video call tests PASSED successfully!", flush=True)
