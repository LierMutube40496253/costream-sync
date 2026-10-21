"""Discord mode: login by token (checked with Discord), rooms = voice channels (from the bot).
Discord itself is faked: /users/@me knows two tokens, everything else is 401."""
import time
from contextlib import ExitStack

import httpx
import pytest
from fastapi.testclient import TestClient

import server.app as srv
from server.auth import DiscordAuth
from server.voice import VoiceDirectory

LIER, SAM = "tok-lier-0123456789", "tok-sam-0123456789"
DISCORD_USERS = {LIER: {"id": "1001", "global_name": "Lier"}, SAM: {"id": "1002", "username": "sam"}}
BOT_KEY = "test-bot-key"
STACK = ExitStack()
calls = []


def fake_discord(request: httpx.Request) -> httpx.Response:
    calls.append(request.url.path)
    token = request.headers.get("authorization", "").removeprefix("Bearer ")
    if request.url.path == "/api/users/@me" and token in DISCORD_USERS:
        return httpx.Response(200, json=DISCORD_USERS[token])
    return httpx.Response(401, json={"message": "401: Unauthorized"})


@pytest.fixture
def client(monkeypatch):
    srv.rooms.clear()
    srv.users.clear()
    calls.clear()
    monkeypatch.setattr(srv, "AUTH_MODE", "discord")
    monkeypatch.setattr(srv, "BOT_SHARED_SECRET", BOT_KEY)
    monkeypatch.setattr(srv, "OFFER_TIMEOUT", 0.5)
    monkeypatch.setattr(srv, "voice", VoiceDirectory())
    monkeypatch.setattr(srv, "auth", DiscordAuth("https://discord.test/api", transport=httpx.MockTransport(fake_discord)))
    with TestClient(srv.app) as c:
        yield c
        STACK.close()


def drain_until(ws, kind):
    while True:
        msg = ws.receive_json()
        if msg["type"] == kind:
            return msg


def connect(client, hello):
    ws = STACK.enter_context(client.websocket_connect("/ws"))
    welcome = ws.receive_json()
    assert welcome["type"] == "welcome" and welcome["auth"] == "discord"
    ws.send_json({"type": "hello", **hello})
    return ws


def bot(client):
    ws = connect(client, {"role": "bot", "secret": BOT_KEY})
    assert ws.receive_json() == {"type": "hello_ok", "role": "bot"}
    return ws


def voice(bot_ws, channel, members, name="LCK casting"):
    bot_ws.send_json({"type": "voice_members", "channel_id": channel, "channel_name": name, "members": members})


def status(delay, k=1792504509.0):
    at = time.time()
    return {"type": "status", "video_id": "v1", "current_delay": delay, "live_edge": delay, "max_delay": 30,
            "at": at, "player_time": at - delay - k, "in_ad": False, "quality": "frame"}


def test_invalid_token_is_rejected(client):
    ws = connect(client, {"role": "panel", "token": "not-a-real-token-123"})
    err = ws.receive_json()
    assert err["type"] == "error" and err["code"] == "auth"
    assert "log in with Discord again" in err["message"]
    assert calls == ["/api/users/@me"]   # it really asked Discord


def test_valid_token_maps_to_the_discord_user_not_a_claimed_id(client):
    ws = connect(client, {"role": "panel", "token": LIER, "user": "1002"})   # claims to be Sam
    ok = drain_until(ws, "hello_ok")
    assert ok["user"] == "1001" and ok["name"] == "Lier"
    ws2 = connect(client, {"role": "panel", "token": SAM})
    assert drain_until(ws2, "hello_ok")["name"] == "sam"


