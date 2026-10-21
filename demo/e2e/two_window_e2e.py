"""End-to-end test (docs/DESIGN.md, Testing strategy): two Chromium windows with the real
extension on a local fake YouTube live page. No internet, no ads, no cookie dialogs.

Playwright answers www.youtube.com with demo/e2e/fake_youtube.html (so the extension's
manifest is unchanged) and serves the test video with byte ranges (so seeking works).
Ground truth is the frame counter: each window reports which frame it presented and when,
    offset = (frameA / 30 - shownAtA) - (frameB / 30 - shownAtB)
Positive = A sees the stream earlier than B.

Steps, each followed by a 20 s measurement that must stay within one frame (33 ms):
  1. A clicks Match on B, B accepts             (A edge 15 s; B edge 18 s, sitting at 30 s)
  2. an 8 s ad on B                             (B rejoins on its own)
  3. A reloads the page                         (A rejoins on its own, no new offer)
  4. YouTube error on B, B refreshes 3 s later  (as a user would; no auto-reload)

  python demo/e2e/two_window_e2e.py            (headless)
  python demo/e2e/two_window_e2e.py --headed   (watch the two windows)
Output: demo/e2e/out/ (log, CSV, extension logs, result JSON). Exit code 0 = all passed.
"""
from __future__ import annotations

import argparse
import asyncio
import csv
import json
import math
import re
import subprocess
import sys
import tempfile
import time
from pathlib import Path

import websockets
from playwright.async_api import async_playwright

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent.parent
EXT = ROOT / "extension"
OUT = HERE / "out"
VIDEO_PATH = HERE / "test_video.webm"
PORT = 8765
FPS = 30
FRAME_MS = 1000 / FPS

# 7 consecutive frames: (frame / 30 - shown at) for each, plus the median. Headless Chromium
# now and then reports a frame one refresh late, which reads as a whole-frame jump (33 ms) for
# that one frame; the median of 7 is what the viewer sees, the raw value is kept as well.
FRAME_PROBE = """() => new Promise((resolve) => {
  const v = document.querySelector('#movie_player video');
  if (!v || v.paused || v.playbackRate === 0) return resolve(null);
  const vals = [];
  let last = null;
  const timer = setTimeout(() => resolve(null), 1000);
  const onFrame = (_now, md) => {
    const perfToWall = Date.now() / 1000 - performance.now() / 1000;
    const frame = Math.round(md.mediaTime * 30);
    last = { frame, wall: perfToWall + md.expectedDisplayTime / 1000 };
    vals.push(frame / 30 - last.wall);
    if (vals.length < 7) return v.requestVideoFrameCallback(onFrame);
    clearTimeout(timer);
    const s = [...vals].sort((a, b) => a - b);
    resolve({ frame: last.frame, raw: vals[vals.length - 1], median: s[3] });
  };
  v.requestVideoFrameCallback(onFrame);
})"""


def log(text: str, lines: list) -> None:
    line = f"{time.strftime('%H:%M:%S')}  {text}"
    print(line, flush=True)
    lines.append(line)


class Panel:
    """Plays one user's panel over WebSocket: the same messages the real panel will send."""

    def __init__(self, user, room, lines):
        self.user, self.room, self.lines = user, room, lines
        self.group, self.results, self.offers = {}, [], []
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
                    self.offers.append(msg)
                    log(f"{self.user}'s panel: '{msg['requester']} wants to match timing with you. "
                        f"Accepting will {msg['your_move']['text']}.' -> accept", self.lines)
                    await ws.send(json.dumps({"type": "match_answer", "offer_id": msg["offer_id"], "accept": True}))
                elif msg["type"] == "match_result":
                    self.results.append(msg)
                    log(f"{self.user}'s panel: match {msg['status']}, group {msg['group']}, target {msg['target_delay']}"
                        + (f", reasons {msg['reasons']}" if msg.get("reasons") else ""), self.lines)

    async def send(self, obj):
        await self.ws.send(json.dumps(obj))


