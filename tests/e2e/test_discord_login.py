"""Discord login in real (headless) browsers, against a fake Discord (tests/e2e/fake_discord.py)
and the real server in discord mode, with a scripted bot putting Lier in a voice channel.

1. Panel: "Log in with Discord" -> back with a token -> shows "Lier · LCK casting".
2. The real extension: costream.login() goes through chrome.identity's login window, then the
   fake YouTube tab joins on its own; the PANEL shows Lier's extension connected with a delay
   (panel and extension paired by the Discord account).
"""
import json
import os
import subprocess
import sys
import tempfile
import threading
import time
from pathlib import Path

import pytest
from playwright.sync_api import expect, sync_playwright
from websockets.sync.client import connect

ROOT = Path(__file__).resolve().parents[2]
EXT = ROOT / "extension"
PORT, DISCORD_PORT = 8771, 8769
BOT_KEY = "test-bot-key"


def wait_port(url):
    for _ in range(60):
        try:
            with connect(url, open_timeout=0.5):
                return
        except OSError:
            time.sleep(0.2)


@pytest.fixture
def servers():
    discord = subprocess.Popen([sys.executable, "-m", "uvicorn", "tests.e2e.fake_discord:app", "--port", str(DISCORD_PORT),
                                "--log-level", "warning"], cwd=ROOT)
    env = {**os.environ, "COSTREAM_AUTH": "discord", "DISCORD_CLIENT_ID": "test-client-id",
           "DISCORD_API_BASE": f"http://localhost:{DISCORD_PORT}/api",
           "DISCORD_AUTHORIZE_URL": f"http://localhost:{DISCORD_PORT}/oauth2/authorize", "BOT_SHARED_SECRET": BOT_KEY}
    server = subprocess.Popen([sys.executable, "-m", "uvicorn", "server.app:app", "--port", str(PORT), "--log-level", "warning"],
                              cwd=ROOT, env=env)
    wait_port(f"ws://localhost:{PORT}/ws")
    # Scripted bot: Lier and Sam are in the voice channel "LCK casting".
    stop = threading.Event()

    def bot():
        with connect(f"ws://localhost:{PORT}/ws") as ws:
            ws.recv()
            ws.send(json.dumps({"type": "hello", "role": "bot", "secret": BOT_KEY}))
            assert json.loads(ws.recv())["type"] == "hello_ok"
            ws.send(json.dumps({"type": "voice_members", "channel_id": "111", "channel_name": "LCK casting",
                                "members": {"1001": "Lier", "1002": "Sam"}}))
            stop.wait()
    t = threading.Thread(target=bot, daemon=True)
    t.start()
    time.sleep(0.5)
    yield
    stop.set()
    server.terminate()
    discord.terminate()


@pytest.mark.e2e
def test_panel_logs_in_with_discord(servers):
    with sync_playwright() as pw:
        browser = pw.chromium.launch()
        page = browser.new_page()
        page.goto(f"http://localhost:{PORT}/panel/")
        login = page.locator("#discord-login")
        expect(login).to_be_visible(timeout=5000)
        login.click()   # -> fake Discord -> back to /panel/#access_token=...&state=...
        expect(page.locator("#who")).to_have_text("Lier · LCK casting", timeout=5000)
        assert "access_token" not in page.url   # token removed from the address bar
        expect(page.locator(".member .name")).to_have_text("Sam")
        browser.close()


@pytest.mark.e2e
def test_extension_logs_in_and_pairs_with_the_panel(servers):
    html = "<html><body><div id='movie_player'><video></video></div></body></html>"
    with sync_playwright() as pw:
        ctx = pw.chromium.launch_persistent_context(
            tempfile.mkdtemp(prefix="costream-login-"), channel="chromium", headless=True,
            args=[f"--disable-extensions-except={EXT}", f"--load-extension={EXT}"])
        ctx.route("https://www.youtube.com/**", lambda r: r.fulfill(body=html, content_type="text/html"))
        sw = ctx.service_workers[0] if ctx.service_workers else ctx.wait_for_event("serviceworker")
        sw.evaluate(f"costream.setServer('ws://localhost:{PORT}')")
        assert sw.evaluate("costream.login()") == "Lier"   # chrome.identity -> fake Discord -> whoami

        # The popup shows the name and the connection.
        ext_id = sw.url.split("/")[2]
        popup = ctx.new_page()
        popup.goto(f"chrome-extension://{ext_id}/popup.html")
        expect(popup.locator("#name")).to_have_text("Lier")

        yt = ctx.pages[0]
        yt.goto("https://www.youtube.com/watch?v=x")   # extension joins this tab by itself
        expect(popup.locator("#conn")).to_contain_text("connected", timeout=10000)

        panel = ctx.new_page()
        panel.goto(f"http://localhost:{PORT}/panel/")
        panel.locator("#discord-login").click()
        expect(panel.locator("#who")).to_have_text("Lier · LCK casting", timeout=5000)
        # Paired: the panel's own card is Lier, and the server reports Lier's extension connected.
        state = None
        for _ in range(50):
            state = panel.evaluate("window.__panel.state()")
            mine = [m for m in state["members"] if m["user"] == "1001"]
            if mine and mine[0]["extension"] and mine[0]["panel"]:
                break
            time.sleep(0.2)
        assert mine and mine[0]["extension"] and mine[0]["panel"], state
        assert len([m for m in state["members"] if m["name"] == "Lier"]) == 1   # one person, both connections

        popup.locator("#logout").click()
        expect(popup.locator("#login")).to_be_visible()
        ctx.close()
