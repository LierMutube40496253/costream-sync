"""delay_probe.py - measure how far a live HLS stream lags behind real time.

How it works, with an example:
  The HLS media playlist lists the newest few chunks ("segments"). Many servers put a
  wall-clock stamp before a segment:

      #EXT-X-PROGRAM-DATE-TIME:2026-10-21T18:00:00.000Z
      #EXTINF:2.000,
      seg_1001.ts

  That means "the first frame of seg_1001.ts was captured at 18:00:00.000 UTC".
  If our NTP-corrected clock says 18:00:07.500 when we read the playlist, and the
  newest segment is seg_1001 (2 s long, so it ends at 18:00:02.000), then:

      edge delay   = 18:00:07.5 - 18:00:02.0 = 5.5 s   (newest moment you *could* see)
      player delay = edge delay + the buffer a normal player keeps behind the edge.
                     A player that starts 3 segments back (streamlink's default
                     --hls-live-edge 3) sees the start of the 3rd-newest segment, e.g. 9.5 s.

Usage:
  python delay_probe.py https://www.twitch.tv/riotgames --duration 60 --interval 2
  python delay_probe.py https://www.youtube.com/watch?v=XXXX --dump   (print raw playlist)
  python delay_probe.py --self-test
"""

from __future__ import annotations

import argparse
import statistics
import sys
import time
from dataclasses import dataclass
from datetime import datetime, timezone

# --------------------------------------------------------------------------- clock


class NtpClock:
    """time.time() corrected by an NTP offset (offset = true_time - local_time)."""

    def __init__(self, server: str = "pool.ntp.org", enabled: bool = True):
        self.offset = 0.0
        self.source = "local clock (NTP disabled)"
        if not enabled:
            return
        try:
            import ntplib

            resp = ntplib.NTPClient().request(server, version=3, timeout=3)
            self.offset = resp.offset
            self.source = f"NTP {server}, offset {resp.offset * 1000:+.1f} ms, rtt {resp.delay * 1000:.1f} ms"
        except Exception as exc:  # UDP 123 blocked, no network, ...
            self.source = f"local clock (NTP failed: {exc})"

    def now(self) -> float:
        return time.time() + self.offset


# ------------------------------------------------------------------------ playlist


@dataclass
class Segment:
    uri: str
    duration: float
    pdt: float | None          # wall-clock start (unix seconds), None if unknown
    pdt_explicit: bool         # True if a PDT tag sat directly on this segment


@dataclass
class Playlist:
    segments: list[Segment]
    target_duration: float | None
    media_sequence: int | None
    prefetch: int              # Twitch low-latency "#EXT-X-TWITCH-PREFETCH" entries
    pdt_tags: int


def parse_pdt(value: str) -> float:
    value = value.strip()
    if value.endswith("Z"):
        value = value[:-1] + "+00:00"
    dt = datetime.fromisoformat(value)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.timestamp()


def parse_playlist(text: str) -> Playlist:
    segments: list[Segment] = []
    target = None
    seq = None
    prefetch = 0
    pdt_tags = 0
    pending_pdt = None
    pending_dur = None
    for raw in text.splitlines():
        line = raw.strip()
        if not line:
            continue
        if line.startswith("#EXT-X-TARGETDURATION:"):
            target = float(line.split(":", 1)[1])
        elif line.startswith("#EXT-X-MEDIA-SEQUENCE:"):
            seq = int(line.split(":", 1)[1])
        elif line.startswith("#EXT-X-PROGRAM-DATE-TIME:"):
            pending_pdt = parse_pdt(line.split(":", 1)[1])
            pdt_tags += 1
        elif line.startswith("#EXTINF:"):
            pending_dur = float(line.split(":", 1)[1].split(",")[0])
        elif line.startswith("#EXT-X-TWITCH-PREFETCH:"):
            prefetch += 1
        elif line.startswith("#EXT-X-DISCONTINUITY"):
            pass  # PDT after a discontinuity resets the timeline; handled by explicit tags
        elif not line.startswith("#") and pending_dur is not None:
            if pending_pdt is not None:
                pdt, explicit = pending_pdt, True
            elif segments and segments[-1].pdt is not None:
                # No tag on this segment: previous start + previous duration.
                pdt, explicit = segments[-1].pdt + segments[-1].duration, False
            else:
                pdt, explicit = None, False
            segments.append(Segment(line, pending_dur, pdt, explicit))
            pending_pdt = pending_dur = None
    return Playlist(segments, target, seq, prefetch, pdt_tags)


@dataclass
class Measurement:
    edge_delay: float      # now - end of newest segment
    player_delay: float    # now - start of the segment `live_edge` from the end
    newest_pdt: float


def measure(pl: Playlist, now: float, live_edge: int = 3) -> Measurement | None:
    timed = [s for s in pl.segments if s.pdt is not None]
    if not timed:
        return None
    newest = timed[-1]
    start_seg = timed[-min(live_edge, len(timed))]
    return Measurement(
        edge_delay=now - (newest.pdt + newest.duration),
        player_delay=now - start_seg.pdt,
        newest_pdt=newest.pdt,
    )


# -------------------------------------------------------------------------- stream