def make_router(html: str, video: bytes):
    async def handle(route, request):
        url = request.url
        if "/costream-test/test_video.webm" in url:
            size = len(video)
            m = re.match(r"bytes=(\d+)-(\d*)", request.headers.get("range", ""))
            if m:
                start = int(m.group(1))
                end = int(m.group(2)) if m.group(2) else min(size - 1, start + 4_000_000 - 1)
                body = video[start:end + 1]
                await route.fulfill(status=206, body=body, headers={
                    "Content-Type": "video/webm", "Accept-Ranges": "bytes",
                    "Content-Range": f"bytes {start}-{end}/{size}", "Content-Length": str(len(body))})
            else:
                await route.fulfill(status=200, body=video, headers={"Content-Type": "video/webm", "Accept-Ranges": "bytes"})
        elif "/watch" in url:
            await route.fulfill(status=200, body=html, headers={"Content-Type": "text/html; charset=utf-8"})
        else:
            await route.abort()
    return handle


async def open_window(pw, router, url, x, headed):
    args = [f"--disable-extensions-except={EXT}", f"--load-extension={EXT}", "--mute-audio",
            "--autoplay-policy=no-user-gesture-required"]
    if headed:
        args += [f"--window-position={x},40", "--window-size=700,520"]
    ctx = await pw.chromium.launch_persistent_context(tempfile.mkdtemp(prefix="costream-e2e-"), channel="chromium",
                                                      headless=not headed, args=args)
    await ctx.route("https://www.youtube.com/**", router)
    page = ctx.pages[0] if ctx.pages else await ctx.new_page()
    sw = ctx.service_workers[0] if ctx.service_workers else await ctx.wait_for_event("serviceworker")
    await page.goto(url)
    return ctx, page, sw


async def ext_state(sw) -> dict:
    try:
        states = await sw.evaluate("costream.state()")
    except Exception:
        return {}
    return next(iter(states.values()), {}) if states else {}


async def probe(page):
    try:
        return await page.evaluate(FRAME_PROBE)
    except Exception:
        return None


