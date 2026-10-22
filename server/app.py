"""Sync server (docs/DESIGN.md, "Communication protocol"). JSON over WebSocket.

Run:  python -m uvicorn server.app:app --port 8765
Connect: ws://localhost:8765/ws

Every connection first gets {"type": "welcome", "auth": "discord"|"dev", ...}.
Discord mode: hello carries a Discord access token; the server asks Discord whose it is and
uses that id (an extension and a panel of the same account are paired). The room is the voice
channel the bot reports. Dev mode (no DISCORD_CLIENT_ID): hello carries user + room names,
as in the examples below. The bot says {"type": "hello", "role": "bot", "secret": ...} and then
sends voice_members (see server/voice.py, server/bot.py).

extension -> server
  {"type": "hello", "role": "extension", "user": "A", "room": "demo"}
  {"type": "clock_ping", "t0": <client send time>}
  {"type": "status", "video_id", "current_delay", "live_edge", "max_delay", "player_time",
   "at", "in_ad", "dvr_available", "quality", "paused_by", "sync_error_ms", "session_id"}
      at = server-clock time the frame with `player_time` was on screen
  {"type": "leave_sync", "session_id"}
panel -> server
  {"type": "hello", "role": "panel", "user": "A", "room": "demo"}
  {"type": "match_request", "target_user": "B"}
  {"type": "sync_all_request"}
  {"type": "match_answer", "offer_id", "accept": true}
  {"type": "leave_sync", "session_id"}
server -> extension
  {"type": "clock_pong", "t0", "t1": <server receive>, "t2": <server send>}
  {"type": "match_result", ...}             same as panels get; lets the extension check acceptance
  {"type": "set_target", "session_id", "reference_stamp", "target_delay", "target_player_offset"}
      the player clock (getCurrentTime) should show server_time + target_player_offset
  {"type": "leave_sync", "session_id"}
server -> panel
  {"type": "group", "members": [...]}       on any change (at most 4 per second)
  {"type": "match_offer", "offer_id", "kind", "requester", "target_delay", "moves", "your_move", "timeout"}
  {"type": "match_result", "offer_id", "kind", "status", "users", "reasons", "group", "target_delay", "session_id"}
any -> {"type": "error", "message"}

Example: A (live edge 20 s, sitting at 20 s) clicks B (live edge 25 s, sitting at 41 s).
B's panel gets match_offer with moves A: pause 5.0 s, B: skip forward 16.0 s. If B accepts
within 15 s, both extensions get set_target 25 s on one reference stamp.
"""

from __future__ import annotations

import asyncio
import collections
import hmac
import itertools
import math
import os
import statistics
import time
from dataclasses import dataclass, field
from pathlib import Path

from fastapi import FastAPI, WebSocket, WebSocketDisconnect
from fastapi.responses import RedirectResponse
from fastapi.staticfiles import StaticFiles

from .auth import DiscordAuth
from .sync_logic import DEFAULT_MAX_GAP, Member, Plan, match_pair, plan_group
from .voice import VoiceDirectory

OFFER_TIMEOUT = 15.0      # seconds a user has to answer an offer; tests lower it
STATUS_STALE = 3.0        # a user whose extension hasn't reported for this long can't be synced
MAX_GAP = DEFAULT_MAX_GAP
GROUP_INTERVAL = 0.25     # at most 4 group updates per second per room
RATE_LIMIT = (5, 30.0)    # at most 5 match / sync-all requests per 30 s per user

# Login: "discord" (hello carries a Discord access token; rooms = voice channels, reported by
# the bot) or "dev" (hello carries user + room names; no Discord needed, used by the tests).
DISCORD_CLIENT_ID = os.environ.get("DISCORD_CLIENT_ID", "")
AUTH_MODE = os.environ.get("COSTREAM_AUTH") or ("discord" if DISCORD_CLIENT_ID else "dev")
DISCORD_AUTHORIZE_URL = os.environ.get("DISCORD_AUTHORIZE_URL", "https://discord.com/oauth2/authorize")
BOT_SHARED_SECRET = os.environ.get("BOT_SHARED_SECRET", "")
auth = DiscordAuth(os.environ.get("DISCORD_API_BASE", "https://discord.com/api"))
voice = VoiceDirectory()

