"""Who is in which Discord voice channel (docs/DESIGN.md: groups = voice channel members).

The bot sends one snapshot per channel whenever someone joins or leaves:
    {"type": "voice_members", "channel_id": "111", "channel_name": "LCK casting",
     "members": {"8123": "Lier", "9456": "Sam"}}
`update()` returns who moved, so the server can move them between rooms.

Example: Sam moves from channel 111 to 222. The bot sends 111 without Sam and 222 with Sam;
the first update reports Sam as left (None), the second as joined 222.
"""

from __future__ import annotations

from dataclasses import dataclass, field


@dataclass
class VoiceDirectory:
    channel_of: dict[str, str] = field(default_factory=dict)       # user id -> channel id
    channel_names: dict[str, str] = field(default_factory=dict)    # channel id -> name
    names: dict[str, str] = field(default_factory=dict)            # user id -> display name

    def members(self, channel_id: str) -> set[str]:
        return {u for u, c in self.channel_of.items() if c == channel_id}

    def update(self, channel_id: str, channel_name: str, members: dict[str, str]) -> dict[str, str | None]:
        """Apply one channel snapshot. Returns {user id: new channel id or None} for users who moved."""
        self.channel_names[channel_id] = channel_name
        moved: dict[str, str | None] = {}
        for uid in self.members(channel_id) - set(members):
            del self.channel_of[uid]
            moved[uid] = None
        for uid, name in members.items():
            self.names[uid] = name
            if self.channel_of.get(uid) != channel_id:
                self.channel_of[uid] = channel_id
                moved[uid] = channel_id
        return moved
