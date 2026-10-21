"""Who is this? (docs/DESIGN.md, Safety: "Discord login means nobody can pretend to be another
user").

The panel and the extension log in with Discord themselves (scope `identify`, so we only learn
the user's id and name) and send the access token in `hello`. The server asks Discord whose
token it is. No Discord client secret is needed anywhere.

Example: token "abc" -> GET https://discord.com/api/users/@me -> {"id": "8123...", "global_name": "Lier"}
-> Identity(id="8123...", name="Lier"). Cached for 10 minutes so reconnects don't hit Discord.
"""

from __future__ import annotations

import hashlib
import time
from dataclasses import dataclass

import httpx

CACHE_SECONDS = 600


@dataclass(frozen=True)
class Identity:
    id: str
    name: str


class DiscordAuth:
    def __init__(self, api_base: str = "https://discord.com/api", transport: httpx.AsyncBaseTransport | None = None):
        self.api_base = api_base.rstrip("/")
        self.transport = transport
        self._cache: dict[str, tuple[float, Identity]] = {}

    async def identify(self, token: str) -> Identity | None:
        """The Discord user behind `token`, or None if Discord doesn't accept it."""
        if not isinstance(token, str) or not 10 <= len(token) <= 200:
            return None
        key = hashlib.sha256(token.encode()).hexdigest()   # never keep raw tokens around
        hit = self._cache.get(key)
        if hit and hit[0] > time.monotonic():
            return hit[1]
        try:
            async with httpx.AsyncClient(transport=self.transport, timeout=10) as client:
                r = await client.get(f"{self.api_base}/users/@me", headers={"Authorization": f"Bearer {token}"})
        except httpx.HTTPError:
            return None
        if r.status_code != 200:
            return None
        data = r.json()
        if not data.get("id"):
            return None
        ident = Identity(str(data["id"]), data.get("global_name") or data.get("username") or str(data["id"]))
        self._cache[key] = (time.monotonic() + CACHE_SECONDS, ident)
        return ident