app = FastAPI(title="costream-sync")
_ids = itertools.count(1)

# The panel page (docs/DESIGN.md, Architecture): http://<server>/panel/
app.mount("/panel", StaticFiles(directory=Path(__file__).resolve().parent.parent / "panel", html=True), name="panel")


@app.get("/")
async def root() -> RedirectResponse:
    return RedirectResponse("/panel/")


@app.on_event("startup")
async def say_mode() -> None:
    # One line that tells which login mode is on (a missing .env silently meant dev mode).
    if AUTH_MODE == "discord":
        bot = "bot key set" if BOT_SHARED_SECRET else "NO BOT_SHARED_SECRET: the bot can't connect"
        print(f"costream-sync: Discord login ON (application {DISCORD_CLIENT_ID}, {bot})", flush=True)
    else:
        print("costream-sync: dev mode (no DISCORD_CLIENT_ID in .env): name + room, no Discord login", flush=True)


@dataclass
class User:
    name: str                         # key: Discord user id (discord mode) or the typed name (dev mode)
    display: str = ""                 # what panels show
    extension: WebSocket | None = None
    panel: WebSocket | None = None
    status: dict | None = None
    status_time: float = 0.0          # time.monotonic() of the last status
    session: str | None = None
    room: "Room | None" = None        # changes when the user switches voice channel
    requests: collections.deque = field(default_factory=collections.deque)   # rate limiting

    def label(self) -> str:
        return self.display or self.name

    def member(self) -> Member | None:
        s = self.status
        if not s or s.get("in_ad") or time.monotonic() - self.status_time > STATUS_STALE:
            return None
        return Member(self.name, s["video_id"], s["live_edge"], s["current_delay"], s["max_delay"])

    def stamp(self) -> float | None:
        """This viewer's broadcast stamp K: wall-clock time = player time + K."""
        s = self.status
        if not s or s.get("player_time") is None or s.get("at") is None:
            return None
        return s["at"] - s["current_delay"] - s["player_time"]


@dataclass
class Offer:
    id: str
    kind: str                 # "match" or "sync_all"
    requester: str
    asked: set[str]
    plan: Plan
    answers: dict[str, bool] = field(default_factory=dict)
    done: asyncio.Event = field(default_factory=asyncio.Event)


@dataclass
class Session:
    id: str
    users: set[str]
    target_delay: float
    reference_stamp: float


class Room:
    def __init__(self, name: str, title: str = "") -> None:
        self.name = name                  # key: voice channel id (discord) or room name (dev)
        self.title = title or name        # what panels show
        self.users: dict[str, User] = {}  # who is in this room now
        self.offers: dict[str, Offer] = {}
        self.sessions: dict[str, Session] = {}
        self._group_task: asyncio.Task | None = None
        self._last_group = 0.0

    # Messages go through the global user list, so someone who switched channels during an
    # offer still gets its result.
    async def to_panel(self, name: str, msg: dict) -> None:
        await _send(users.get(name) and users[name].panel, msg)

    async def to_extension(self, name: str, msg: dict) -> None:
        await _send(users.get(name) and users[name].extension, msg)

    def group_msg(self) -> dict:
        members = []
        for u in self.users.values():
            s = u.status or {}
            members.append({
                "user": u.name, "name": u.label(), "extension": u.extension is not None, "panel": u.panel is not None,
                "video_id": s.get("video_id"), "current_delay": s.get("current_delay"),
                "live_edge": s.get("live_edge"), "max_delay": s.get("max_delay"),
                "in_ad": s.get("in_ad", False), "quality": s.get("quality"),
                "paused_by": s.get("paused_by"), "dvr_available": s.get("dvr_available"),
                "sync_error_ms": s.get("sync_error_ms"), "session_id": u.session,
            })
        return {"type": "group", "room": self.name, "room_name": self.title, "server_time": time.time(),
                "members": members}

    def group_changed(self) -> None:
        """Push `group` to panels soon, at most every GROUP_INTERVAL."""
        if self._group_task and not self._group_task.done():
            return
        wait = max(0.0, self._last_group + GROUP_INTERVAL - time.monotonic())
        self._group_task = asyncio.create_task(self._push_group(wait))

    async def _push_group(self, wait: float) -> None:
        await asyncio.sleep(wait)
        self._last_group = time.monotonic()
        msg = self.group_msg()
        for u in list(self.users.values()):
            await _send(u.panel, msg)


