"""Two-window demo: two Chromium windows (two separate profiles = two streamers) watch the
same YouTube live stream with the extension loaded. B starts ~12 s further behind than A.
A asks to match B, B accepts, and the extension in A pauses and then fine-tunes until both
windows show the same frame.

Two independent measurements are logged every second:
  * truth_ms: from the outside. Each window reports, via requestVideoFrameCallback, which
    frame (player time) it presented and when. Both windows run on this PC's clock, so
        offset = (playerA - wallA) - (playerB - wallB)
    is how far apart the two viewers are, without using the extension's own math.
    The two panels are played by this script over WebSocket (A clicks Match, B accepts).
    Positive = A is ahead (sees things earlier than B).
  * the extension's own view: each window's measured delay and its error vs the target.

Run (the server is started for you):
  .\\.venv\\Scripts\\python.exe demo\\two_window_demo.py --url "https://www.youtube.com/watch?v=<live id>"
Output: demo/demo_log.csv and demo/demo_summary.txt
"""

from __future__ import annotations

import argparse
import asyncio
import csv
import json
import math
import statistics
import subprocess
import sys
import tempfile
import time
from pathlib import Path

import websockets
from playwright.async_api import async_playwright

ROOT = Path(__file__).resolve().parent.parent
EXT = ROOT / "extension"
OUT = ROOT / "demo"
PORT = 8765

FRAME_PROBE = """() => new Promise((resolve) => {
  const p = document.getElementById('movie_player');
  const v = p && p.querySelector('video.html5-main-video, video');
  if (!p || !v || p.classList.contains('ad-showing') || v.paused) return resolve(null);
  const timer = setTimeout(() => resolve(null), 500);
  v.requestVideoFrameCallback((_now, md) => {
    clearTimeout(timer);
    const perfToWall = Date.now() / 1000 - performance.now() / 1000;
    resolve({ wall: perfToWall + md.expectedDisplayTime / 1000,
              player: md.mediaTime + (p.getCurrentTime() - v.currentTime) });
  });
})"""

PLAYER_READY = """() => {
  const p = document.getElementById('movie_player');
  const v = p && p.querySelector('video');
  if (!p || !v || !p.getVideoData) return 'no player';
  if (p.classList.contains('ad-showing')) {
    const skip = p.querySelector('.ytp-skip-ad-button, .ytp-ad-skip-button, .ytp-ad-skip-button-modern');
    if (skip && skip.offsetParent) { skip.click(); return 'ad (skipped)'; }
    if (v.paused) { v.play(); return 'ad (paused, restarted)'; }
    return 'ad ' + Math.round(v.currentTime) + '/' + Math.round(v.duration || 0) + ' s';
  }
  if (!p.getVideoData().isLive) return 'not live';
  if (v.paused) { v.play(); return 'paused'; }
  return 'ok';
}"""


async def open_viewer(pw, name: str, url: str, x: int):
    profile = tempfile.mkdtemp(prefix=f"costream-{name}-")
    ctx = await pw.chromium.launch_persistent_context(
        profile, headless=False, viewport={"width": 940, "height": 620},
        args=[f"--disable-extensions-except={EXT}", f"--load-extension={EXT}",
              "--mute-audio", "--autoplay-policy=no-user-gesture-required",
              f"--window-position={x},40", "--window-size=960,760"])
    page = ctx.pages[0] if ctx.pages else await ctx.new_page()
    await page.goto(url, wait_until="domcontentloaded")
    if "consent." in page.url:
        # Most privacy-preserving choice on YouTube's cookie page.
        await page.get_by_role("button", name="Reject all").first.click()
        await page.wait_for_url("**/www.youtube.com/**", timeout=30000)
    sw = ctx.service_workers[0] if ctx.service_workers else await ctx.wait_for_event("serviceworker")
    return ctx, page, sw


async def dismiss_consent_dialog(page) -> bool:
    """YouTube may also show the cookie dialog on top of the watch page; the video stays
    paused until it is answered."""
    # Match the visible text: on this dialog the button's accessible name is a longer
    # description, so get_by_role(name="Reject all") finds nothing.
    button = page.locator("button", has_text="Reject all")
    try:
        if await button.count() and await button.first.is_visible():
            await button.first.scroll_into_view_if_needed()
            await button.first.click()
            return True
    except Exception:
        pass
    return False


