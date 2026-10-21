from types import SimpleNamespace as NS

from server.bot import changed_channels, snapshot


def member(id_, name, bot=False):
    return NS(id=id_, display_name=name, bot=bot)


def test_snapshot_lists_people_not_bots():
    ch = NS(id=111, name="LCK casting", members=[member(1001, "Lier"), member(1002, "Sam"), member(5, "costream", bot=True)])
    assert snapshot(ch) == {"type": "voice_members", "channel_id": "111", "channel_name": "LCK casting",
                            "members": {"1001": "Lier", "1002": "Sam"}}


def test_switching_channels_updates_both():
    a, b = NS(id=111), NS(id=222)
    assert [c.id for c in changed_channels(NS(channel=a), NS(channel=b))] == [111, 222]
    assert [c.id for c in changed_channels(NS(channel=None), NS(channel=b))] == [222]   # joined
    assert [c.id for c in changed_channels(NS(channel=a), NS(channel=None))] == [111]   # left
    assert [c.id for c in changed_channels(NS(channel=a), NS(channel=a))] == [111]      # mute etc.
