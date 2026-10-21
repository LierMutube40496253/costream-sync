"""Two short checks on real YouTube (design doc, Robustness).

  ads      Two windows, extension NOT loaded, no control: only rejects cookies so playback
           can start, then watches for 5 minutes. Does an ad in one window come with
           "Something went wrong" in the other?
  actions  One window, extension loaded (idle). Does the same <video> operations the
           extension uses (content.js), one at a time with 20 s between them, and logs which
           one (if any) makes YouTube show "Something went wrong". Before each skip it logs
           how far ahead the player has downloaded.

  python tools/youtube_checks.py ads     "https://www.youtube.com/watch?v=<live id>"
  python tools/youtube_checks.py actions "https://www.youtube.com/watch?v=<live id>"
Output: tools/logs/youtube_check_<mode>.txt
"""
import asyncio
import sys
import tempfile
import time
from pathlib import Path

from playwright.async_api import async_playwright

ROOT = Path(__file__).resolve().parent.parent
EXT = ROOT / "extension"
LOGS = ROOT / "tools" / "logs"

STATE = """() => {
  const p = document.getElementById('movie_player');
  const v = p && p.querySelector('video');
  if (!p || !v) return {player: false};
  const e = document.querySelector('.ytp-error');
  const err = e && e.offsetParent !== null ? (e.innerText || 'error').split('\\n')[0] : null;
  const b = v.buffered.length ? v.buffered.end(v.buffered.length - 1) - v.currentTime : null;
  let pr = {};
  try { pr = p.getProgressState(); } catch (x) {}
  return {player: true, ad: p.classList.contains('ad-showing'), error: err, paused: v.paused,
          ct: +v.currentTime.toFixed(2), rate: v.playbackRate,
          bufferedAhead: b == null ? null : +b.toFixed(2),
          seekableAhead: pr.seekableEnd != null ? +(pr.seekableEnd - p.getCurrentTime()).toFixed(2) : null};
}"""


class Log:
    def __init__(self, mode):
        LOGS.mkdir(parents=True, exist_ok=True)
        self.path = LOGS / f"youtube_check_{mode}.txt"
        self.f = open(self.path, "w", encoding="utf-8")
        self.t0 = time.monotonic()

    def __call__(self, text):
        line = f"{time.monotonic() - self.t0:7.1f}  {text}"
        print(line, flush=True)
        self.f.write(line + "\n")
        self.f.flush()


async def open_window(pw, x, with_extension, url):
    args = ["--mute-audio", "--autoplay-policy=no-user-gesture-required",
            f"--window-position={x},40", "--window-size=960,760"]
    if with_extension:
        args += [f"--disable-extensions-except={EXT}", f"--load-extension={EXT}"]
    ctx = await pw.chromium.launch_persistent_context(tempfile.mkdtemp(), headless=False, args=args)
    page = ctx.pages[0]
    await page.goto(url, wait_until="domcontentloaded")
    return ctx, page


async def reject_cookies(page):
    btn = page.locator("button", has_text="Reject all")
    try:
        if await btn.count() and await btn.first.is_visible():
            await btn.first.scroll_into_view_if_needed()
            await btn.first.click()
            return True
    except Exception:
        pass
    return False


async def state(page):
    try:
        return await page.evaluate(STATE)
    except Exception:
        return {"player": False, "navigating": True}