async def wait_playing(page, name: str, timeout: float = 240):
    """Ready = no ad, live, and currentTime advancing for 3 s in a row."""
    t0 = time.monotonic()
    last, last_print, good, prev_ct = None, 0.0, 0, None
    while time.monotonic() - t0 < timeout:
        if await dismiss_consent_dialog(page):
            print(f"  {name}: rejected cookies in the on-page dialog")
        try:
            state = await page.evaluate(PLAYER_READY)
            ct = await page.evaluate("() => { const v = document.querySelector('#movie_player video'); return v ? v.currentTime : null; }")
        except Exception:
            state, ct = "page reloading", None   # YouTube navigated; try again next second
        moving = state == "ok" and prev_ct is not None and ct is not None and 0.5 < ct - prev_ct < 2
        prev_ct = ct
        good = good + 1 if moving else 0
        label = "playing" if moving else state
        # Print changes; an ad's progress ("ad 12/30 s") only every 10 s.
        kind = "ad" if label.startswith("ad ") else label
        if kind != last or (kind == "ad" and time.monotonic() - last_print >= 10):
            print(f"  {name}: {label}")
            last, last_print = kind, time.monotonic()
        if good >= 3:
            return
        await asyncio.sleep(1)
    await page.screenshot(path=str(OUT / f"not_ready_{name}.png"))
    raise SystemExit(f"{name}: player never became ready ({last}); see demo/not_ready_{name}.png")


async def frame_probe(page):
    try:
        return await page.evaluate(FRAME_PROBE)
    except Exception:
        return None


async def ext_state(sw) -> dict:
    try:
        states = await sw.evaluate("costream.state()")
    except Exception:
        return {}
    return next(iter(states.values()), {}) if states else {}


class ExtLog:
    """Appends new extension log lines to a file every second, so a crash can't lose them."""

    def __init__(self, path: Path):
        self.path = path
        self.seen: set = set()
        path.write_text("")

    def add(self, entries, t: float) -> None:
        new = [e for e in entries or [] if (e["t"], e["text"]) not in self.seen]
        if not new:
            return
        with open(self.path, "a", encoding="utf-8") as f:
            for e in new:
                self.seen.add((e["t"], e["text"]))
                f.write(f"{t:7.1f}  {e['text']}\n")
                print(f"    [ext] {e['text']}")


class Panel:
    """Stands in for a user's panel page: same messages the real panel will send."""

    def __init__(self, user: str, room: str, accept: bool = True):
        self.user, self.room, self.accept = user, room, accept
        self.group: dict = {}
        self.results: list = []
        self.ws = None

    async def run(self):
        async with websockets.connect(f"ws://localhost:{PORT}/ws") as ws:
            self.ws = ws
            await ws.send(json.dumps({"type": "hello", "role": "panel", "user": self.user, "room": self.room}))
            async for raw in ws:
                msg = json.loads(raw)
                if msg["type"] == "group":
                    self.group = {m["user"]: m for m in msg["members"]}
                elif msg["type"] == "match_offer":
                    print(f">>> {self.user}'s panel: '{msg['requester']} wants to match timing with you. "
                          f"Accepting will {msg['your_move']['text']}.' -> {'accept' if self.accept else 'decline'}")
                    await ws.send(json.dumps({"type": "match_answer", "offer_id": msg["offer_id"], "accept": self.accept}))
                elif msg["type"] == "match_result":
                    self.results.append(msg)
                    print(f">>> {self.user}'s panel: match {msg['status']}, group {msg['group']}, target {msg['target_delay']}")
                    if msg.get("reason"):
                        print(f"    reason: {msg['reason']}")
                    for u, why in (msg.get("reasons") or {}).items():
                        print(f"    {u}: {why}")
                    for u, m in self.group.items():
                        print(f"    {u} as the server saw it: current {m.get('current_delay')} s, live edge {m.get('live_edge')} s, max {m.get('max_delay')} s")
                elif msg["type"] == "error":
                    print(f">>> {self.user}'s panel error: {msg['message']}")

    async def send(self, obj):
        await self.ws.send(json.dumps(obj))


YOUTUBE_ERROR = """() => {
  const e = document.querySelector('.ytp-error');
  return !!e && e.offsetParent !== null ? (e.innerText || 'error').slice(0, 80) : null;
}"""


_error_showing: dict[str, bool] = {}


