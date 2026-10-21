# Discord setup

One Discord application gives you both the login and the bot. About 10 minutes, once.

## 1. Create the application

1. Go to https://discord.com/developers/applications and click **New Application**. Name it,
   for example "costream-sync".
2. On **General Information**, copy the **Application ID**. In your `.env`:
   `DISCORD_CLIENT_ID=<Application ID>`. This id is public: it appears in the login link.

## 2. Allow the login redirects

On **OAuth2 → Redirects**, add every address that will log in:

- the panel: `http://localhost:8765/panel/`. When testing with two PCs, also add
  `http://<PC 1's IP>:8765/panel/`, if Discord accepts it (Discord may require `https` for
  non-localhost addresses; if it refuses, use the panel on PC 1 only, or put the server behind
  https).
- the extension: open the extension's service-worker console (`chrome://extensions` →
  costream-sync → service worker) and run `chrome.identity.getRedirectURL()`. It looks like
  `https://<extension id>.chromiumapp.org/`; add exactly that. Each PC that loads the extension
  unpacked from a different folder gets a different id, so add each one.

No client secret is used anywhere: the panel and the extension get a token with only the
`identify` scope (your Discord id and name), and the server checks it with Discord.

## 3. The bot

1. On **Bot**, click **Reset Token**, copy it, and in your `.env`: `DISCORD_BOT_TOKEN=<token>`.
   Treat it like a password; it only belongs in `.env`.
2. No privileged intents are needed (leave "Server Members" and "Message Content" off).
3. Make a shared key for the bot and the server:
   `python -c "import secrets; print(secrets.token_urlsafe(32))"` and in `.env`:
   `BOT_SHARED_SECRET=<that string>`.
4. Invite the bot to your Discord server: **OAuth2 → URL Generator**, scope **bot**, permission
   **View Channels**. Open the generated link and pick your server.

## 4. Run it

```powershell
.\.venv\Scripts\python.exe -m server        # sync server + panel, now in Discord mode
.\.venv\Scripts\python.exe -m server.bot    # in a second terminal
```

The bot prints `bot logged in as …` and `connected to ws://127.0.0.1:8765/ws`.

## 5. Use it

- Join a voice channel on that Discord server.
- Open the panel (`http://localhost:8765/panel/`) and click **Log in with Discord**. The header
  shows your name and the voice channel.
- In Chrome, click the extension's icon and **Log in with Discord**. Open a YouTube live; the
  extension connects on its own, and your delay appears in the panel.
- Everyone in the same voice channel shows up in the panel; Match and Sync all work as before.

## What the server keeps

Your Discord user id, display name and voice channel, and your current delays, in memory only.
No message content, no history, nothing written to disk.
