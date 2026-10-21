# costream-sync

Co-streamers watching the same YouTube live broadcast see the same moment, within about one
video frame. The design and its rules live in [docs/DESIGN.md](docs/DESIGN.md) (source of
truth). This README covers how to run each part.

| Part | Status |
|---|---|
| `server/`: sync rules, offers, accept/decline/timeout, sessions, clock (doc protocol) | done, 21 tests |
| `extension/`: Chrome MV3 core: measure, hold target, ads, freezes, reload rejoin, local safety checks | done, 33 tests; no UI except the badge |
| `demo/sim/`: fake YouTube player + far-apart simulator with live graph | done, 5 scenarios pass |
| `demo/e2e/`: end-to-end test, two headless Chromium windows on a fake YouTube page | done, passes (within one frame) |
| `tools/youtube_checks.py`: checks on real YouTube | live smoke checks only (see below) |
| Panel, Discord login and bot, CI, deployment | not started |

## Setup (Windows, PowerShell)

```powershell
cd costream-sync
py -3.13 -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r requirements.txt
npm install
```

## Tests

```powershell
.\.venv\Scripts\python.exe -m pytest -q                 # server (21) + end-to-end (1, about 3 min)
.\.venv\Scripts\python.exe -m pytest -q -m "not e2e"    # quick: server only
npx vitest run                                          # extension logic (33)
npm run sim:all                                         # 5 simulator scenarios (about 9 min)
```

## End-to-end test (demo/e2e/)

```powershell
.\.venv\Scripts\python.exe demo\e2e\two_window_e2e.py            # headless
.\.venv\Scripts\python.exe demo\e2e\two_window_e2e.py --headed   # watch the two windows
```

This runs two Chromium instances with the real extension (all parts, including `background.js`
and `content.js`) on `fake_youtube.html`. That page answers as `www.youtube.com` (Playwright
serves it locally) and behaves like YouTube's live player, as measured:
- a live head that moves in real time
- `seekableEnd` that only moves on downloads
- per-chunk stamps
- ads, YouTube's error box, freezes
- a reload restarts at live after a preroll

The test video (`make_test_video.py`, generated once) has a frame counter, and the true offset
between the windows is read from it. The test steps:
1. A clicks Match on B, B accepts (B starts 15 s behind).
2. An 8 s ad on B.
3. A reloads the page.
4. YouTube error on B, then a refresh.

Each step must end within one frame, with no new offer.

How "within one frame" is checked: screens show whole frames, so two players that are 8 ms apart
read as −8 ms or +25 ms on the frame counter, depending on where the frame boundary falls. This
happens with visible windows too. The check therefore uses the mean over 20 s, which recovers
the real offset, and it must be within half a frame at 30 fps (16.7 ms). On top of that, no
single sample may be more than two frames (66.7 ms) apart, so the screens never differ by more
than one frame.

Run as part of `pytest` (2026-10-21):

| Step | Mean | Worst sample |
|---|---|---|
| Before sync | +14,983 ms | |
| After accept | 7.7 ms | 25.3 ms |
| After the ad | 4.7 ms | 25.4 ms |
| After the reload | −1.5 ms | 25.1 ms |
| After error + refresh | −3.2 ms | 17.2 ms |

One offer was shown in total. In the five runs since this rule was set, every step passed, with
means between −5.6 and +8.4 ms.

## Sync server

```powershell
.\.venv\Scripts\python.exe -m uvicorn server.app:app --port 8765
```

Everything connects to `ws://localhost:8765/ws` and starts with `hello` (role `extension` or
`panel`, `user`, `room`). Until the Discord bot exists, `room` stands in for the voice channel.
The message list is at the top of `server/app.py` and in the design doc.

Example (rule 3): A sits at its live edge of 20 s. B's live edge is 25 s, but B sits at 41 s. A
clicks Match on B. B's panel gets "Accepting will skip your stream forward 15.5 s". Both
extensions then get `set_target`: one reference stamp, and the target on the player clock.

## Extension (extension/)