async def handle_youtube_error(page, name: str) -> str | None:
    """Log (once) when YouTube shows "Something went wrong". No auto-reload: reloading
    brought a preroll ad, during which the other window errored and reloaded too, in a loop."""
    try:
        err = await page.evaluate(YOUTUBE_ERROR)
    except Exception:
        return None   # page is navigating right now; check again next second
    was = _error_showing.get(name, False)
    _error_showing[name] = bool(err)
    if not err or was:
        return None
    await page.screenshot(path=str(OUT / f"youtube_error_{name}.png"))
    return f"YouTube showed '{err.splitlines()[0]}' in {name} (not reloading)"


async def handle_youtube_pause(page, name: str, state: dict, shots: set) -> str | None:
    """Design doc, Robustness: if YouTube paused the video itself (cookie dialog, autoplay
    rules), the extension waits. Here we act as the streamer: answer the dialog, press play."""
    try:
        paused = await page.evaluate("() => { const v = document.querySelector('#movie_player video'); return !!v && v.paused; }")
    except Exception:
        return None   # page is navigating (e.g. YouTube reloaded it); check again next second
    if not paused or state.get("c", {}).get("state") == "pausing":
        return None
    seen_as = state.get("c", {}).get("state")
    key = f"{name}-{seen_as}"
    if key not in shots:
        shots.add(key)
        await page.screenshot(path=str(OUT / f"paused_{name}_{len(shots)}.png"))
    dialog = await dismiss_consent_dialog(page)
    try:
        await page.evaluate("() => document.querySelector('#movie_player video').play()")
    except Exception:
        return None
    return f"{name} was paused ({seen_as}){' by the cookie dialog' if dialog else ''}; pressed play"


def fmt(x, nd=3):
    return "" if x is None else f"{x:.{nd}f}"


async def main(args):
    OUT.mkdir(exist_ok=True)
    server_log = open(OUT / "server.log", "w")
    server = subprocess.Popen([sys.executable, "-m", "uvicorn", "server.app:app", "--port", str(PORT)],
                              cwd=ROOT, stdout=server_log, stderr=subprocess.STDOUT)
    rows: list[dict] = []
    try:
        await asyncio.sleep(2)
        async with async_playwright() as pw:
            print("opening two windows ...")
            (ctx_a, page_a, sw_a), (ctx_b, page_b, sw_b) = await asyncio.gather(
                open_viewer(pw, "A", args.url, 0), open_viewer(pw, "B", args.url, 960))
            print("waiting for both players (ads, consent, autoplay) ...")
            await asyncio.gather(wait_playing(page_a, "A"), wait_playing(page_b, "B"))

            # Different starting delays: A at YouTube's live head, B ~12 s further back.
            await page_a.evaluate("() => document.getElementById('movie_player').seekToLiveHead()")
            await asyncio.sleep(3)
            await page_b.evaluate(f"() => {{ const v = document.querySelector('#movie_player video'); v.currentTime -= {args.b_behind}; }}")
            await asyncio.sleep(3)

            await sw_a.evaluate(f"costream.join({{server: 'ws://localhost:{PORT}', room: '{args.room}', user: 'A', maxDelay: {args.max_a}}})")
            await sw_b.evaluate(f"costream.join({{server: 'ws://localhost:{PORT}', room: '{args.room}', user: 'B', maxDelay: {args.max_b}}})")
            pa, pb = Panel("A", args.room), Panel("B", args.room, accept=True)
            tasks = [asyncio.create_task(pa.run()), asyncio.create_task(pb.run())]

            phase, t_start, t_phase = "before", time.monotonic(), time.monotonic()
            shots: set = set()
            notes: list[str] = []
            ext_logs = {"A": ExtLog(OUT / "ext_log_A.txt"), "B": ExtLog(OUT / "ext_log_B.txt")}
            while True:
                t = time.monotonic() - t_start
                fa, fb, sa, sb = await asyncio.gather(frame_probe(page_a), frame_probe(page_b),
                                                      ext_state(sw_a), ext_state(sw_b))
                truth = None
                if fa and fb:
                    truth = ((fa["player"] - fa["wall"]) - (fb["player"] - fb["wall"])) * 1000
                    if not math.isfinite(truth):
                        truth = None   # a page without frame timing yet (seen: inf right after load)
                st_a, st_b = sa.get("status") or {}, sb.get("status") or {}
                ext_logs["A"].add(sa.get("log"), t)
                ext_logs["B"].add(sb.get("log"), t)
                ga, gb = pa.group.get("A", {}), pa.group.get("B", {})
                row = {
                    "t": round(t, 1), "phase": phase,
                    "truth_ms": None if truth is None else round(truth, 1),
                    "a_delay": ga.get("current_delay"), "b_delay": gb.get("current_delay"),
                    "a_edge": ga.get("live_edge"), "b_edge": gb.get("live_edge"),
                    "a_head_gap": (st_a.get("m") or {}).get("headGap"), "b_head_gap": (st_b.get("m") or {}).get("headGap"),
                    "a_state": (st_a.get("c") or {}).get("state"), "a_err_ms": ga.get("sync_error_ms"),
                    "b_state": (st_b.get("c") or {}).get("state"), "b_err_ms": gb.get("sync_error_ms"),
                    "a_quality": ga.get("quality"), "b_quality": gb.get("quality"),
                    "a_paused_by": ga.get("paused_by"), "b_paused_by": gb.get("paused_by"),
                }
                rows.append(row)
                print(f"{row['t']:6.1f} {phase:9} truth {fmt(truth, 1):>9} ms | A {fmt(row['a_delay'])} edge {fmt(row['a_edge'], 2)} "
                      f"{row['a_state']} err {row['a_err_ms']} | B {fmt(row['b_delay'])} edge {fmt(row['b_edge'], 2)} "
                      f"{row['b_state']} err {row['b_err_ms']}")

                for page, name, st in ((page_a, "A", st_a), (page_b, "B", st_b)):
                    note = await handle_youtube_error(page, name) or await handle_youtube_pause(page, name, st, shots)
                    if note:
                        print(">>> " + note)
                        notes.append(f"t={row['t']}: {note}")

                in_phase = time.monotonic() - t_phase
                if phase == "before" and in_phase >= args.before and ga.get("current_delay") and gb.get("current_delay"):
                    print(">>> A clicks Match on B")
                    await pa.send({"type": "match_request", "target_user": "B"})
                    phase, t_phase = "adjusting", time.monotonic()
                elif phase == "adjusting" and pa.results and pa.results[-1]["status"] not in ("accepted",):
                    print(">>> match not accepted, stopping")
                    break
                elif phase == "adjusting" and (row["a_state"] == "synced" and row["b_state"] == "synced" and in_phase > 5
                                               or in_phase > 90):
                    print(">>> both extensions report synced; logging the 'after' phase")
                    phase, t_phase = "after", time.monotonic()
                elif phase == "after" and in_phase >= args.after:
                    break
                await asyncio.sleep(1)

            for name, s in (("A", sa), ("B", sb)):
                print(f"extension log {name}:", *(e["text"] for e in s.get("log", [])), sep="\n  ")
            for task in tasks:
                task.cancel()
            await ctx_a.close()
            await ctx_b.close()
    finally:
        server.terminate()
        server_log.close()
        if rows:
            with open(OUT / "demo_log.csv", "w", newline="") as f:
                w = csv.DictWriter(f, fieldnames=list(rows[0]))
                w.writeheader()
                w.writerows(rows)
            write_summary(rows, args, locals().get("notes", []))