rooms: dict[str, Room] = {}
users: dict[str, User] = {}


def get_user(key: str) -> User:
    return users.setdefault(key, User(key))


async def move_user(user: User, room: Room | None) -> None:
    """Put `user` in `room` (None = in no voice channel). Leaving a room ends their sync."""
    old = user.room
    if old is room:
        return
    if old is not None:
        await leave_session(old, user.name)
        old.users.pop(user.name, None)
        old.group_changed()
    user.room = room
    if room is not None:
        room.users[user.name] = user
        room.group_changed()
    else:
        await _send(user.panel, {"type": "no_voice_channel"})
        await _send(user.extension, {"type": "no_voice_channel"})


def room_for_channel(channel_id: str | None) -> Room | None:
    if channel_id is None:
        return None
    room = rooms.setdefault(channel_id, Room(channel_id))
    room.title = voice.channel_names.get(channel_id, room.title)
    return room


def rate_limited(user: User) -> bool:
    n, window = RATE_LIMIT
    now = time.monotonic()
    while user.requests and user.requests[0] < now - window:
        user.requests.popleft()
    if len(user.requests) >= n:
        return True
    user.requests.append(now)
    return False


async def _send(ws: WebSocket | None, msg: dict) -> None:
    if ws is None:
        return
    try:
        await ws.send_json(msg)
    except Exception:
        pass  # its own handler cleans up on disconnect


def _num(msg: dict, key: str, lo: float, hi: float, required: bool = True) -> float | None:
    v = msg.get(key)
    if v is None and not required:
        return None
    if isinstance(v, bool) or not isinstance(v, (int, float)) or not math.isfinite(v) or not lo <= v <= hi:
        raise ValueError(f"{key} must be a number in [{lo}, {hi}]")
    return float(v)


def parse_status(msg: dict) -> dict:
    vid = msg.get("video_id")
    if not isinstance(vid, str) or not vid:
        raise ValueError("video_id must be a non-empty string")
    return {
        "video_id": vid,
        "current_delay": _num(msg, "current_delay", -5, 3600),
        "live_edge": _num(msg, "live_edge", -5, 3600),
        "max_delay": _num(msg, "max_delay", 0, 600),
        "player_time": _num(msg, "player_time", -1e7, 1e10, required=False),
        "at": _num(msg, "at", 1e9, 1e10, required=False),
        "in_ad": bool(msg.get("in_ad", False)),
        "dvr_available": msg.get("dvr_available"),
        "quality": msg.get("quality"),
        "paused_by": msg.get("paused_by"),
        "sync_error_ms": _num(msg, "sync_error_ms", -1e6, 1e6, required=False),
    }


# ------------------------------------------------------------------ offers


def names(keys) -> dict[str, str]:
    """Display names for panels (keys are Discord ids in discord mode)."""
    return {k: users[k].label() if k in users else k for k in keys}


async def refuse(room: Room, requester: str, kind: str, plan: Plan, reason: str) -> None:
    await room.to_panel(requester, {
        "type": "match_result", "offer_id": None, "kind": kind, "status": "refused", "reason": reason,
        "users": {u: "left_out" for u in plan.left_out}, "reasons": plan.left_out,
        "names": names([requester, *plan.left_out]),
        "group": [], "target_delay": plan.target, "session_id": None})


