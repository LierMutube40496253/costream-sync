"""Discord bot (docs/DESIGN.md: "tells the server who is in which voice channel").

Run next to the server, with DISCORD_BOT_TOKEN and BOT_SHARED_SECRET in .env:
    python -m server.bot
It needs no privileged intents: voice state events already carry the member and their name.
Invite it to your Discord server once with the "View Channels" permission.

On start, and whenever someone joins, leaves or switches a voice channel, it sends one
snapshot per affected channel to the sync server:
    {"type": "voice_members", "channel_id": "111", "channel_name": "LCK casting",
     "members": {"8123": "Lier", "9456": "Sam"}}
"""

from __future__ import annotations

import asyncio
import json
import os
import sys
from pathlib import Path

from .__main__ import load_env


def snapshot(channel) -> dict:
    """voice_members message for a discord.py voice channel (anything with .id, .name and
    .members, whose members have .id, .display_name and .bot)."""
    return {"type": "voice_members", "channel_id": str(channel.id), "channel_name": channel.name,
            "members": {str(m.id): m.display_name for m in channel.members if not m.bot}}


def changed_channels(before, after) -> list:
    """Voice channels whose member list changed in a voice_state_update."""
    out = []
    for ch in (before.channel, after.channel):
        if ch is not None and all(ch.id != c.id for c in out):
            out.append(ch)
    return out


async def run(server_url: str, bot_token: str, secret: str) -> None:
    import discord
    import websockets

    queue: asyncio.Queue = asyncio.Queue()
    intents = discord.Intents.none()
    intents.guilds = True
    intents.voice_states = True
    client = discord.Client(intents=intents)

    @client.event
    async def on_ready():
        print(f"bot logged in as {client.user}; watching {len(client.guilds)} server(s)", flush=True)
        for guild in client.guilds:
            for ch in guild.voice_channels:
                queue.put_nowait(snapshot(ch))

    @client.event
    async def on_guild_join(guild):
        # Invited while running: people may already sit in its voice channels.
        print(f"added to {guild.name}; watching {len(client.guilds)} server(s)", flush=True)
        for ch in guild.voice_channels:
            queue.put_nowait(snapshot(ch))

    @client.event
    async def on_voice_state_update(member, before, after):
        for ch in changed_channels(before, after):
            queue.put_nowait(snapshot(ch))

    async def forward():
        """Keep a connection to the sync server and pass snapshots on; reconnect on errors."""
        while True:
            try:
                async with websockets.connect(server_url) as ws:
                    await ws.recv()   # welcome
                    await ws.send(json.dumps({"type": "hello", "role": "bot", "secret": secret}))
                    reply = json.loads(await ws.recv())
                    if reply.get("type") != "hello_ok":
                        print(f"sync server refused the bot: {reply.get('message')}", flush=True)
                        await asyncio.sleep(30)
                        continue
                    print(f"connected to {server_url}", flush=True)
                    if client.is_ready():   # resend everything after a reconnect
                        for guild in client.guilds:
                            for ch in guild.voice_channels:
                                queue.put_nowait(snapshot(ch))
                    while True:
                        await ws.send(json.dumps(await queue.get()))
            except (OSError, websockets.WebSocketException) as exc:
                print(f"sync server connection lost ({exc}); retrying in 5 s", flush=True)
                await asyncio.sleep(5)

    asyncio.create_task(forward())
    await client.start(bot_token)


if __name__ == "__main__":
    load_env(Path(__file__).resolve().parent.parent / ".env")
    token, secret = os.environ.get("DISCORD_BOT_TOKEN", ""), os.environ.get("BOT_SHARED_SECRET", "")
    if not token or not secret or token.startswith("your-") or secret.startswith("your-"):
        sys.exit("Set DISCORD_BOT_TOKEN and BOT_SHARED_SECRET in .env (see .env.example and docs/DISCORD_SETUP.md)")
    port = os.environ.get("COSTREAM_PORT", "8765")
    url = os.environ.get("COSTREAM_BOT_SERVER", f"ws://127.0.0.1:{port}/ws")
    asyncio.run(run(url, token, secret))
