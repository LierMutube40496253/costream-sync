"""Panel in a real (headless) browser against the real server. Two scripted "extensions" report
the design doc's rule-3 example: A at its live edge 20 s, B with live edge 25 s sitting at 41 s.
A clicks Match in A's panel, B's panel shows the offer worded from B's position, B accepts,
both panels show the result and both extensions get set_target. About 15 s.
"""
import json
import subprocess
import sys
import threading
import time
from pathlib import Path

import pytest
from playwright.sync_api import expect, sync_playwright
from websockets.sync.client import connect

ROOT = Path(__file__).resolve().parents[2]
PORT = 8767
K = 1792504509.0


class FakeExtension(threading.Thread):
    """Sends status every 250 ms like the extension, records what the server sends back."""

    def __init__(self, user, room, edge, current):
        super().__init__(daemon=True)
        self.user, self.room, self.edge, self.current = user, room, edge, current
        self.received, self.stop = [], threading.Event()

    def run(self):
        with connect(f"ws://localhost:{PORT}/ws") as ws:
            ws.send(json.dumps({"type": "hello", "role": "extension", "user": self.user, "room": self.room}))
            while not self.stop.is_set():
                at = time.time()
                ws.send(json.dumps({"type": "status", "video_id": "v1", "current_delay": self.current,
                                    "live_edge": self.edge, "max_delay": 30, "at": at,
                                    "player_time": at - self.current - K, "in_ad": False, "quality": "frame"}))
                try:
                    while True:
                        self.received.append(json.loads(ws.recv(timeout=0.25)))
                except TimeoutError:
                    pass


@pytest.fixture
def server():
    proc = subprocess.Popen([sys.executable, "-m", "uvicorn", "server.app:app", "--port", str(PORT), "--log-level", "warning"],
                            cwd=ROOT)
    for _ in range(50):
        try:
            with connect(f"ws://localhost:{PORT}/ws", open_timeout=0.5):
                break
        except OSError:
            time.sleep(0.2)
    yield
    proc.terminate()
    proc.wait(10)   # on Linux uvicorn shuts down gracefully; the next test needs the port free


@pytest.mark.e2e
def test_match_accept_through_the_panel(server):
    room = f"panel-{int(time.time())}"
    a, b = FakeExtension("A", room, 20, 20), FakeExtension("B", room, 25, 41)
    a.start(); b.start()
    try:
        with sync_playwright() as pw:
            browser = pw.chromium.launch()
            pa = browser.new_page()
            pb = browser.new_page()
            pa.goto(f"http://localhost:{PORT}/panel/?user=A&room={room}")
            pb.goto(f"http://localhost:{PORT}/panel/?user=B&room={room}")

            match = pa.locator('button[data-user="B"]')
            expect(match).to_be_enabled(timeout=10000)
            expect(pa.locator("#me .big")).to_have_text("20.0 s")
            match.click()

            expect(pb.locator("#offer")).to_be_visible(timeout=5000)
            expect(pb.locator("#offer-title")).to_have_text("A wants to match timing with you")
            expect(pb.locator("#offer-move")).to_have_text("Accepting will skip your stream forward 16.0 s.")
            expect(pb.locator("#offer-others")).to_contain_text("A: pause stream 5.0 s")
            pb.locator("#accept").click()

            expect(pa.locator(".toast").first).to_contain_text("Synced with B at 25.0 s behind live", timeout=5000)
            expect(pb.locator(".toast").first).to_contain_text("Synced with A")
            expect(pa.locator("#leave")).to_be_visible(timeout=5000)
            expect(pa.locator(".sync-box")).to_contain_text("In sync with B")
            browser.close()
        for ext in (a, b):
            targets = [m for m in ext.received if m["type"] == "set_target"]
            assert targets and targets[0]["target_delay"] == 25
    finally:
        a.stop.set(); b.stop.set()


@pytest.mark.e2e
def test_decline_shows_in_requesters_panel(server):
    room = f"panel-d-{int(time.time())}"
    a, b = FakeExtension("A", room, 3, 3), FakeExtension("B", room, 8, 8)
    a.start(); b.start()
    try:
        with sync_playwright() as pw:
            browser = pw.chromium.launch()
            pa, pb = browser.new_page(), browser.new_page()
            pa.goto(f"http://localhost:{PORT}/panel/?user=A&room={room}")
            pb.goto(f"http://localhost:{PORT}/panel/?user=B&room={room}")
            pa.locator('button[data-user="B"]').click(timeout=10000)
            expect(pb.locator("#offer-move")).to_have_text("Your stream won't change.", timeout=5000)
            pb.locator("#decline").click()
            expect(pa.locator(".toast").first).to_have_text("B declined", timeout=5000)
            browser.close()
        assert not [m for m in a.received if m["type"] == "set_target"]
    finally:
        a.stop.set(); b.stop.set()