async def start_match(room: Room, requester: str, other: str) -> None:
    me, them = room.users[requester].member(), room.users.get(other) and room.users[other].member()
    if me is None:
        await room.to_panel(requester, {"type": "error", "message": "your extension isn't measuring yet (or an ad is playing)"})
        return
    if them is None or other == requester:
        await room.to_panel(requester, {"type": "error", "message": f"{other} isn't available to sync"})
        return
    plan = match_pair(me, them, MAX_GAP)
    if not plan.ok:
        await refuse(room, requester, "match", plan, "; ".join(plan.left_out.values()))
        return
    if room.users[other].panel is None:
        await room.to_panel(requester, {"type": "error", "message": f"{other} has no panel open to answer"})
        return
    await send_offer(room, Offer(f"o{next(_ids)}", "match", requester, {other}, plan))


async def start_sync_all(room: Room, requester: str) -> None:
    me = room.users[requester].member()
    if me is None:
        await room.to_panel(requester, {"type": "error", "message": "your extension isn't measuring yet (or an ad is playing)"})
        return
    members = [m for u in room.users.values() if (m := u.member()) is not None]
    plan = plan_group(members, must_include=requester, max_gap=MAX_GAP)
    asked = {u for u in plan.group if u != requester and room.users[u].panel is not None}
    for u in set(plan.group) - asked - {requester}:
        plan.left_out[u] = "no panel open to answer"
    if not asked:
        await refuse(room, requester, "sync_all", plan, "nobody else can join")
        return
    await send_offer(room, Offer(f"o{next(_ids)}", "sync_all", requester, asked, plan))


async def send_offer(room: Room, offer: Offer) -> None:
    room.offers[offer.id] = offer
    moves = {u: m.as_dict() for u, m in offer.plan.moves.items()}
    for user in offer.asked:
        # For sync_all the final group can only shrink, so the target can only drop.
        await room.to_panel(user, {
            "type": "match_offer", "offer_id": offer.id, "kind": offer.kind,
            "requester": offer.requester, "target_delay": offer.plan.target, "moves": moves,
            "your_move": moves[user], "left_out": offer.plan.left_out, "timeout": OFFER_TIMEOUT,
            "names": names([*moves, *offer.plan.left_out])})
    asyncio.create_task(finish_offer(room, offer))


async def finish_offer(room: Room, offer: Offer) -> None:
    try:
        await asyncio.wait_for(offer.done.wait(), OFFER_TIMEOUT)
    except asyncio.TimeoutError:
        pass
    room.offers.pop(offer.id, None)
    answers: dict[str, str] = {offer.requester: "requester"}
    for u in offer.asked:
        answers[u] = {True: "accepted", False: "declined"}.get(offer.answers.get(u), "timed_out")
    reasons = dict(offer.plan.left_out)
    for u in offer.plan.left_out:
        answers[u] = "left_out"

    # Recompute with fresh positions: people may have moved during the 15 s.
    # Someone who left the voice channel meanwhile is no longer in room.users.
    final = [m for u in [offer.requester, *sorted(offer.asked)]
             if answers[u] in ("requester", "accepted") and u in room.users
             and (m := room.users[u].member()) is not None]
    plan = plan_group(final, must_include=offer.requester, max_gap=MAX_GAP) if len(final) > 1 else None
    session = None
    if plan and plan.ok:
        for u, why in plan.left_out.items():
            answers[u], reasons[u] = "left_out", why
        session = await start_session(room, plan)

    if offer.kind == "match":
        answer = answers[next(iter(offer.asked))]
        if session:
            status = "accepted"
        elif answer == "accepted":
            status = "failed"      # accepted, but positions changed and they no longer fit
        else:
            status = answer        # "declined" or "timed_out"
    else:
        status = "synced" if session else "not_synced"
    result = {"type": "match_result", "offer_id": offer.id, "kind": offer.kind, "status": status,
              "users": answers, "reasons": reasons, "group": sorted(session.users) if session else [],
              "names": names(answers), "target_delay": session.target_delay if session else None,
              "session_id": session.id if session else None}
    for u in answers:
        await room.to_panel(u, result)
        await room.to_extension(u, result)
    if session:
        for u in session.users:
            await room.to_extension(u, set_target_msg(session))
    room.group_changed()


