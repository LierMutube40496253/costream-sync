import time
from contextlib import ExitStack

import pytest
from fastapi.testclient import TestClient

import server.app as srv
from server.app import app, rooms

V = "0sfDth4lMLc"
K0 = 1792504509.0   # broadcast stamp: wall-clock time = player time + K
STACK = ExitStack()


@pytest.fixture
def client(monkeypatch):
    rooms.clear()
    monkeypatch.setattr(srv, "OFFER_TIMEOUT", 0.5)
    with TestClient(app) as c:
        yield c
        STACK.close()  # close sockets before the TestClient shuts down


def drain_until(ws, kind):
    while True:
        msg = ws.receive_json()
        if msg["type"] == kind:
            return msg


def types_until_pong(ws):
    """Message types already sent to ws: ping and collect until the pong."""
    ws.send_json({"type": "clock_ping", "t0": 0})
    seen = []
    while (m := ws.receive_json())["type"] != "clock_pong":
        seen.append(m["type"])
    return seen


def status(edge, current, mx=30, k=K0, video=V, **extra):
    at = time.time()
    return {"type": "status", "video_id": video, "current_delay": current, "live_edge": edge,
            "max_delay": mx, "at": at, "player_time": at - current - k, "in_ad": False,
            "quality": "frame", **extra}


class U:
    """One test user: extension + panel connections."""

    def __init__(self, client, room, name, edge, current=None, mx=30, k=K0):
        self.name = name
        self.ext = STACK.enter_context(client.websocket_connect("/ws"))
        self.ext.send_json({"type": "hello", "role": "extension", "user": name, "room": room})
        assert self.ext.receive_json()["type"] == "hello_ok"
        self.ext.send_json(status(edge, edge if current is None else current, mx, k))
        self.panel = STACK.enter_context(client.websocket_connect("/ws"))
        self.panel.send_json({"type": "hello", "role": "panel", "user": name, "room": room})


def room_of(client, *people):
    users = [U(client, "r", *p) for p in people]
    for u in users:   # wait until every panel sees every member measured
        while True:
            g = drain_until(u.panel, "group")
            if sum(m["current_delay"] is not None for m in g["members"]) == len(users):
                break
    return users


def test_match_offer_moves_from_current_position_and_accept(client):
    # Design doc rule 3: B's live edge is 25 s but B sits at 41 s.
    a, b = room_of(client, ("A", 20, 20, 30, K0 + 0.000), ("B", 25, 41, 30, K0 + 0.040))
    a.panel.send_json({"type": "match_request", "target_user": "B"})
    offer = drain_until(b.panel, "match_offer")
    assert offer["requester"] == "A" and offer["target_delay"] == 25
    assert offer["moves"]["A"]["text"] == "pause your stream 5.0 s"
    assert offer["your_move"] == {"action": "skip_forward", "seconds": 16, "text": "skip your stream forward 16.0 s"}

    b.panel.send_json({"type": "match_answer", "offer_id": offer["offer_id"], "accept": True})
    res = drain_until(a.panel, "match_result")
    assert res["status"] == "accepted" and res["group"] == ["A", "B"] and res["target_delay"] == 25
    assert drain_until(a.ext, "match_result")["session_id"] == res["session_id"]
    ta, tb = drain_until(a.ext, "set_target"), drain_until(b.ext, "set_target")
    # One reference stamp for both: the median of their own stamps (40 ms apart).
    assert ta["reference_stamp"] == tb["reference_stamp"] == pytest.approx(K0 + 0.020, abs=1e-3)
    assert ta["target_player_offset"] == pytest.approx(-(25 + K0 + 0.020), abs=1e-3)
    assert ta["session_id"] == tb["session_id"] == res["session_id"]


def test_match_from_slower_side_offers_pause_to_the_faster(client):
    a, b = room_of(client, ("A", 3), ("B", 8, 8, 10))
    b.panel.send_json({"type": "match_request", "target_user": "A"})
    offer = drain_until(a.panel, "match_offer")
    assert offer["your_move"]["text"] == "pause your stream 5.0 s"
    assert offer["moves"]["B"]["text"] == "no change to your stream"


def test_match_decline_changes_nothing(client):
    a, b = room_of(client, ("A", 3), ("B", 8, 8, 10))
    a.panel.send_json({"type": "match_request", "target_user": "B"})
    offer = drain_until(b.panel, "match_offer")
    b.panel.send_json({"type": "match_answer", "offer_id": offer["offer_id"], "accept": False})
    res = drain_until(a.panel, "match_result")
    assert res["status"] == "declined" and res["session_id"] is None
    assert "set_target" not in types_until_pong(a.ext)


def test_match_timeout_and_late_answer(client):
    a, b = room_of(client, ("A", 3), ("B", 8, 8, 10))
    a.panel.send_json({"type": "match_request", "target_user": "B"})
    offer = drain_until(b.panel, "match_offer")
    t0 = time.monotonic()
    res = drain_until(a.panel, "match_result")
    assert res["status"] == "timed_out" and time.monotonic() - t0 >= 0.4
    assert res["users"] == {"A": "requester", "B": "timed_out"}
    b.panel.send_json({"type": "match_answer", "offer_id": offer["offer_id"], "accept": True})
    assert "no such open offer" in drain_until(b.panel, "error")["message"]
    assert "set_target" not in types_until_pong(a.ext)