| File | Runs in | Does |
|---|---|---|
| `page.js` | YouTube page (MAIN world) | reads the player (`getCurrentTime`, `getMediaReferenceTime`, live head). Read-only |
| `content.js` | isolated world | YouTube adapter: frame-exact measurement with `requestVideoFrameCallback`; pause, skip and speed |
| `controller.js` | content + simulator | holds the target: pause / skip forward, then 0.98–1.02x speed |
| `client_core.js` | background + simulator | protocol, clock sync (lowest-RTT pings), acceptance and max checks |
| `sync_math.js` | everywhere | pure maths |
| `background.js` | service worker | one server connection per YouTube tab, badge |

Load it in Chrome: open `chrome://extensions`, turn on Developer mode, click "Load unpacked"
and choose `extension/`. Until the panel and Discord login exist, joining is done from the
service worker console: `costream.join({room: 'demo', user: 'A', maxDelay: 30})`.

## Simulator (demo/sim/)

```powershell
node demo/sim/run.js demo/sim/scenarios/copenhagen_sydney.json   # live graph: http://localhost:8790
npm run sim:all
```

Each virtual user runs the real `controller.js` and `client_core.js` against a fake YouTube
player, through a network shaper (delay per direction, jitter, loss), on its own wrong clock, with
the real Python server. Pass or fail comes from ground truth: the fake players know exactly what
each user sees. Results go to `demo/sim/out/`.

Results (2026-10-21):

| Scenario | What happens | Worst offset | Limit |
|---|---|---|---|
| `nearby` | A at 20 s, B edge 25 s sitting at 41 s, B's clock 2.3 s slow | 3.6 ms | 33 ms |
| `doc_example` | A/B/C/D Sync all: A, B, C sync at 8 s, D left out (12 s over the gap) | 5.9–12.4 ms | 33 ms |
| `youtube_head` | YouTube lets you play 0.5 s from its newest chunk; A and B sit there (real demo run 8: both refused the target before the fix) | 15.8 ms | 33 ms |
| `copenhagen_sydney` | B: 200 ms out / 100 ms back, ±40 ms jitter, 2% loss, clock 2.3 s slow drifting 50 ms/min; 30 s ad on B; 1.5 s rebuffer on A; YouTube pauses A for 4 s | 71–75 ms | 83 ms |

The fake player's `seekableEnd` behaves like YouTube's: it only moves when a chunk is downloaded,
never during pauses, freezes or ads. The extension's live-edge estimate (`HeadTracker` in
`sync_math.js`) keeps the head moving in real time, and ignores catch-up downloads after a pause.
In all scenarios each user's reported live edge is within 0.4 s of the true one, and the run logs
this error as `*_edge_err_ms`.

The 83 ms limit is one frame plus 50 ms. A route with 200 ms out and 100 ms back shifts the clock
estimate by (200 − 100) / 2 = 50 ms, and no client can see that (design doc, Timing model). On
symmetric routes the result stays within one frame.

## Real YouTube

Full sync demos on real YouTube are **not** used as tests any more. In automated test windows,
YouTube shows "Something went wrong" on its own, about 30 s into playback, in both windows at
once, even with no extension loaded and no ads (check 1, see the Robustness table in docs/DESIGN.md).
Real YouTube is only used for the live smoke checks:

```powershell
.\.venv\Scripts\python.exe tools\youtube_checks.py ads     "https://www.youtube.com/watch?v=<live id>"   # 2 windows, no extension, 5 min
.\.venv\Scripts\python.exe tools\youtube_checks.py actions "https://www.youtube.com/watch?v=<live id>"   # 1 window, pause/rewind/skip/speed
```

`demo/two_window_demo.py` (two real-YouTube windows) is kept for manual runs. It no longer
auto-reloads: reloading looped with YouTube's preroll ads.

## Tools

- `tools/delay_probe.py`: HLS `#EXT-X-PROGRAM-DATE-TIME` probe, used for cross-checks
  (`--self-test`, or `<url> --duration 60`). Logs from the first validation are in `tools/logs/`.
- `demo/debug_player.py`: opens one window and prints the YouTube player's state each second.