async def start_session(room: Room, plan: Plan) -> Session:
    stamps = [k for u in plan.group if (k := users[u].stamp()) is not None]
    # One reference stamp for everyone, so all clients convert delay <-> player clock alike.
    ref = statistics.median(stamps) if stamps else 0.0
    session = Session(f"s{next(_ids)}", set(plan.group), plan.target, ref)
    for u in plan.group:
        await leave_session(room, u, notify=False)
        users[u].session = session.id
    room.sessions[session.id] = session
    return session


def set_target_msg(s: Session) -> dict:
    return {"type": "set_target", "session_id": s.id, "reference_stamp": s.reference_stamp,
            "target_delay": s.target_delay, "target_player_offset": -(s.target_delay + s.reference_stamp)}


async def leave_session(room: Room, user: str, notify: bool = True) -> None:
    sid = users[user].session if user in users else None
    session = room.sessions.get(sid) if sid else None
    if user in users:
        users[user].session = None
    if session is None:
        return
    session.users.discard(user)
    if notify:
        await room.to_extension(user, {"type": "leave_sync", "session_id": sid})
    if len(session.users) < 2:
        # Nobody left to be in sync with: hand back normal playback.
        for u in list(session.users):
            users[u].session = None
            await room.to_extension(u, {"type": "leave_sync", "session_id": sid})
        del room.sessions[sid]
    room.group_changed()


# ------------------------------------------------------------------ socket


async def reject(ws: WebSocket, message: str, code: str = "bad_hello") -> None:
    await ws.send_json({"type": "error", "code": code, "message": message})
    await ws.close()


async def identify(ws: WebSocket, hello: dict) -> tuple[User, Room | None] | None:
    """Who is connecting. In discord mode the user id comes from Discord's answer to the token,
    never from anything the client claims (a "user" field in hello is ignored)."""
    if AUTH_MODE == "dev":
        name, room_name = hello.get("user"), hello.get("room")
        if not isinstance(name, str) or not name or not isinstance(room_name, str) or not room_name:
            await reject(ws, "first message must be hello with role, user and room")
            return None
        user = get_user(name)
        return user, rooms.setdefault(room_name, Room(room_name))
    ident = await auth.identify(hello.get("token"))
    if ident is None:
        await reject(ws, "Discord login invalid or expired: log in with Discord again", code="auth")
        return None
    user = get_user(ident.id)
    user.display = ident.name
    return user, room_for_channel(voice.channel_of.get(ident.id))


async def run_bot(ws: WebSocket) -> None:
    """The Discord bot reports who is in which voice channel (voice_members)."""
    await ws.send_json({"type": "hello_ok", "role": "bot"})
    print("costream-sync: Discord bot connected", flush=True)
    while True:
        msg = await ws.receive_json()
        if msg.get("type") != "voice_members":
            continue
        channel_id, members = str(msg.get("channel_id", "")), msg.get("members")
        if not channel_id or not isinstance(members, dict):
            await ws.send_json({"type": "error", "message": "voice_members needs channel_id and members"})
            continue
        moved = voice.update(channel_id, str(msg.get("channel_name") or channel_id),
                             {str(k): str(v) for k, v in members.items()})
        for uid, name in members.items():
            get_user(str(uid)).display = str(name)
        for uid, channel in moved.items():
            await move_user(get_user(uid), room_for_channel(channel))
        if channel_id in rooms:
            room_for_channel(channel_id).group_changed()


