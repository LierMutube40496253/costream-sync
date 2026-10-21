"""Reproduce "accepted target not applied" with ONE real window.

A = real extension in a Chromium window, sitting at YouTube's live head.
B = scripted in Python: its "extension" reports a delay 4 s behind A, its panel accepts.
A's panel (also Python) clicks Match on B. Prints A's extension log and measurement.
Expected if everything works: A's extension logs "holding target" and pauses ~4 s.
"""
import asyncio
import json
import subprocess
import sys
import tempfile
import time
from pathlib import Path

import websockets
from playwright.async_api import async_playwright

ROOT = Path(__file__).resolve().parent.parent
EXT = ROOT / "extension"
URL = "ws://localhost:8765/ws"


async def fake_b(state: dict):
    """B's extension (status every 250 ms, 4 s behind A) and B's panel (accepts offers)."""
    async with websockets.connect(URL) as ext, websockets.connect(URL) as panel:
        await ext.send(json.dumps({"type": "hello", "role": "extension", "user": "B", "room": "repro"}))
        await panel.send(json.dumps({"type": "hello", "role": "panel", "user": "B", "room": "repro"}))

        async def answer():
            async for raw in panel:
                msg = json.loads(raw)
                if msg["type"] == "match_offer":
                    print(f"B's panel got: '{msg['your_move']['text']}' (A: {msg['moves']['A']['text']}) -> accept")
                    await panel.send(json.dumps({"type": "match_answer", "offer_id": msg["offer_id"], "accept": True}))
        asyncio.create_task(answer())
        while True:
            a = state.get("A")
            if a:
                cur = a["current"] + 4.0
                at = time.time()
                await ext.send(json.dumps({
                    "type": "status", "video_id": a["video"], "current_delay": cur, "live_edge": cur,
                    "max_delay": 30, "at": at, "player_time": at - cur - a["K"], "in_ad": False, "quality": "frame"}))
            await asyncio.sleep(0.25)


async def main(url):
    server = subprocess.Popen([sys.executable, "-m", "uvicorn", "server.app:app", "--port", "8765", "--log-level", "warning"], cwd=ROOT)
    state: dict = {}
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
            for _ in range(60):   # cookie dialog, ads, then playback
                btn = page.locator("button", has_text="Reject all")
                if await btn.count() and await btn.first.is_visible():
                    await btn.first.scroll_into_view_if_needed()
                    await btn.first.click()
                ok = await page.evaluate("() => { const p = document.getElementById('movie_player'); const v = p && p.querySelector('video'); if (v && v.paused) v.play(); return !!p && !!v && !v.paused && !p.classList.contains('ad-showing') && v.currentTime > 0; }")
                if ok:
                    break
                await asyncio.sleep(1)
            await page.evaluate("() => document.getElementById('movie_player').seekToLiveHead()")
            await asyncio.sleep(3)
            await sw.evaluate("costream.join({room: 'repro', user: 'A', maxDelay: 30})")
            asyncio.create_task(fake_b(state))

            async with websockets.connect(URL) as pa:
                await pa.send(json.dumps({"type": "hello", "role": "panel", "user": "A", "room": "repro"}))

                async def a_panel():
                    async for raw in pa:
                        msg = json.loads(raw)
                        if msg["type"] == "match_result":
                            print(f"A's panel: match {msg['status']}, target {msg['target_delay']}, reasons {msg.get('reasons')}")
                asyncio.create_task(a_panel())

                seen = set()
                for i in range(30):
                    st = next(iter((await sw.evaluate("costream.state()")).values()), {})
                    m = (st.get("status") or {}).get("m") or {}
                    c = (st.get("status") or {}).get("c") or {}
                    if m.get("valid") and m.get("K"):
                        state["A"] = {"current": m["at"] - m["playerTime"] - m["K"], "K": m["K"], "video": m["videoId"]}
                    for e in st.get("log", []):
                        if (e["t"], e["text"]) not in seen:
                            seen.add((e["t"], e["text"]))
                            print(f"  [A ext] {e['text']}")
                    if i % 2 == 0 and "A" in state:
                        print(f"t={i:2d}s  A delay {state['A']['current']:.3f}  headGap {m.get('headGap')}  state {c.get('state')}  err {c.get('err')}")
                    if i == 6:
                        print(">>> A clicks Match on B")
                        await pa.send(json.dumps({"type": "match_request", "target_user": "B"}))
                    await asyncio.sleep(1)
            await ctx.close()
    finally:
        server.terminate()


asyncio.run(main(sys.argv[1]))
