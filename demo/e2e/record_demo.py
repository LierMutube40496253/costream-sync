"""Record the README demo GIF: two windows with the real extension on the fake YouTube page,
B starts 15 s behind A, A clicks Match, B accepts, both end on the same frame. Each window's
caption shows its delay, the measured offset to the other window (from the frame counter)
and the extension's state. Output: docs/demo.gif

  python demo/e2e/record_demo.py
"""
import asyncio
import shutil
import subprocess
import sys
import tempfile
import time
from pathlib import Path

import imageio_ffmpeg
from playwright.async_api import async_playwright

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
import two_window_e2e as e2e  # noqa: E402  (fake page router, panels, frame probe)

ROOT = HERE.parent.parent
GIF = ROOT / "docs" / "demo.gif"
REC = HERE / "out" / "recording"
SIZE = {"width": 640, "height": 420}


async def open_recorded(pw, router, url):
    # Not Playwright's video recording: each window's recording starts at a slightly different
    # moment, and side by side they were 3 frames apart while the measured offset was 9 ms.
    # Instead, both windows are screenshotted at the same instant (see shoot()).
    ctx = await pw.chromium.launch_persistent_context(
        tempfile.mkdtemp(prefix="costream-rec-"), channel="chromium", headless=True, viewport=SIZE,
        args=[f"--disable-extensions-except={e2e.EXT}", f"--load-extension={e2e.EXT}", "--mute-audio",
              "--autoplay-policy=no-user-gesture-required"])
    await ctx.route("https://www.youtube.com/**", router)
    page = ctx.pages[0] if ctx.pages else await ctx.new_page()
    sw = ctx.service_workers[0] if ctx.service_workers else await ctx.wait_for_event("serviceworker")
    await page.goto(url)
    return ctx, page, sw


async def main():
    shutil.rmtree(REC, ignore_errors=True)
    REC.mkdir(parents=True)
    if not e2e.VIDEO_PATH.exists():
        subprocess.run([sys.executable, str(HERE / "make_test_video.py")], check=True)
    router = e2e.make_router((HERE / "fake_youtube.html").read_text(encoding="utf-8"), e2e.VIDEO_PATH.read_bytes())
    server = subprocess.Popen([sys.executable, "-m", "uvicorn", "server.app:app", "--port", str(e2e.PORT), "--log-level", "warning"],
                              cwd=ROOT)
    lines: list = []
    try:
        await asyncio.sleep(2)
        t0 = int(time.time() * 1000)
        base = f"https://www.youtube.com/watch?v=costreamtest&t0={t0}"
        async with async_playwright() as pw:
            (ca, pa, swa), (cb, pb, swb) = await asyncio.gather(
                open_recorded(pw, router, f"{base}&edge=15&sit=15"),
                open_recorded(pw, router, f"{base}&edge=18&sit=30"))
            started = time.monotonic()
            room = f"rec-{t0}"
            await asyncio.sleep(2)
            await swa.evaluate(f"costream.join({{server: 'ws://localhost:{e2e.PORT}', room: '{room}', user: 'A', maxDelay: 30}})")
            await swb.evaluate(f"costream.join({{server: 'ws://localhost:{e2e.PORT}', room: '{room}', user: 'B', maxDelay: 30}})")
            panel_a, panel_b = e2e.Panel("A", room, lines), e2e.Panel("B", room, lines)
            tasks = [asyncio.create_task(panel_a.run()), asyncio.create_task(panel_b.run())]

            shots = {"n": 0, "t0": None, "on": False}

            async def shoot():
                """Both windows at the same instant, about 10 times a second."""
                while True:
                    if shots["on"]:
                        if shots["t0"] is None:
                            shots["t0"] = time.monotonic()
                        a, b = await asyncio.gather(pa.screenshot(type="jpeg", quality=85),
                                                    pb.screenshot(type="jpeg", quality=85))
                        (REC / f"a_{shots['n']:05d}.jpg").write_bytes(a)
                        (REC / f"b_{shots['n']:05d}.jpg").write_bytes(b)
                        shots["n"] += 1
                    await asyncio.sleep(0.05)
            shooter = asyncio.create_task(shoot())

            async def captions(phase, seconds):
                end = time.monotonic() + seconds
                while time.monotonic() < end:
                    fa, fb, sa, sb = await asyncio.gather(e2e.probe(pa), e2e.probe(pb), e2e.ext_state(swa), e2e.ext_state(swb))
                    off = None if not (fa and fb) else (fa["median"] - fb["median"]) * 1000
                    for page, me, other, st, sign in ((pa, "A", "B", sa, 1), (pb, "B", "A", sb, -1)):
                        g = panel_a.group.get(me, {})
                        state = ((st.get("status") or {}).get("c") or {}).get("state") or ""
                        state = {"idle": "", "synced": "synced ✓"}.get(state, state)
                        delay = "" if g.get("current_delay") is None else f"{g['current_delay']:.2f} s behind live · "
                        offset = "" if off is None else f"{other}: {sign * off:+.0f} ms"
                        try:
                            await page.evaluate("([l, t]) => window.__fake.note(l, t)",
                                                [me, f"{delay}{offset}  {phase}{'  ' + state if state else ''}"])
                        except Exception:
                            pass
                    await asyncio.sleep(0.5)

            await captions("", 9)                        # loading, live-head estimate gets ready
            shots["on"] = True
            await captions("", 3)                        # 3 s before the request
            await panel_a.send({"type": "match_request", "target_user": "B"})
            await captions("→ A asked B to match", 2)
            await captions("", 16)
            shots["on"] = False
            shooter.cancel()
            fps = shots["n"] / (time.monotonic() - shots["t0"])
            for t in tasks:
                t.cancel()
            await ca.close()
            await cb.close()
    finally:
        server.terminate()

    if shots["n"] < 20:
        raise SystemExit("recording failed: too few screenshots")
    ff = imageio_ffmpeg.get_ffmpeg_exe()
    rate = f"{fps:.2f}"
    graph = ("[0:v]scale=480:-2[a];[1:v]scale=480:-2[b];[a][b]hstack=inputs=2,split[s0][s1];"
             "[s0]palettegen=max_colors=96[p];[s1][p]paletteuse=dither=bayer:bayer_scale=4")
    GIF.parent.mkdir(exist_ok=True)
    subprocess.run([ff, "-y", "-framerate", rate, "-i", str(REC / "a_%05d.jpg"),
                    "-framerate", rate, "-i", str(REC / "b_%05d.jpg"),
                    "-filter_complex", graph, str(GIF)], check=True,
                   stdout=subprocess.DEVNULL, stderr=subprocess.PIPE)
    print(f"wrote {GIF} ({GIF.stat().st_size / 1e6:.1f} MB, {shots['n']} frame pairs at {fps:.1f} per second)")


if __name__ == "__main__":
    asyncio.run(main())