def write_summary(rows, args, notes):
    def stats(phase, key="truth_ms"):
        v = [r[key] for r in rows if r["phase"] == phase and r[key] is not None and math.isfinite(r[key])]
        if not v:
            return "no samples"
        a = [abs(x) for x in v]
        return (f"n={len(v)}  mean {statistics.mean(v):+.1f} ms  mean|.| {statistics.mean(a):.1f} ms  "
                f"max|.| {max(a):.1f} ms  stdev {statistics.pstdev(v):.1f} ms")
    lines = [
        f"url {args.url}   B started {args.b_behind} s further behind than A",
        f"before sync (true offset A vs B):  {stats('before')}",
        f"after sync  (true offset A vs B):  {stats('after')}",
        f"after sync  (A's own error):       {stats('after', 'a_err_ms')}",
        f"after sync  (B's own error):       {stats('after', 'b_err_ms')}",
        "positive = A sees the stream earlier than B",
        *(["YouTube pauses handled:"] + ["  " + n for n in notes] if notes else []),
    ]
    (OUT / "demo_summary.txt").write_text("\n".join(lines) + "\n")
    print("\n" + "\n".join(lines))


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--url", required=True)
    ap.add_argument("--room", default="demo")
    ap.add_argument("--b-behind", type=float, default=12)
    ap.add_argument("--max-a", type=float, default=30)
    ap.add_argument("--max-b", type=float, default=30)
    ap.add_argument("--before", type=float, default=15, help="seconds logged before the request")
    ap.add_argument("--after", type=float, default=60, help="seconds logged after sync")
    asyncio.run(main(ap.parse_args()))
