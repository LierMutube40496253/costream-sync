# Manual test on real YouTube with two PCs

Goal: two people (A and B) on two PCs, in normal Chrome, watch the same YouTube live stream.
A asks to match, B accepts, and both see the same moment. PC 1 also runs the sync server.
Takes about 15 minutes to set up.

## PC 1: run the server

1. Get the code and install it (once). You need Python 3.12 or newer:
   ```powershell
   git clone https://github.com/LierMutube40496253/costream-sync.git
   cd costream-sync
   py -m venv .venv
   .\.venv\Scripts\python.exe -m pip install -r requirements.txt
   ```
2. Copy `.env.example` to `.env`, and in `.env` change `COSTREAM_HOST=127.0.0.1` to
   `COSTREAM_HOST=0.0.0.0`. The server can then be reached from PC 2.
3. Start the server:
   ```powershell
   .\.venv\Scripts\python.exe -m server
   ```
   If Windows asks whether Python may use the network, allow **Private networks**.
4. Find PC 1's address: run `ipconfig` and note the **IPv4 Address**, for example
   `192.168.1.23`. The rest of this guide calls it `<IP>`.
5. Check it from PC 2: open `http://<IP>:8765/panel/` in a browser there. You should see
   "Join a room". If not, see "Problems" below.

## Both PCs: load the extension in normal Chrome

1. Get the `extension` folder onto the PC: clone the repository as above, or copy the folder.
2. Open `chrome://extensions`, turn on **Developer mode** (top right), click **Load unpacked**
   and choose the `extension` folder. "costream-sync" appears with an icon in the toolbar
   (pin it with the puzzle-piece button so you can see its badge).
3. Open the **same** YouTube live stream on both PCs, and let any ad finish.
4. Connect the extension. On `chrome://extensions`, under costream-sync, click **service worker**
   (next to "Inspect views"). A DevTools window opens; in its **Console**, type (with the YouTube
   tab as the active tab in Chrome):
   - on PC 1: `costream.join({server: 'ws://<IP>:8765', room: 'test', user: 'A', maxDelay: 30})`
   - on PC 2: `costream.join({server: 'ws://<IP>:8765', room: 'test', user: 'B', maxDelay: 30})`

   This step goes away once the Discord login exists. If you reload the extension, do it again.
5. Open the panel in a separate window (not on the YouTube page):
   - PC 1: `http://<IP>:8765/panel/?user=A&room=test`
   - PC 2: `http://<IP>:8765/panel/?user=B&room=test`

   The names must match the ones in step 4.

## The test

1. In each panel, check that your delay is shown, Measurement says **frame-exact**, and the
   other person is listed with their delay.
2. On PC 1, click **Match** next to B. PC 2's panel shows the offer, for example "Accepting will
   pause your stream 4.2 s". Click **Accept**.
3. Watch for about 30 s:
   - One player pauses or skips once, then plays slightly faster or slower for a few seconds.
   - The panels show "In sync with …" and an **offset** in ms.
   - The extension badge turns green with the error in ms.
4. Compare by eye or ear: something with a clear moment (a scene cut, a beat, a scoreboard
   change) should happen at the same time on both screens.
5. Optional: reload the YouTube page on one PC. After the preroll ad, it should rejoin on its
   own, without a new offer.

## What to send me

- The offset shown in the panels after 30 s, and whether it looked in sync.
- Whether YouTube showed "Something went wrong" in normal Chrome (this happened in the automated
  test browser, even without the extension).
- The extension's log: in the service-worker console, run `copy(JSON.stringify(costream.state(), null, 1))`
  and paste the result into a file. It contains your delays, the sync log and PC 1's local
  network address (the `<IP>` from above); it does not contain the stream URL or your public
  IP address.

## Problems

- **The panel doesn't open on PC 2:** both PCs must be on the same network. Allow Python in the
  Windows firewall (Windows Security → Firewall → Allow an app → Python, Private), then
  restart the server.
- **The extension log says "disconnected; retrying":** check the `<IP>` in step 4, and that the
  server is running. In the automated test this worked without extra permissions; if your
  Chrome blocks it, tell me the exact message from the service-worker console.
- **The panel says "extension not connected":** the extension's `user` and `room` must match
  the panel's exactly. Run step 4 again.
- **"Match" is greyed out:** both people must be on the same video, with no ad running.
