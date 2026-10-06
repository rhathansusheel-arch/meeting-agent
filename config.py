"""
config.py
---------
All of your settings and secrets live here (or in the .env file).
Edit the SETTINGS section below to change how the bot behaves.

Secrets (tokens, keys) are NEVER written in this file. They are read from
the .env file, which is kept out of git by .gitignore.
"""

import os
import re
from pathlib import Path
from dotenv import load_dotenv

# Folder this file lives in. We build all paths from here so the bot works
# no matter which folder you run it from.
BASE_DIR = Path(__file__).resolve().parent

# Load the .env file (if it exists) into the environment.
load_dotenv(BASE_DIR / ".env")


# ======================================================================
# SECRETS  (read from .env — do not type the real values here)
# ======================================================================

TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "")

# Your Telegram chat ID(s). Stored as text in .env (one id, or several separated
# by commas/spaces). We turn them into a list of numbers here.
_owner_raw = os.getenv("OWNER_CHAT_ID", "")
OWNER_CHAT_IDS = [
    int(piece)
    for piece in re.split(r"[,\s]+", _owner_raw.strip())
    if piece.lstrip("-").isdigit()
]
# A single "primary" id, used when we need just one (e.g. error notifications).
OWNER_CHAT_ID = OWNER_CHAT_IDS[0] if OWNER_CHAT_IDS else None


# ======================================================================
# GOOGLE FILES
# ======================================================================

# You download this from Google Cloud (Stage 1, step 5).
GOOGLE_CREDENTIALS_FILE = BASE_DIR / "credentials.json"

# Created automatically the first time you log in to Google (Stage 2).
GOOGLE_TOKEN_FILE = BASE_DIR / "token.json"

# What we are allowed to do with your Google account: manage calendar events.
GOOGLE_SCOPES = ["https://www.googleapis.com/auth/calendar"]


# ======================================================================
# SETTINGS  (safe to edit — these change how the bot behaves)
# ======================================================================

# Timezone all dates/times are understood in.
TIMEZONE = "Asia/Kolkata"

# Default meeting length in minutes, used when you don't mention one.
DEFAULT_MEETING_MINUTES = 30

# Your usual working hours, in 24-hour time (0-23).
# The bot will WARN you (but still let you continue) outside these hours.
WORK_START_HOUR = 10   # 10:00 AM
WORK_END_HOUR = 19     # 7:00 PM

# Your working days. 0 = Monday, 6 = Sunday.
# Default below is Monday-Saturday (Sunday off).
WORK_DAYS = [0, 1, 2, 3, 4, 5]

# Which Google calendar to use. "primary" means your main calendar.
CALENDAR_ID = "primary"


# ======================================================================
# Small helper so other files can check everything is configured.
# ======================================================================

def missing_settings():
    """Return a list of human-readable names of anything not yet filled in."""
    missing = []
    if not TELEGRAM_BOT_TOKEN:
        missing.append("TELEGRAM_BOT_TOKEN (.env)")
    if not OWNER_CHAT_IDS:
        missing.append("OWNER_CHAT_ID (.env)")
    if not GOOGLE_CREDENTIALS_FILE.exists():
        missing.append("credentials.json (Google file)")
    return missing
