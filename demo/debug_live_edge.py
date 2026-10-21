"""One window: what live edge does the extension report at YouTube's live head, and after a
12 s rewind? The live edge should stay put when you rewind; only the current delay grows.
Also prints YouTube's raw numbers behind it (seekableEnd, getCurrentTime, buffered end)."""
import asyncio
import json
import subprocess
import sys
import tempfile
from pathlib import Path

import websockets
from playwright.async_api import async_playwright

ROOT = Path(__file__).resolve().parent.parent
EXT = ROOT / "extension"
RAW = """() => {
  const p = document.getElementById('movie_player'), v = p.querySelector('video');
  const pr = p.getProgressState();
  const be = v.buffered.length ? v.buffered.end(v.buffered.length - 1) : null;
  return {cur: +p.getCurrentTime().toFixed(3), seekableEnd: +pr.seekableEnd.toFixed(3),
          gap: +(pr.seekableEnd - p.getCurrentTime()).toFixed(3), atHead: pr.isAtLiveHead,
          bufferedAhead: be == null ? null : +(be - v.currentTime).toFixed(3),
          delayNow: +(Date.now() / 1000 - p.getMediaReferenceTime()).toFixed(3)};
}"""


async def main(url):
    server = subprocess.Popen([sys.executable, "-m", "uvicorn", "server.app:app", "--port", "8765", "--log-level", "warning"], cwd=ROOT)
    group = {}
    try:
        await asyncio.sleep(2)
        async with async_playwright() as pw:
            ctx = await pw.chromium.launch_persistent_context(
                tempfile.mkdtemp(), headless=False,
                args=[f"--disable-extensions-except={EXT}", f"--load-extension={EXT}", "--mute-audio",
                      "--autoplay-policy=no-user-gesture-required"])
            page = ctx.pages[0]
            sw = ctx.service_workers[0] if ctx.service_workers else await ctx.wait_for_event("serviceworker")
            await page.goto(url)
            for _ in range(30):   # cookie dialog, then wait for playback
                btn = page.locator("button", has_text="Reject all")
                if await btn.count() and await btn.first.is_visible():
                    await btn.first.scroll_into_view_if_needed()
                    await btn.first.click()
                ok = await page.evaluate("() => { const p = document.getElementById('movie_player'); const v = p && p.querySelector('video'); if (v && v.paused) v.play(); return !!p && !!v && !v.paused && !p.classList.contains('ad-showing') && v.currentTime > 0; }")
                if ok:
                    break
                await asyncio.sleep(1)
            await sw.evaluate("costream.join({room: 'edge', user: 'B', maxDelay: 30})")

            async def panel():
                async with websockets.connect("ws://localhost:8765/ws") as ws:
                    await ws.send(json.dumps({"type": "hello", "role": "panel", "user": "obs", "room": "edge"}))
                    async for raw in ws:
                        msg = json.loads(raw)
                        if msg["type"] == "group":
                            group.update({m["user"]: m for m in msg["members"]})
            task = asyncio.create_task(panel())

            async def show(label, n):
                for _ in range(n):
                    await asyncio.sleep(1)
                    raw = await page.evaluate(RAW)
                    g = group.get("B", {})
                    print(f"{label:14} current {g.get('current_delay')}  live_edge {g.get('live_edge')}  | youtube {raw}")

            await page.evaluate("() => document.getElementById('movie_player').seekToLiveHead()")
            await asyncio.sleep(3)
            await show("at live head", 8)
            await page.evaluate("() => { document.querySelector('#movie_player video').currentTime -= 12; }")
            await show("rewound 12 s", 12)
            task.cancel()
            await ctx.close()
    finally:
        server.terminate()


asyncio.run(main(sys.argv[1]))