def test_match_over_gap_is_refused_without_offer(client):
    a, b, c = room_of(client, ("A", 3), ("B", 8, 8, 10), ("C", 45, 45, 20))
    a.panel.send_json({"type": "match_request", "target_user": "C"})
    res = drain_until(a.panel, "match_result")
    assert res["status"] == "refused" and "over the 10 s gap" in res["reasons"]["C"]
    assert "match_offer" not in types_until_pong(c.panel)


def test_doc_worked_example_sync_all(client):
    # A 3/30, B 8/10, C 6/20, D 15/20 -> A, B, C at 8 s; D left out by the gap.
    a, b, c, d = room_of(client, ("A", 3), ("B", 8, 8, 10), ("C", 6, 6, 20), ("D", 15, 15, 20))
    a.panel.send_json({"type": "sync_all_request"})
    ob, oc = drain_until(b.panel, "match_offer"), drain_until(c.panel, "match_offer")
    assert ob["target_delay"] == 8 and oc["your_move"]["text"] == "pause your stream 2.0 s"
    assert "12.0 s behind the fastest (A)" in ob["left_out"]["D"]
    b.panel.send_json({"type": "match_answer", "offer_id": ob["offer_id"], "accept": True})
    c.panel.send_json({"type": "match_answer", "offer_id": oc["offer_id"], "accept": True})
    res = drain_until(a.panel, "match_result")
    assert res["status"] == "synced" and res["group"] == ["A", "B", "C"] and res["target_delay"] == 8
    assert res["users"]["D"] == "left_out"
    assert drain_until(d.panel, "match_result")["users"]["D"] == "left_out"   # D is told why
    assert drain_until(c.ext, "set_target")["target_delay"] == 8


def test_sync_all_only_accepters_join(client):
    a, b, d = room_of(client, ("A", 3), ("B", 8, 8, 10), ("D", 6, 6, 10))
    a.panel.send_json({"type": "sync_all_request"})
    ob, od = drain_until(b.panel, "match_offer"), drain_until(d.panel, "match_offer")
    b.panel.send_json({"type": "match_answer", "offer_id": ob["offer_id"], "accept": False})
    d.panel.send_json({"type": "match_answer", "offer_id": od["offer_id"], "accept": True})
    res = drain_until(a.panel, "match_result")
    # Only A and D: slowest of them is D at 6 s.
    assert res["group"] == ["A", "D"] and res["target_delay"] == 6
    assert res["users"] == {"A": "requester", "B": "declined", "D": "accepted"}


def test_sync_all_no_answer_is_timed_out(client):
    a, b, d = room_of(client, ("A", 3), ("B", 8, 8, 10), ("D", 6, 6, 10))
    a.panel.send_json({"type": "sync_all_request"})
    od = drain_until(d.panel, "match_offer")
    d.panel.send_json({"type": "match_answer", "offer_id": od["offer_id"], "accept": True})
    res = drain_until(a.panel, "match_result")
    assert res["users"]["B"] == "timed_out" and res["group"] == ["A", "D"]


def test_leave_sync_hands_back_playback(client):
    a, b = room_of(client, ("A", 3), ("B", 8, 8, 10))
    a.panel.send_json({"type": "match_request", "target_user": "B"})
    offer = drain_until(b.panel, "match_offer")
    b.panel.send_json({"type": "match_answer", "offer_id": offer["offer_id"], "accept": True})
    sid = drain_until(a.panel, "match_result")["session_id"]
    drain_until(b.ext, "set_target")
    a.panel.send_json({"type": "leave_sync", "session_id": sid})
    assert drain_until(a.ext, "leave_sync")["session_id"] == sid
    assert drain_until(b.ext, "leave_sync")["session_id"] == sid   # B alone: session ends


def test_user_in_ad_cannot_be_matched(client):
    a, b = room_of(client, ("A", 3), ("B", 8, 8, 10))
    b.ext.send_json({**status(8, 8, 10), "in_ad": True})
    types_until_pong(b.ext)   # make sure the server processed it
    a.panel.send_json({"type": "match_request", "target_user": "B"})
    assert drain_until(a.panel, "error")["message"] == "B isn't available to sync"


def test_clock_pong_hello_and_validation(client):
    ws = STACK.enter_context(client.websocket_connect("/ws"))
    ws.send_json({"type": "status"})
    assert "first message must be hello" in drain_until(ws, "error")["message"]

    ext = STACK.enter_context(client.websocket_connect("/ws"))
    ext.send_json({"type": "hello", "role": "extension", "user": "A", "room": "r2"})
    drain_until(ext, "hello_ok")
    ext.send_json({"type": "clock_ping", "t0": 123.0})
    pong = drain_until(ext, "clock_pong")
    assert pong["t0"] == 123.0 and pong["t1"] <= pong["t2"] and abs(pong["t2"] - time.time()) < 5
    ext.send_json({**status(3, 3), "current_delay": "fast"})
    assert "current_delay must be a number" in drain_until(ext, "error")["message"]
    ext.send_json({"type": "match_request", "target_user": "B"})
    assert "not allowed for extension" in drain_until(ext, "error")["message"]

    dup = STACK.enter_context(client.websocket_connect("/ws"))
    dup.send_json({"type": "hello", "role": "extension", "user": "A", "room": "r2"})
    assert "already has a extension" in drain_until(dup, "error")["message"]