async def check_ads(url, seconds=300):
    log = Log("ads")
    log(f"check 1: two windows, extension NOT loaded, no control, {seconds} s, {url}")
    async with async_playwright() as pw:
        (ca, pa), (cb, pb) = await asyncio.gather(open_window(pw, 0, False, url), open_window(pw, 960, False, url))
        pages = {"A": pa, "B": pb}
        prev = {"A": {}, "B": {}}
        events = []
        t0 = time.monotonic()
        while time.monotonic() - t0 < seconds:
            for name, page in pages.items():
                if await reject_cookies(page):
                    log(f"{name}: rejected cookies (the only interaction)")
                s = await state(page)
                p = prev[name]
                for key, on, off in (("ad", "ad starts", "ad ends"), ("error", "ERROR shown", "error gone"),
                                     ("paused", "paused", "playing")):
                    if bool(s.get(key)) != bool(p.get(key)) and s.get("player"):
                        text = on if s.get(key) else off
                        if key == "error" and s.get(key):
                            text += f": '{s['error']}'"
                            await page.screenshot(path=str(LOGS / f"check_ads_error_{name}_{int(time.monotonic() - t0)}.png"))
                        log(f"{name}: {text}  (ct {s.get('ct')})")
                        events.append((time.monotonic() - t0, name, key, bool(s.get(key))))
                prev[name] = s
            await asyncio.sleep(1)
        # Did an error in one window follow an ad start in the other (within 10 s)?
        ads = [(t, n) for t, n, k, on in events if k == "ad" and on]
        errors = [(t, n) for t, n, k, on in events if k == "error" and on]
        log(f"summary: {len(ads)} ad starts, {len(errors)} errors")
        for te, ne in errors:
            cause = [f"{n} ad at {t:.0f} s" for t, n in ads if n != ne and 0 <= te - t <= 10]
            own = [f"own ad at {t:.0f} s" for t, n in ads if n == ne and 0 <= te - t <= 10]
            log(f"  error in {ne} at {te:.0f} s; within 10 s before: {', '.join(cause + own) or 'no ad in either window'}")
        await ca.close()
        await cb.close()


async def check_actions(url):
    log = Log("actions")
    log(f"check 2: one window, extension loaded (idle), same <video> operations as content.js, {url}")
    async with async_playwright() as pw:
        ctx, page = await open_window(pw, 0, True, url)

        async def wait_playing(limit=180):
            t0 = time.monotonic()
            while time.monotonic() - t0 < limit:
                await reject_cookies(page)
                s = await state(page)
                if s.get("player") and not s.get("ad") and not s.get("error"):
                    if s.get("paused"):
                        await page.evaluate("() => document.querySelector('#movie_player video').play()")
                    else:
                        return s
                await asyncio.sleep(1)
            return await state(page)

        async def watch(label, seconds):
            for _ in range(seconds):
                s = await state(page)
                if s.get("error"):
                    log(f"  -> ERROR after '{label}': '{s['error']}'  {s}")
                    await page.screenshot(path=str(LOGS / f"check_actions_error_{label.replace(' ', '_')}.png"))
                    return True
                if s.get("ad"):
                    log(f"  (ad playing during '{label}')")
                await asyncio.sleep(1)
            log(f"  ok after '{label}': {await state(page)}")
            return False

        actions = [
            ("pause 5 s", "async () => { const v = document.querySelector('#movie_player video'); v.pause(); await new Promise(r => setTimeout(r, 5000)); v.play(); }"),
            ("rewind 5 s", "() => { document.querySelector('#movie_player video').currentTime -= 5; }"),
            ("skip forward 5 s", "() => { document.querySelector('#movie_player video').currentTime += 5; }"),
            ("skip forward 18 s", "() => { document.querySelector('#movie_player video').currentTime += 18; }"),
            ("speed 0.98 for 10 s", "async () => { const v = document.querySelector('#movie_player video'); v.playbackRate = 0.98; await new Promise(r => setTimeout(r, 10000)); v.playbackRate = 1; }"),
        ]
        log(f"start: {await wait_playing()}")
        # Start ~25 s behind live so the forward skips have somewhere to go.
        await page.evaluate("() => { document.querySelector('#movie_player video').currentTime -= 25; }")
        await asyncio.sleep(5)
        log(f"moved 25 s back from where it started: {await state(page)}")
        for label, js in actions:
            s = await state(page)
            log(f"ACTION {label}: before {s}")
            try:
                await page.evaluate(js)
            except Exception as exc:
                log(f"  action raised {exc}")
            if await watch(label, 20):
                await page.reload(wait_until="domcontentloaded")   # single window: no partner to loop with
                log(f"  reloaded once; {await wait_playing()}")
                await page.evaluate("() => { document.querySelector('#movie_player video').currentTime -= 25; }")
                await asyncio.sleep(5)
        await ctx.close()


if __name__ == "__main__":
    mode, url = sys.argv[1], sys.argv[2]
    asyncio.run(check_ads(url) if mode == "ads" else check_actions(url))
