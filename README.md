# Telegram Song Control

A Python Telegram bot with a mobile-friendly private dashboard. It supports `/song`, `!song`, `/video`, `!video`, `/search`, and `!search`. The bot's `/start` command is intentionally silent.

## Run

1. Install Python 3.11+ and FFmpeg (the project can use `imageio-ffmpeg` if system FFmpeg is absent).
2. Install dependencies:

   ```bash
   python3 -m pip install -r requirements.txt
   ```

3. Configure `.env` with your bot token, API endpoint, dashboard password, host, and port. `.env.example` is a template. Never publish `.env` or the `data/` directory.
4. Start the combined service (installs dependencies automatically):

   ```bash
   ./start.sh
   ```

   On Windows, run `start.bat`. You can also install `requirements.txt` yourself and run `python3 run.py`.

5. Open `http://127.0.0.1:8000` (or the configured port) and sign in using the dashboard password. For a phone, open the dashboard over a trusted HTTPS connection or a private network; do not expose plain HTTP to the public internet.

The dashboard binds to `0.0.0.0` by default so a trusted network/preview can reach it. Protect it with HTTPS and keep the password private. The dashboard includes login throttling, CSRF protection, an HTTP-only signed session cookie, and security headers. The bot API token and dashboard password belong only in `.env`.

## Link your Telegram user account

From the dashboard, submit your Telegram API ID, API hash, and phone number with country code. Enter the Telegram login code, then the account's two-step verification password if requested. Get API credentials through Telegram's official developer portal. The API hash, login code, and two-step password are used only during the current login flow; they are not written to disk or logs. The authorized Telethon session is stored with owner-only file permissions under `data/` so it can reconnect after restart. Treat that session file as a password: anyone who obtains it may control the linked Telegram session. The dashboard's disconnect action logs out and removes this local session.

Telegram may restrict automated use of personal accounts. Use this only for your own account and within Telegram's rules. The linked account ignores ordinary text and `/start`. Other people can trigger it only by replying directly to a message sent by the linked account with a complete media command and query. The account owner can issue a complete command anywhere and the account sends the result as a reply to that command. A command without a query does nothing. Commands are:

- `/song <name>` or `!song <name>` — search, download, and send MP3 audio.
- `/video <name>` or `!video <name>` — find and send compatible HD MP4 video.
- `/search <name>` or `!search <name>` — show choices; reply to the results with `1`–`5` for audio or `1v`–`5v` for video.
- A YouTube URL can be used in place of a name.

The linked account replies in the same chat and quotes the requesting command. Telegram only permits bots—not regular user accounts—to send inline keyboards. Accordingly, linked-account `/search` results ask you to reply to the result card with `1`–`5` for audio or `1v`–`5v` for video; request an alternate media type with `/song <name>` or `/video <name>`. When the linked account is disconnected, the Bot API bot retains its single alternate-format button.

## Files

- `bot.py`: Telegram Bot API bot.
- `dashboard.py`: password-protected account dashboard.
- `services/user_account.py`: Telethon login and explicit-command listener.
- `services/downloader.py`: audio/video download and processing.
- `data/`: local account session and dashboard cookie signing key (sensitive; do not share).
