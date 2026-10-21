# Co-stream Sync: Design Document

Oct 21, 2026 · @mlops

## Problem and goal

Co-streamers who watch the same official YouTube broadcast each see it with a different delay, so they comment on different moments. Today they fix it by pausing and coordinating by hand.

**Example.** A sees a kill 3 s after it happens and B sees it 8 s after. A shouts "what a play!" while B hasn't seen it yet.

**Goal.** A clicks B, B accepts, and A's YouTube player shifts 5 s so both see the same moment, within one video frame (about 20–35 ms). The sync then holds on its own until someone leaves.

## Scope of v1

v1 syncs people in the same Discord voice channel who are watching the same YouTube live video on youtube.com.

**In scope**

- YouTube live on youtube.com, watched in Chrome as normal
- Main use: League of Legends; also Valorant and any other YouTube live
- Groups = members of the same Discord voice channel
- A panel that runs as an OBS custom dock or as a normal browser window (no OBS needed for testing)

**Out of scope for v1**

- Twitch (its player can't rewind live; revisit later)
- Syncing across different videos or platforms (would need audio fingerprinting)
- Compensating for Discord voice delay (see Timing model)
- A native C++ OBS plugin (the dock covers it for now)

## Architecture

Four parts: two on the streamer's PC and two in the cloud. Only the extension ever touches the video.

&#91;embedded content: architecture · 4 components\]

- **Chrome extension**: reads the frame's broadcast time from YouTube's player, and applies a target delay by pausing (to add delay) or skipping forward (to remove it), plus small speed changes. It draws nothing on the page.
- **Panel**: one web page that shows the group, requests and sync status. In OBS it's a custom dock; for testing it's a normal browser window.
- **Sync server**: gives every client the same clock, applies the sync rules, and forwards requests and answers.
- **Discord bot**: added to the group's Discord server once; tells the server who is in which voice channel.

## Timing model and precision

The target is one video frame (16.7 ms at 60 fps, 33 ms at 30 fps) for users near each other. Across continents it is typically under about 30 ms, with a worst case set by the network: clock sync can be off by up to half the round trip when the route out and back differ (up to 150 ms for a 300 ms round trip to Sydney).

**Delay definition.** Delay = server time now − broadcast time of the frame on screen. YouTube's page player gives that broadcast time directly (`getMediaReferenceTime()`, confirmed on a live stream).

| Error source | Example | Fix |
| --- | --- | --- |
| PC clocks differ | One test PC was 2.3 s slow | Each client syncs to the sync server's clock with an NTP-style ping exchange (many samples, keep the lowest round-trip ones) |
| Broadcast stamps differ between viewers | Each 5 s chunk's stamp jumps about 40 ms; viewers who joined at different moments end up tens of ms apart while both think they're in sync | The server picks one reference stamp per sync, and `set_target` is expressed on the player's own clock (`getCurrentTime()`), which is identical to 0.1 ms for every viewer of the same frame |
| Player time updates in steps | The value jumps when a new chunk starts | Interpolate between updates with `requestVideoFrameCallback` / `video.currentTime` |
| A jump is not exact | A 5 s rewind lands as +5.12 s (keyframe), while a 4 s pause adds exactly 4.00 s | Two stages: coarse pause (to add delay; never rewind) or skip forward (to remove it), then fine-tune with playback speed 0.98–1.02× (pitch preserved) |
| Drift over time | B rebuffers and falls 400 ms behind | Sync is continuous: keep measuring and nudging speed while synced |
| Lopsided network routes | Slow trip out, fast trip back | Can't be fully removed; measured in the simulator and shown in the reported offset |

**Discord voice delay (out of scope).** Voice adds about 50–150 ms. It can only be compensated in one direction: delaying B so B hears A in time makes A hear B twice as late. Equal video timing is the fair choice for equal co-casters. A "main caster" mode could be added later.

**Reported precision.** The panel shows the achieved offset between synced users in ms, so real precision is always visible.

## Sync rules

The group syncs at the earliest moment every member can reach, so nobody watches later than needed.

1. Each user has a range. Earliest = their live edge (they can't see the match sooner). Latest = live edge + their max extra delay (default 30 s).
2. Target = the highest live edge in the group, which is the slowest member's earliest moment. Anyone can move anywhere inside their range, forward or back.
3. Offers state the move from where the user is sitting now, not from their live edge. Example: B's live edge is 25 s but B is sitting at 41 s, so the offer says "Accepting will skip your stream forward 16 s", not "+0 s".
4. The target must be inside everyone's range. If the ranges don't all overlap, the server picks the largest group whose ranges do overlap (a standard interval-overlap check, fast for up to 10 users). The requester is always in the group. If two groups of the same size fit, the one with the lower target wins (the earlier moment).
5. Max gap: nobody is synced more than 10 s behind the group's fastest member (a setting). Anyone further behind is left out, with a message suggesting lower video quality or a connection check.
6. Matching is a request: nothing changes until the other side accepts (see Key flows).
7. When someone leaves, the target is recomputed, and the rest move forward only if all accept.

**Worked example**

| User | Live edge | Max extra | Range | Result |
| --- | --- | --- | --- | --- |
| A | 3 s | 30 s | 3–33 s | Waits 5 s |
| B | 8 s | 10 s | 8–18 s | Waits 0 s (slowest in the group) |
| C | 6 s | 20 s | 6–26 s | Waits 2 s |
| D | 15 s | 20 s | 15–35 s | Left out: 12 s behind the fastest, over the 10 s gap |

(Delays in this example assume each user is sitting at their live edge; offers are worded from their actual position, rule 3.)

Result: A, B and C sync at 8 s, the earliest moment B can reach. D's range overlaps everyone's, so without the gap rule the whole group would sync at 15 s, 7 s further from live, just for D. The gap rule keeps the group at 8 s.

## Key flows

Every change to someone's player starts with their own accept. After that, the sync keeps itself in place.

**Match request** (A at 3 s clicks B at 8 s)

&#91;embedded content: match request · A (3 s) asks B (8 s)\]

If B had clicked A instead, A gets the offer "Accepting will pause your stream 5 s", because A is the one who moves.

**Sync all**

1. Anyone who would exceed their max is left out up front, with the reason.
2. Everyone else gets the offer.
3. Those who accept sync at the slowest delay among them. Example: B declines and D (6 s) accepts, so A and D sync at 6 s.

**Ad and auto-rejoin** (A, B, C synced at 8 s; A gets a 30 s ad)

1. A's extension detects the ad and reports "in ad"; B and C keep watching normally.
2. The ad ends and A's player is somewhere off the group's moment.
3. A is behind the group: the extension jumps him forward to the group's moment (never past live). A is ahead: it pauses him until the group catches up.
4. A is back in sync with no new request, because he already accepted.
5. Only if the gap exceeds his max is A removed, with a message.

The same happens after a page reload (for example after YouTube's "Something went wrong" and a
refresh): the sync is kept in the background script and A rejoins without a new request.

Check 2 (Oct 21, one window, one action at a time): pause 5 s, rewind 5 s, skip forward 5 s and
18 s (inside the downloaded range) and speed 0.98× for 10 s caused no error. The player was frozen
after the first seek during that check, and skipping past the downloaded range was not tested.

## Communication protocol

All parts talk to the sync server over WebSocket (wss) with JSON messages. The extension and the panel never talk to each other directly. Both log in with the same Discord account, and the server pairs them.

| Message | From → to | Content | When |
| --- | --- | --- | --- |
| `welcome` | server → any | login mode (`discord` or `dev`), Discord client id and login address | First message on every connection |
| `hello` | extension, panel → server | Discord access token (scope `identify`), role. The server asks Discord whose token it is (`/users/@me`) and uses that user id, never one the client claims | On connect |
| `hello` (bot) | Discord bot → server | role `bot`, shared key (`BOT_SHARED_SECRET`) | On connect |
| `whoami` | extension popup → server | token; answer: Discord id and name, nothing registered | After logging in |
| `no_voice_channel` | server → panel, extension | the user is in no voice channel the bot can see | On connect or when they leave |
| `clock_ping` / `clock_pong` | extension ↔ server | client send time / server receive and send times | Every few seconds |
| `status` | extension → server | video id, current delay, live edge, max delay, player clock time, in\_ad, dvr\_available, measurement quality | About every 250 ms |
| `group` | server → panel | voice channel members with their delay and state | On any change |
| `match_request` | panel → server | target user | A clicks Match |
| `sync_all_request` | panel → server | none (whole voice channel) | A clicks Sync all |
| `match_offer` | server → each asked panel | requester, group target, and for each user: move from their current position (e.g. "skip forward 16 s", "pause 5 s", "no change") | Request forwarded |
| `match_answer` | panel → server | offer id, accept or decline | Within 15 s |
| `match_result` | server → all asked panels | per user: accepted, declined, timed out, or left out with reason; final group and target | After all answers or timeout |
| `set_target` | server → extension | sync session id, reference stamp, target on the player clock | After accept, then on each correction |
| `leave_sync` | panel or extension → server | sync session id | User leaves |
| `voice_members` | Discord bot → server | voice channel id, member ids | On join or leave |

The extension re-checks every `set_target` against its own max and the user's acceptance before acting (see Safety).

**Login.** The panel and the extension each log in with Discord (OAuth2 token grant, scope
`identify`). The panel uses a "Log in with Discord" button. The extension uses its toolbar popup,
which only shows "Log in with Discord", the logged-in name, the server connection status and
"Log out". No Discord client secret exists anywhere, and the only secrets (bot token, bot key)
live in the server's `.env`. Panel and extension are paired because the server resolves both
tokens to the same Discord user id. Leaving the voice channel ends that user's sync. A server
without `DISCORD_CLIENT_ID` runs in **dev mode** (name + room instead of Discord), which the
tests use. Setup: [DISCORD_SETUP.md](DISCORD_SETUP.md).

## Safety, privacy and invisibility

The extension never touches the video unless the user accepted, and nothing it does shows up on stream.

**Never interferes unless asked**

- Read-only by default: it measures, changes nothing.
- Acts only inside a sync the user accepted in this session.
- Enforced in the extension itself: a `set_target` beyond the user's max, or outside an accepted sync, is ignored, even if the server sends it.
- Every server message is validated (type, range, session id).
- "Leave sync" instantly restores normal playback: speed 1.0 and no further control.

**Invisible on stream**

- Nothing is ever drawn on the YouTube page, because streamers capture it in OBS.
- All UI lives in the panel: an OBS dock, which isn't part of the stream output, or a separate browser window.
- Fine corrections use small speed changes, which viewers don't notice.

**Privacy and access**

- Permissions: youtube.com, plus Chrome's identity permission for the Discord login. Development builds also allow localhost for local testing. No history, tabs or other sites.
- Encrypted connection (wss) only.
- Data kept: Discord user id, display name, voice channel id, current delays. Nothing else, and no history stored.
- Discord login means nobody can pretend to be another user; requests are rate-limited.

## Robustness

Each known way the sync can break has a planned response.

| Situation | What happens | Response |
| --- | --- | --- |
| Ad on one user | Player time freezes; afterwards the user is 15–40 s off (measured: 42 s after a preroll) | Status "in ad", sync suspended for that user only, automatic rejoin after the ad (see Key flows) |
| YouTube pauses the video itself | Cookie or consent dialog, autoplay rules; seen in testing | Detect pauses the extension didn't cause and that the user didn't click; show "YouTube paused your video" in the panel and resume sync once playback resumes, instead of treating it as the user leaving |
| YouTube shows "Something went wrong" on its own | Check 1 (Oct 21): two automated test windows, extension not loaded, no control, 5 min: both errored about 30 s into playback, 2 s apart, with no ads at all. Not caused by us | The extension reports `youtube_error` and does nothing; after the user refreshes, the sync resumes (see Page reload). Tests use the fake YouTube page; real YouTube only for the live smoke test. Test scripts never auto-reload: that looped (reload → preroll ad → the other window errors → reload) |
| Player frozen (rebuffer, or stuck after a seek) | Playing, but the position doesn't move; seen after seeks in test windows | The extension waits (`stalled`) and corrects once playback moves again, instead of skipping forward mid-freeze |
| Page reload | The page's state is lost; YouTube restarts at live after a preroll ad | The accepted sync lives in the background script. The reloaded page rejoins on its own, like after an ad: no new offer, still within the user's max. It first waits until it can measure and has seen YouTube's newest chunk move twice (up to about 10 s), so the target isn't judged against a stale live head |
| Window covered or minimised | Chrome stops reporting new frames; precision falls back to `video.currentTime` | Report lower measurement quality in `status`; the panel shows it next to that user |
| DVR (rewind) disabled on the stream | Seeking back fails | Pause instead; we only ever add delay from the live edge, so pause always works |
| Different videos | Timestamps can't be compared | Match is only offered between users on the same video id |
| YouTube changes its player | `getMediaReferenceTime()` may disappear or change | Fallback: the player's progress state (`ingestionTime`). YouTube's stats latency value is not a usable fallback (it returned nothing on a normal-latency stream). If neither works: a clear "can't measure" error, never a silent wrong sync. A daily live check warns us |
| User rebuffers | Falls behind the group | Continuous sync nudges back; a gap over the max removes the user with a message |
| Network drop to the server | No new targets | Extension keeps the current delay; after 10 s it hands back normal playback |
| Someone leaves the group | The rest may be later than needed | Step back down toward the new target, only if all remaining members accept |

## Testing strategy

Every push runs all automated tests. Only the live smoke test needs a real YouTube stream.

| Level | What it checks | How | Runs |
| --- | --- | --- | --- |
| Unit | Sync rules, max delay, accept/decline/timeout, clock-offset math, message validation | pytest (server), Vitest (extension logic) | Every push |
| Integration | Server plus several simulated clients, including a jittery one, converge within one frame; ad and rejoin; network drop | Simulated clients over real WebSockets | Every push |
| End-to-end | Real Chrome with the extension and panel on a local test page with a fake live player | Playwright | Every push |
| Live smoke | Measurement still works on a real YouTube live | Script on a 24/7 stream (e.g. Lofi Girl) | Daily and before a release |

Already done: 21 server tests, 33 extension tests, 5 simulator scenarios, and the end-to-end test.

**How "within one frame" is measured.** Screens only show whole frames, so two players that are 8 ms apart read as −8 ms one moment and +25 ms the next on a frame counter. The end-to-end test therefore checks the mean offset over 20 s, which must be within half a frame at 30 fps (16.7 ms, one frame at 60 fps). No single sample may be more than two frames apart, so the screens never differ by more than one frame.

### Simulator: far-apart users on one PC

A simulator runs up to 10 virtual users on one PC, each with its own network, clock and stream conditions. It tests users far apart without needing real computers in other countries.

| Condition per virtual user | Example values |
| --- | --- |
| Network delay to the server | 20 ms (Copenhagen), 150 ms (US West), 300 ms (Australia) |
| Jitter and packet loss | ±40 ms, 2% loss, lopsided routes (e.g. 200 ms out, 100 ms back) |
| Clock error | 2.3 s slow, drifting 50 ms per minute |
| Live edge | 3 s to 40 s behind the real game |
| Events | Rebuffer at minute 2, 30 s ad at minute 4, user leaves |

**How it works**

- Each virtual user runs the real extension logic against a fake YouTube player that has its own broadcast clock.
- A network shaper (e.g. Toxiproxy) sits between each virtual user and the real sync server and adds the delay, jitter and loss.
- Scenarios are small files (e.g. "Copenhagen + Sydney, Sydney gets an ad"), so any problem can be replayed exactly.

**What it shows**

- A live graph of each user's delay over time, and the offset between users in ms.
- Pass or fail: everyone within one frame of the target within a set time after accept, and back in sync after ads and rebuffers.
- The same scenarios run in GitHub Actions, so a change that breaks far-apart sync fails the build.

**Real-world check**: before a release, run one real client on a cloud machine in another region (or with a friend abroad) next to a local one, on the same YouTube live.

## Deployment and DevOps

Code lives in one GitHub repository, tested on every push and released in small versions.

**Repository layout**

- `server/`: sync server (FastAPI, WebSocket) and Discord bot
- `extension/`: Chrome extension (Manifest V3)
- `panel/`: the panel web page, served by the server
- `tests/`: unit, integration and end-to-end tests
- `tools/`: `delay_probe.py` and the live smoke test; \`demo/\` holds the demo scripts and simulator scenarios

**Pipeline**

1. Push to GitHub, and GitHub Actions runs lint and all automated tests.
2. Merge to `main` deploys the server to staging.
3. A version tag deploys the server to production (e.g. Fly.io or Render) and builds the extension zip.
4. The extension is published to the Chrome Web Store, which needs a privacy policy and a short permissions justification.

**For streamers**

- Install the extension, log in with Discord, and add the bot to the Discord server once.
- In OBS, add the panel's address as a custom browser dock. Without OBS, open the same address in a browser window.

**Repo basics**: README with setup and a demo GIF, MIT license, CHANGELOG, issue templates.

## Roadmap and status

The measurement method and the request-based sync server are proven. The extension core is next.

- [x] Prove timing on live YouTube: page player gives frame broadcast time, steady to 0.1–0.2 s
- [x] Sync server with request, accept, decline and timeout; doc protocol, offers worded from the current position, shared reference stamp (21 tests)
- [x] Extension core: measuring on the server clock, pause/skip plus speed fine-tuning, ad detection, local safety checks (33 tests)
- [x] Frame-level precision: frame callbacks, player-clock targets, offset reported in ms
- [x] Panel web page (works in a browser window and as an OBS dock)
- [x] Discord login and bot: groups = voice channel members (tested against a fake Discord; not yet with a real Discord application)
- [x] Ad auto-rejoin, rejoin after a page reload, continuous sync
- [x] Far-apart simulator (5 scenarios) and end-to-end test on a fake YouTube page (two headless Chromium windows, within one frame after sync, ad, reload, error + refresh)
- [ ] GitHub Actions
- [ ] GitHub repo, README, license, first release
- [ ] Host the server, publish to the Chrome Web Store
- [ ] Later: native OBS plugin, Twitch, main-caster mode

**Open decisions**

- GitHub repository name and whether it's public
- Hosting provider for the server
- Default max delay (30 s proposed) and max gap behind the fastest member (10 s proposed)
- Decided: stepping back down after someone leaves needs everyone's accept
