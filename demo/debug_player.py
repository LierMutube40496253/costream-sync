"""Open one Chromium window with the extension and print the YouTube player's state each second."""
import asyncio
import sys
import tempfile
from pathlib import Path

from playwright.async_api import async_playwright

EXT = Path(__file__).resolve().parent.parent / "extension"
STATE = """() => {
  const p = document.getElementById('movie_player'); const v = p && p.querySelector('video');
  if (!p || !v) return {noPlayer: true, url: location.href};
  const b = v.buffered.length ? [v.buffered.start(0), v.buffered.end(v.buffered.length - 1)] : null;
  let mrt = null; try { mrt = p.getMediaReferenceTime(); } catch (e) {}
  return {ad: p.classList.contains('ad-showing'), paused: v.paused, rs: v.readyState, ct: +v.currentTime.toFixed(3),
          cur: +p.getCurrentTime().toFixed(3), mrt, buf: b, err: v.error && v.error.code, st: p.getPlayerState(),
          err2: (document.querySelector('.ytp-error') || {}).innerText, hidden: document.hidden,
          vis: document.visibilityState};
}"""


async def main(url, seconds):
    async with async_playwright() as pw:
        ctx = await pw.chromium.launch_persistent_context(
            tempfile.mkdtemp(), headless=False,
            args=[f"--disable-extensions-except={EXT}", f"--load-extension={EXT}", "--mute-audio",
                  "--autoplay-policy=no-user-gesture-required"])
        page = ctx.pages[0]
        await page.goto(url)
        if "consent." in page.url:
            await page.get_by_role("button", name="Reject all").first.click()
            await page.wait_for_url("**/www.youtube.com/**")
        for i in range(seconds):
            print(i, await page.evaluate(STATE), flush=True)
            await asyncio.sleep(1)
        await page.screenshot(path=str(Path(__file__).parent / "debug_player.png"))
        await ctx.close()


asyncio.run(main(sys.argv[1], int(sys.argv[2]) if len(sys.argv) > 2 else 40))