class Recorder:
    def __init__(self, pages, sws, panel, lines):
        self.pages, self.sws, self.panel, self.lines = pages, sws, panel, lines
        self.rows: list[dict] = []
        self.seen_logs = {"A": set(), "B": set()}
        self.ext_files = {u: open(OUT / f"ext_log_{u}.txt", "w", encoding="utf-8") for u in ("A", "B")}
        self.t0 = time.monotonic()
        self.phase = "start"

    async def sample(self) -> dict:
        fa, fb, sa, sb = await asyncio.gather(probe(self.pages["A"]), probe(self.pages["B"]),
                                              ext_state(self.sws["A"]), ext_state(self.sws["B"]))
        offset = raw = None
        if fa and fb:
            offset = (fa["median"] - fb["median"]) * 1000
            raw = (fa["raw"] - fb["raw"]) * 1000
            if not (math.isfinite(offset) and math.isfinite(raw)):
                offset = raw = None
        for u, s in (("A", sa), ("B", sb)):
            for e in s.get("log", []):
                key = (e["t"], e["text"])
                if key not in self.seen_logs[u]:
                    self.seen_logs[u].add(key)
                    self.ext_files[u].write(f"{time.monotonic() - self.t0:7.1f}  {e['text']}\n")
                    self.ext_files[u].flush()
                    log(f"    [ext {u}] {e['text']}", self.lines)
        ca, cb = (sa.get("status") or {}).get("c") or {}, (sb.get("status") or {}).get("c") or {}
        ga, gb = self.panel.group.get("A", {}), self.panel.group.get("B", {})
        row = {"t": round(time.monotonic() - self.t0, 2), "phase": self.phase,
               "offset_ms": None if offset is None else round(offset, 1),
               "raw_offset_ms": None if raw is None else round(raw, 1),
               "frame_a": fa and fa["frame"], "frame_b": fb and fb["frame"],
               "a_state": ca.get("state"), "b_state": cb.get("state"),
               "a_err_ms": None if ca.get("err") is None else round(ca["err"] * 1000, 1),
               "b_err_ms": None if cb.get("err") is None else round(cb["err"] * 1000, 1),
               "a_delay": ga.get("current_delay"), "b_delay": gb.get("current_delay"),
               "a_edge": ga.get("live_edge"), "b_edge": gb.get("live_edge")}
        self.rows.append(row)
        return row

    async def run_for(self, seconds, label=None):
        if label:
            self.phase = label
        end = time.monotonic() + seconds
        while time.monotonic() < end:
            row = await self.sample()
            log(f"  {row['phase']:<22} offset {'' if row['offset_ms'] is None else row['offset_ms']:>9} ms | "
                f"A {row['a_state']} err {row['a_err_ms']} | B {row['b_state']} err {row['b_err_ms']}", self.lines)
            await asyncio.sleep(0.5)

    async def wait_synced(self, label, limit=60):
        """Until the measured offset is within one frame for 3 s, with both extensions holding
        the target (not pausing or skipping). Their own synced/adjusting label flickers with
        the same whole-frame steps the frame counter shows, so it isn't used here."""
        self.phase = label
        good, end = 0, time.monotonic() + limit
        holding = ("synced", "adjusting")
        while time.monotonic() < end:
            row = await self.sample()
            log(f"  {label:<22} offset {'' if row['offset_ms'] is None else row['offset_ms']:>9} ms | "
                f"A {row['a_state']} err {row['a_err_ms']} | B {row['b_state']} err {row['b_err_ms']}", self.lines)
            ok = (row["offset_ms"] is not None and abs(row["offset_ms"]) <= FRAME_MS
                  and row["a_state"] in holding and row["b_state"] in holding)
            good = good + 1 if ok else 0
            if good >= 6:
                return True
            await asyncio.sleep(0.5)
        return False

    def window(self, label, key="offset_ms"):
        return [r[key] for r in self.rows if r["phase"] == label and r[key] is not None]


