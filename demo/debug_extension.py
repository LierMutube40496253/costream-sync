"""Open one window with the extension, join a local server, and dump what the extension sees:
service-worker state, content-script errors and the page's console."""
import asyncio
import json
import subprocess
import sys
import tempfile
from pathlib import Path

from playwright.async_api import async_playwright

ROOT = Path(__file__).resolve().parent.parent
EXT = ROOT / "extension"


async def main(url, seconds):
    server = subprocess.Popen([sys.executable, "-m", "uvicorn", "server.app:app", "--port", "8765", "--log-level", "warning"], cwd=ROOT)
    try:
        await asyncio.sleep(2)
        async with async_playwright() as pw:
            ctx = await pw.chromium.launch_persistent_context(
                tempfile.mkdtemp(), headless=False,
                args=[f"--disable-extensions-except={EXT}", f"--load-extension={EXT}", "--mute-audio",
                      "--autoplay-policy=no-user-gesture-required"])
            page = ctx.pages[0]
            page.on("console", lambda m: print("page console:", m.type, m.text[:200]) if "costream" in m.text.lower() or m.type == "error" else None)
            sw = ctx.service_workers[0] if ctx.service_workers else await ctx.wait_for_event("serviceworker")
            sw.on("console", lambda m: print("sw console:", m.type, m.text[:300]))
            await page.goto(url)
            for _ in range(20):
                btn = page.locator("button", has_text="Reject all")
                if await btn.count() and await btn.first.is_visible():
                    await btn.first.scroll_into_view_if_needed()
                    await btn.first.click()
                    print("rejected cookies")
                    break
                await asyncio.sleep(1)
            print("join ->", await sw.evaluate("costream.join({room: 'dbg', user: 'A', maxDelay: 30})"))
            for i in range(seconds):
                await asyncio.sleep(1)
                if i % 5 == 4:
                    st = await sw.evaluate("costream.state()")
                    print(json.dumps(st, default=str)[:1500])
            await ctx.close()
    finally:
        server.terminate()


asyncio.run(main(sys.argv[1], int(sys.argv[2]) if len(sys.argv) > 2 else 20))