def resolve_media_playlist(url: str, quality: str) -> tuple[str, str]:
    """Return (media playlist URL, chosen quality name) via streamlink."""
    import streamlink
    from streamlink.stream.hls import HLSStream

    session = streamlink.Streamlink()
    streams = session.streams(url)
    if not streams:
        raise SystemExit(f"No streams found for {url} (offline?)")
    name = quality if quality in streams else "best"
    stream = streams[name]
    if not isinstance(stream, HLSStream):
        raise SystemExit(f"Stream '{name}' is {type(stream).__name__}, not HLS")
    return stream.url, name


def run_live(args) -> int:
    import requests

    clock = NtpClock(enabled=not args.no_ntp)
    print(f"clock: {clock.source}")
    playlist_url, qname = resolve_media_playlist(args.url, args.quality)
    print(f"quality: {qname}")
    print(f"playlist: {playlist_url[:110]}{'...' if len(playlist_url) > 110 else ''}")

    http = requests.Session()
    http.headers["User-Agent"] = "Mozilla/5.0 delay_probe"

    if args.dump:
        print(http.get(playlist_url, timeout=10).text)
        return 0

    rows = []
    t_end = time.monotonic() + args.duration
    print(f"\n{'t':>5} {'segs':>4} {'pdt':>4} {'dur':>5} {'edge':>7} {'player':>7} {'newest PDT (UTC)':>24}")
    t0 = time.monotonic()
    while True:
        text = http.get(playlist_url, timeout=10).text
        now = clock.now()
        pl = parse_playlist(text)
        m = measure(pl, now, args.live_edge)
        t = time.monotonic() - t0
        avg_dur = statistics.mean(s.duration for s in pl.segments) if pl.segments else 0
        if m is None:
            print(f"{t:5.1f} {len(pl.segments):4d} {pl.pdt_tags:4d} {avg_dur:5.2f}   -- no PDT tags in playlist --")
        else:
            rows.append(m)
            stamp = datetime.fromtimestamp(m.newest_pdt, timezone.utc).strftime("%H:%M:%S.%f")[:-3]
            print(f"{t:5.1f} {len(pl.segments):4d} {pl.pdt_tags:4d} {avg_dur:5.2f} "
                  f"{m.edge_delay:7.2f} {m.player_delay:7.2f} {stamp:>24}")
        if time.monotonic() >= t_end:
            break
        time.sleep(args.interval)

    print()
    if not rows:
        print("RESULT: no #EXT-X-PROGRAM-DATE-TIME tags -> PDT method not usable for this source")
        return 2
    for label, vals in (("edge", [r.edge_delay for r in rows]), ("player", [r.player_delay for r in rows])):
        sd = statistics.pstdev(vals)
        print(f"{label:>6} delay: mean {statistics.mean(vals):6.2f} s  min {min(vals):6.2f}  "
              f"max {max(vals):6.2f}  stdev {sd:5.2f}  (n={len(vals)})")
    # Expect a sawtooth: edge delay grows between playlist updates and drops when a new
    # segment appears, so min..max spans about one segment duration.
    return 0


# ------------------------------------------------------------------------ self-test

SAMPLE = """#EXTM3U
#EXT-X-VERSION:3
#EXT-X-TARGETDURATION:2
#EXT-X-MEDIA-SEQUENCE:1000
#EXT-X-PROGRAM-DATE-TIME:2026-10-21T18:00:00.000Z
#EXTINF:2.000,live
seg_1000.ts
#EXTINF:2.000,live
seg_1001.ts
#EXT-X-PROGRAM-DATE-TIME:2026-10-21T18:00:04.000+00:00
#EXTINF:2.000,live
seg_1002.ts
#EXTINF:2.000,live
seg_1003.ts
#EXT-X-TWITCH-PREFETCH:https://example/seg_1004.ts
"""


def self_test() -> int:
    pl = parse_playlist(SAMPLE)
    assert len(pl.segments) == 4, pl.segments
    assert pl.target_duration == 2 and pl.media_sequence == 1000
    assert pl.pdt_tags == 2 and pl.prefetch == 1
    base = parse_pdt("2026-10-21T18:00:00Z")
    assert [s.pdt - base for s in pl.segments] == [0, 2, 4, 6]
    assert [s.pdt_explicit for s in pl.segments] == [True, False, True, False]
    # Clock reads 18:00:13.5 -> newest segment ends at 18:00:08 -> edge 5.5 s.
    # live_edge=3 -> player starts at seg_1001 (18:00:02) -> player delay 11.5 s.
    m = measure(pl, base + 13.5, live_edge=3)
    assert abs(m.edge_delay - 5.5) < 1e-6, m
    assert abs(m.player_delay - 11.5) < 1e-6, m
    assert measure(parse_playlist("#EXTM3U\n#EXTINF:2,\na.ts\n"), base) is None
    print("self-test OK")
    return 0


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("url", nargs="?", help="Twitch/YouTube/any streamlink-supported live URL")
    ap.add_argument("--quality", default="best")
    ap.add_argument("--duration", type=float, default=60, help="seconds to sample")
    ap.add_argument("--interval", type=float, default=2, help="seconds between playlist fetches")
    ap.add_argument("--live-edge", type=int, default=3, help="segments a player keeps behind the edge")
    ap.add_argument("--no-ntp", action="store_true")
    ap.add_argument("--dump", action="store_true", help="print the raw media playlist and exit")
    ap.add_argument("--self-test", action="store_true")
    args = ap.parse_args()
    if args.self_test:
        return self_test()
    if not args.url:
        ap.error("url is required (or --self-test)")
    return run_live(args)


if __name__ == "__main__":
    sys.exit(main())
