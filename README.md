# Personal Meeting-Scheduling Assistant

A private Telegram bot that books Google Calendar meetings for you.

You message the bot in plain language (e.g. *"Meeting with Rahul, rahul@gmail.com,
tomorrow 4pm"*). It reads the details using simple text rules (a regex for the email
and the `dateparser` library for the date/time), checks your calendar for conflicts,
shows you a summary to confirm, then creates a Google Calendar event with a Google
Meet link and emails the customer an invite.

**Only you can use the bot.** It ignores everyone else.

---

## Project files

| File | What it does |
|------|--------------|
| `bot.py` | The Telegram bot: conversation, buttons, booking flow. |
| `calendar_service.py` | Talks to Google Calendar: login, conflict check, create events, list upcoming. |
| `message_parser.py` | Reads your messages with regexes + `dateparser` (no AI). |
| `config.py` | Your settings (working hours, timezone, default length). Reads secrets from `.env`. |
| `.env` | Your secret keys. **Never shared, never committed.** |
| `.env.example` | Template showing which keys are needed. |
| `check_setup.py` | A health-check script. |
| `test_calendar.py` | One-time Google login + test event. |
| `run.bat` | Double-click launcher (used for 24/7 running too). |
| `requirements.txt` | The Python packages the project needs. |

Secret files kept out of git by `.gitignore`: `.env`, `credentials.json`, `token.json`.

---

## Commands (in Telegram)

| Command | What it does |
|---------|--------------|
| (any message) | Describe a meeting; the bot parses it and asks for anything missing. |
| `/start` | Welcome message. |
| `/help` | How to use the bot. |
| `/upcoming` | List your next 10 meetings. |
| `/cancel` | Forget the current booking and start fresh. |

---

## Settings (edit in `config.py`)

- `TIMEZONE` - default `Asia/Kolkata`
- `DEFAULT_MEETING_MINUTES` - default `30`
- `WORK_START_HOUR` / `WORK_END_HOUR` - default `10`-`19`
- `WORK_DAYS` - default Monday-Saturday (`0`-`5`)

---

## Running it (day to day)

1. Open the project folder.
2. Double-click **`run.bat`** (or in PowerShell: `.\.venv\Scripts\Activate.ps1` then `python bot.py`).
3. Leave the window open. Message your bot in Telegram.
4. To stop: close the window, or press **Ctrl + C**.

---

## Running it 24/7 (so you don't keep a window open)

The bot only works while `bot.py` is running. Here are two options.

### Option A - Keep it running on this PC (simplest, free)

Use Windows **Task Scheduler** so the bot starts automatically and restarts if it crashes.
Your PC must be on and online for the bot to answer.

1. Press **Start**, type **Task Scheduler**, open it.
2. Right side -> **Create Task...** (not "Basic Task").
3. **General** tab: Name it `Meeting Bot`. Tick **"Run whether user is logged on or not"**.
4. **Triggers** tab -> **New...** -> Begin the task: **At log on** -> OK.
5. **Actions** tab -> **New...** -> Action: **Start a program** ->
   - Program/script: browse to your **`run.bat`**.
   - "Start in": paste the project folder path.
6. **Settings** tab: tick **"If the task fails, restart every"** -> 1 minute, up to 3 times.
   Untick "Stop the task if it runs longer than...".
7. **OK**. It starts at every logon. To start it now: right-click the task -> **Run**.

To check it's working, message your bot. To stop it: Task Scheduler -> right-click -> **End**,
and disable the task so it doesn't restart.

### Option B - Run on a cheap cloud server (always on, even when your PC is off)

Rent a small Linux VM (e.g. a $4-6/month instance). Then:

1. Install Python 3.11+, copy the project files over (including `.env`, `credentials.json`,
   `token.json` - generate `token.json` locally first with `python test_calendar.py`,
   because cloud servers have no browser).
2. Create a virtual environment and `pip install -r requirements.txt`.
3. Run it under a process manager so it restarts automatically, e.g. a `systemd` service
   running `python bot.py`.

Ask your assistant to walk you through Option B in detail if/when you want it.

---

## Troubleshooting

- **Bot doesn't reply:** is `bot.py` actually running? Check the window/logs.
- **"This is a private bot."** to you: your `OWNER_CHAT_ID` in `.env` is wrong.
- **Google errors after months:** delete `token.json` and run `python test_calendar.py` to log in again.
- **Re-check setup anytime:** `python check_setup.py`.