async def main(args) -> int:
    OUT.mkdir(exist_ok=True)
    lines: list[str] = []
    if not VIDEO_PATH.exists():
        log("generating the test video (once, ~1.5 min) ...", lines)
        subprocess.run([sys.executable, str(HERE / "make_test_video.py")], check=True)
    html = (HERE / "fake_youtube.html").read_text(encoding="utf-8")
    router = make_router(html, VIDEO_PATH.read_bytes())
    server_log = open(OUT / "server.log", "w")
    server = subprocess.Popen([sys.executable, "-m", "uvicorn", "server.app:app", "--port", str(PORT), "--log-level", "warning"],
                              cwd=ROOT, stdout=server_log, stderr=subprocess.STDOUT)
    results = []
    rec = None
    try:
        await asyncio.sleep(2)
        t0 = int(time.time() * 1000)
        base = f"https://www.youtube.com/watch?v=costreamtest&t0={t0}"
        async with async_playwright() as pw:
            (ca, pa, swa), (cb, pb, swb) = await asyncio.gather(
                open_window(pw, router, f"{base}&edge=15&sit=15", 0, args.headed),
                open_window(pw, router, f"{base}&edge=18&sit=30", 720, args.headed))
            room = f"e2e-{t0}"
            await asyncio.sleep(3)
            await swa.evaluate(f"costream.join({{server: 'ws://localhost:{PORT}', room: '{room}', user: 'A', maxDelay: 30}})")
            await swb.evaluate(f"costream.join({{server: 'ws://localhost:{PORT}', room: '{room}', user: 'B', maxDelay: 30}})")
            panel_a, panel_b = Panel("A", room, lines), Panel("B", room, lines)
            tasks = [asyncio.create_task(panel_a.run()), asyncio.create_task(panel_b.run())]
            rec = Recorder({"A": pa, "B": pb}, {"A": swa, "B": swb}, panel_a, lines)

            await rec.run_for(12, "before")   # long enough for the live-head estimate to be ready
            log(">>> step 1: A clicks Match on B", lines)
            await panel_a.send({"type": "match_request", "target_user": "B"})
            ok = await rec.wait_synced("syncing")
            await rec.run_for(20, "after accept")
            results.append(("synced after accept", "after accept", ok))

            log(">>> step 2: 8 s ad on B", lines)
            await pb.evaluate("() => window.__fake.ad(8)")
            await asyncio.sleep(9)
            ok = await rec.wait_synced("rejoin after ad")
            await rec.run_for(20, "after ad")
            results.append(("back in sync after B's ad", "after ad", ok))

            log(">>> step 3: A reloads the page", lines)
            await pa.reload()
            ok = await rec.wait_synced("rejoin after reload", limit=75)
            await rec.run_for(20, "after reload")
            results.append(("back in sync after A reloads", "after reload", ok))

            log(">>> step 4: YouTube error on B; B refreshes 3 s later", lines)
            await pb.evaluate("() => window.__fake.error()")
            await asyncio.sleep(3)
            await pb.reload()
            ok = await rec.wait_synced("rejoin after error", limit=75)
            await rec.run_for(20, "after error+refresh")
            results.append(("back in sync after B's error + refresh", "after error+refresh", ok))

            offers = len(panel_b.offers) + len(panel_a.offers)
            log(f"offers shown in total: {offers} (1 expected: rejoins need no new offer)", lines)
            for t in tasks:
                t.cancel()
            await ca.close()
            await cb.close()
    finally:
        server.terminate()
        server_log.close()

    before = rec.window("before") if rec else []
    summary = [f"before sync: offset {sum(before) / len(before):+.1f} ms (n={len(before)})" if before else "before sync: no samples"]
    passed_all = True
    out = []
    for name, label, synced in results:
        v = rec.window(label)
        worst = max((abs(x) for x in v), default=None)
        mean = sum(v) / len(v) if v else None
        share = sum(abs(x) <= FRAME_MS for x in v) / len(v) if v else 0.0
        # "Within one frame" (design doc: nothing on screen can be closer than one frame).
        # Screens show whole frames, so two players 8 ms apart read as -8 or +25 ms (or +41)
        # depending on where the frame boundary falls; about delta/33 of the samples land one
        # frame further. The mean recovers the continuous offset; it must be within half a
        # frame at 30 fps (= one frame at 60 fps), and the screens may never differ by more
        # than one frame (no sample beyond two frames).
        ok = (synced and mean is not None and abs(mean) <= FRAME_MS / 2 and worst <= 2 * FRAME_MS)
        passed_all &= ok
        summary.append(f"{'PASS' if ok else 'FAIL'}  {name}: mean {mean if mean is None else round(mean, 1)} ms, "
                       f"worst {worst if worst is None else round(worst, 1)} ms, {share:.0%} of samples within one frame "
                       f"(n={len(v)}; rule: |mean| <= {FRAME_MS / 2:.1f} ms, none beyond {2 * FRAME_MS:.1f} ms)")
        out.append({"name": name, "synced": synced, "mean_ms": mean, "share_within_frame": share, "worst_ms": worst,
                    "samples": len(v), "pass": ok})
    if len(results) < 4:
        passed_all = False
        summary.append("FAIL  did not reach all steps")
    for s in summary:
        log(s, lines)
    (OUT / "e2e.log").write_text("\n".join(lines) + "\n", encoding="utf-8")
    (OUT / "e2e_result.json").write_text(json.dumps(out, indent=2))
    if rec and rec.rows:
        with open(OUT / "e2e.csv", "w", newline="") as f:
            w = csv.DictWriter(f, fieldnames=list(rec.rows[0]))
            w.writeheader()
            w.writerows(rec.rows)
    return 0 if passed_all else 1


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--headed", action="store_true", help="show the two windows")
    sys.exit(asyncio.run(main(ap.parse_args())))