def test_panel_and_extension_of_one_account_are_paired(client):
    b = bot(client)
    voice(b, "111", {"1001": "Lier"})
    ext = connect(client, {"role": "extension", "token": LIER})
    drain_until(ext, "hello_ok")
    panel = connect(client, {"role": "panel", "token": LIER})
    assert drain_until(panel, "hello_ok")["room_name"] == "LCK casting"
    ext.send_json(status(12.5))
    while True:
        g = drain_until(panel, "group")
        mine = [m for m in g["members"] if m["user"] == "1001"]
        if mine and mine[0]["current_delay"] == 12.5:
            break
    assert len(g["members"]) == 1   # one person, not two
    assert mine[0]["extension"] and mine[0]["panel"] and mine[0]["name"] == "Lier"
    # A second extension for the same account is refused (no duplicates).
    ext2 = connect(client, {"role": "extension", "token": LIER})
    assert "already has a extension" in drain_until(ext2, "error")["message"]


def test_bot_key_is_checked(client):
    ws = connect(client, {"role": "bot", "secret": "wrong"})
    err = ws.receive_json()
    assert err["code"] == "auth" and "BOT_SHARED_SECRET" in err["message"]


def test_rooms_follow_voice_channels(client):
    panel = connect(client, {"role": "panel", "token": LIER})
    drain_until(panel, "hello_ok")
    drain_until(panel, "no_voice_channel")           # not in a voice channel yet
    b = bot(client)
    voice(b, "111", {"1001": "Lier", "1002": "sam"})
    g = drain_until(panel, "group")
    assert g["room_name"] == "LCK casting" and {m["name"] for m in g["members"]} == {"Lier", "sam"}
    voice(b, "111", {"1002": "sam"})                  # Lier leaves the channel
    drain_until(panel, "no_voice_channel")


def test_match_between_two_discord_accounts_and_leaving_the_channel_ends_it(client):
    b = bot(client)
    voice(b, "111", {"1001": "Lier", "1002": "sam"})
    socks = {}
    for tok, delay in ((LIER, 3), (SAM, 8)):
        ext = connect(client, {"role": "extension", "token": tok})
        drain_until(ext, "hello_ok")
        ext.send_json(status(delay))
        panel = connect(client, {"role": "panel", "token": tok})
        drain_until(panel, "hello_ok")
        socks[tok] = (ext, panel)
    for _, panel in socks.values():   # both panels see both measured
        while sum(m["current_delay"] is not None for m in drain_until(panel, "group")["members"]) < 2:
            pass
    socks[LIER][1].send_json({"type": "match_request", "target_user": "1002"})
    offer = drain_until(socks[SAM][1], "match_offer")
    assert offer["names"]["1001"] == "Lier" and offer["your_move"]["action"] == "none"
    socks[SAM][1].send_json({"type": "match_answer", "offer_id": offer["offer_id"], "accept": True})
    assert drain_until(socks[LIER][0], "set_target")["target_delay"] == 8
    sid = drain_until(socks[SAM][0], "set_target")["session_id"]
    voice(b, "111", {"1001": "Lier"})                 # sam leaves the voice channel
    assert drain_until(socks[LIER][0], "leave_sync")["session_id"] == sid
    assert drain_until(socks[SAM][0], "leave_sync")["session_id"] == sid


def test_requests_are_rate_limited(client):
    b = bot(client)
    voice(b, "111", {"1001": "Lier"})
    panel = connect(client, {"role": "panel", "token": LIER})
    drain_until(panel, "hello_ok")
    for _ in range(6):
        panel.send_json({"type": "match_request", "target_user": "1002"})
    msgs = [drain_until(panel, "error")["message"] for _ in range(6)]
    assert msgs[:5] == ["your extension isn't measuring yet (or an ad is playing)"] * 5
    assert msgs[5] == "too many requests: wait a few seconds"


def test_whoami_names_the_token_owner_without_registering(client):
    ws = connect(client, {"role": "ignored", "token": LIER} | {"type": "whoami"})
    assert ws.receive_json() == {"type": "whoami", "ok": True, "user": "1001", "name": "Lier"}
    bad = connect(client, {"type": "whoami", "token": "not-a-real-token-123"})
    assert bad.receive_json()["ok"] is False
    assert srv.users == {}


def test_discord_is_asked_once_per_token(client):
    for _ in range(3):
        ws = connect(client, {"role": "panel", "token": SAM})
        drain_until(ws, "hello_ok")
        ws.close()
    assert calls == ["/api/users/@me"]