@app.websocket("/ws")
async def ws_endpoint(ws: WebSocket) -> None:
    await ws.accept()
    # Tells the panel / extension how to log in before they say hello.
    await ws.send_json({"type": "welcome", "auth": AUTH_MODE, "discord_client_id": DISCORD_CLIENT_ID,
                        "discord_authorize_url": DISCORD_AUTHORIZE_URL})
    user: User | None = None
    role = None
    try:
        hello = await ws.receive_json()
        if hello.get("type") == "whoami":
            # The extension popup shows the logged-in name; nothing gets registered.
            ident = await auth.identify(hello.get("token")) if AUTH_MODE == "discord" else None
            await ws.send_json({"type": "whoami", "ok": ident is not None,
                                "user": ident and ident.id, "name": ident and ident.name})
            await ws.close()
            return
        role = hello.get("role")
        if hello.get("type") != "hello" or role not in ("extension", "panel", "bot"):
            await reject(ws, "first message must be hello with a role")
            return
        if role == "bot":
            secret = str(hello.get("secret") or "")
            if not BOT_SHARED_SECRET or not hmac.compare_digest(secret, BOT_SHARED_SECRET):
                await reject(ws, "bot key doesn't match BOT_SHARED_SECRET", code="auth")
                return
            await run_bot(ws)
            return
        found = await identify(ws, hello)
        if found is None:
            return
        user, room = found
        if getattr(user, role) is not None:
            await reject(ws, f"{user.label()} already has a {role} connected")
            user = None
            return
        # Pairing: the panel and the extension of the same Discord account are the same User.
        setattr(user, role, ws)
        await move_user(user, room)
        await ws.send_json({"type": "hello_ok", "user": user.name, "name": user.label(),
                            "room": room.name if room else None, "room_name": room.title if room else None})
        if room is None:
            await ws.send_json({"type": "no_voice_channel"})
        elif role == "panel":
            await ws.send_json(room.group_msg())

        while True:
            msg = await ws.receive_json()
            t1 = time.time()
            kind = msg.get("type")
            room = user.room   # can change while connected (voice channel switch)
            try:
                if kind == "clock_ping":
                    await ws.send_json({"type": "clock_pong", "t0": msg.get("t0"), "t1": t1, "t2": time.time()})
                elif kind == "status" and role == "extension":
                    user.status, user.status_time = parse_status(msg), time.monotonic()
                    if room:
                        room.group_changed()
                elif role != "panel" and kind in ("match_request", "sync_all_request", "match_answer"):
                    raise ValueError(f"unknown or not allowed for {role}: {kind!r}")
                elif kind in ("match_request", "sync_all_request", "match_answer") and room is None:
                    raise ValueError("join a voice channel (with the costream bot) to sync")
                elif kind in ("match_request", "sync_all_request") and rate_limited(user):
                    raise ValueError("too many requests: wait a few seconds")
                elif kind == "match_request":
                    await start_match(room, user.name, str(msg.get("target_user")))
                elif kind == "sync_all_request":
                    await start_sync_all(room, user.name)
                elif kind == "match_answer":
                    offer = room.offers.get(str(msg.get("offer_id")))
                    if offer is None or user.name not in offer.asked:
                        raise ValueError("no such open offer")
                    offer.answers[user.name] = msg.get("accept") is True
                    if set(offer.answers) >= offer.asked:
                        offer.done.set()
                elif kind == "leave_sync":
                    if room is None or msg.get("session_id") != user.session:
                        raise ValueError("not in that sync session")
                    await leave_session(room, user.name)
                else:
                    raise ValueError(f"unknown or not allowed for {role}: {kind!r}")
            except ValueError as exc:
                await ws.send_json({"type": "error", "message": str(exc)})
    except WebSocketDisconnect:
        pass
    finally:
        if user is not None and role in ("extension", "panel") and getattr(user, role) is ws:
            setattr(user, role, None)
            room = user.room
            if role == "extension":
                user.status = None
                if room:
                    await leave_session(room, user.name, notify=False)
            if room:
                room.group_changed()