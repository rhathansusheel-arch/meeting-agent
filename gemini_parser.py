"""
gemini_parser.py
----------------
Conversational understanding using Google's FREE Gemini API.

Instead of blindly extracting a booking from every message, this asks Gemini to
first decide WHAT the owner wants (intent), and only then extract details. It is
given the current draft and recent history so it can hold a real conversation.

Privacy: we send ONLY the owner's message text, the current draft, the recent
chat history, and the current date/time. Never tokens, keys, or calendar data.

Python (the bot) still verifies everything Gemini returns - Gemini never has the
final say, and it never books anything. If Gemini fails for any reason we raise
GeminiError so the bot can fall back to the rule-based parser.
"""

import json
import logging
import re
from datetime import datetime
from typing import List, Optional

from pydantic import BaseModel

import config
from message_parser import TZ, _looks_like_valid_email

logger = logging.getLogger(__name__)
logging.getLogger("google_genai").setLevel(logging.ERROR)  # quiet a harmless SDK warning

INTENTS = {
    "greeting", "chitchat", "new_booking", "update_draft",
    "confirm", "cancel", "calendar_question", "unclear",
}

_NON_NAMES = {"someone", "somebody", "a client", "the client", "a customer",
              "the customer", "a person", "client", "customer", "him", "her", "them"}


class GeminiError(Exception):
    """Raised when Gemini can't be used, so the bot falls back to basic parsing."""


class Turn(BaseModel):
    """What Gemini returns for one message."""
    intent: str = "unclear"
    customer_name: Optional[str] = None
    customer_email: Optional[str] = None
    date: Optional[str] = None          # YYYY-MM-DD
    start_time: Optional[str] = None    # HH:MM (24h)
    end_time: Optional[str] = None      # HH:MM (24h) or null
    duration_minutes: Optional[int] = None
    missing_fields: List[str] = []
    reply: str = ""


# ---------------------------------------------------------------- client

_client = None


def _get_client():
    global _client
    if _client is None:
        if not config.GEMINI_API_KEY:
            raise GeminiError("No GEMINI_API_KEY set.")
        try:
            from google import genai
        except ImportError as error:
            raise GeminiError(f"google-genai not installed: {error}")
        _client = genai.Client(
            api_key=config.GEMINI_API_KEY,
            http_options={"timeout": config.GEMINI_TIMEOUT_SECONDS * 1000},
        )
    return _client


# ---------------------------------------------------------------- helpers
# These are also used by bot.py to verify what Gemini returns.

def valid_email(value):
    value = (value or "").strip()
    return value if value and _looks_like_valid_email(value) else None


def valid_name(value):
    value = (value or "").strip()
    if not value or value.lower() in _NON_NAMES:
        return None
    return value


def parse_date_str(value):
    """'YYYY-MM-DD' -> date, or None."""
    if not value or not isinstance(value, str):
        return None
    try:
        return datetime.strptime(value.strip(), "%Y-%m-%d").date()
    except (ValueError, TypeError):
        return None


def parse_hhmm(value):
    """'HH:MM' -> (hour, minute), or None."""
    if not value or not isinstance(value, str):
        return None
    match = re.fullmatch(r"(\d{1,2}):(\d{2})", value.strip())
    if not match:
        return None
    hour, minute = int(match.group(1)), int(match.group(2))
    return (hour, minute) if 0 <= hour <= 23 and 0 <= minute <= 59 else None


# ---------------------------------------------------------------- prompt

def _build_prompt(text, draft_dict, history, now):
    now_str = now.strftime("%A, %d %B %Y, %H:%M") + " IST"
    draft_json = json.dumps(draft_dict, ensure_ascii=False)
    if history:
        history_text = "\n".join(f"{role.title()}: {msg}" for role, msg in history[-6:])
    else:
        history_text = "(no previous messages)"

    return f"""You are a warm, concise scheduling assistant for a busy professional (the "owner").
The owner books meetings with their customers by chatting with you. Only the owner talks to you.

Right now it is {now_str}. Use this as the reference point for all relative dates.

Current draft booking (fields already known; null means not provided yet):
{draft_json}

Recent conversation (oldest first):
{history_text}

The owner's new message:
\"\"\"{text}\"\"\"

Classify the message and extract ONLY what this message adds or changes.

"intent" must be exactly one of:
- greeting        : hello / hi / good morning, etc.
- chitchat        : small talk or thanks, not about a booking.
- new_booking     : the owner is starting to arrange a meeting.
- update_draft    : the owner is adding to or correcting the current draft (gives the email, "make it 5pm", etc.).
- confirm         : the owner says yes / confirm / go ahead / book it.
- cancel          : the owner says cancel / forget it / never mind.
- calendar_question: the owner asks about their schedule ("what's on tomorrow", "am I free Friday at 3").
- unclear         : you genuinely cannot tell.

Rules:
- Fill a field ONLY if THIS message provides or changes it. If a field is unchanged, set it to null. Never echo unchanged fields.
- NEVER invent a name, email, date or time. If something needed is unknown, leave it null and list it in missing_fields.
- customer_name: the customer's real name only (never words like "meeting", "schedule", "call", "with"). Placeholders like "someone"/"a client" are NOT names (null).
- customer_email: only a valid email address; else null.
- date: an absolute YYYY-MM-DD computed from the reference time. "tomorrow"=+1 day, "day after tomorrow"=+2, "in N days"=+N. "next <weekday>" = that weekday in NEXT week; "this <weekday>" or a bare weekday = the soonest upcoming one. Explicit dates like "20th October" use the current year unless a year is stated.
- start_time: 24-hour "HH:MM". Bare hour with no am/pm: 8-11 => morning, 12 => noon, 1-7 => afternoon/evening (e.g. "at 4" = 16:00).
- end_time: "HH:MM" if a time range is given (e.g. "3pm to 6pm"); else null.
- duration_minutes: integer ONLY if explicitly stated ("1 hour"=60, "45 minutes"=45); else null.
- For a calendar_question, STILL fill date (and start_time if the owner names a time) with the day/time they are asking about - e.g. "what's on tomorrow" -> date = tomorrow; "am I free Friday at 3" -> date = that Friday, start_time = 15:00. If they ask about no specific day, leave date null (it means today).
- missing_fields: for a booking, list which of ["customer_name","customer_email","date","start_time"] are STILL unknown after combining the draft with this message. For non-booking intents use an empty list.
- reply: ONE short, friendly, natural sentence to the owner. Greet/chitchat naturally. For a booking, acknowledge warmly and, if something is missing, ask for just ONE missing item. NEVER say anything is booked or confirmed - only the owner's Confirm button books a meeting.
"""


def run_turn(text, draft_dict, history, now=None):
    """
    Run one conversational turn. Returns a Turn. Raises GeminiError on failure.
    """
    if now is None:
        now = datetime.now(TZ)
    client = _get_client()

    try:
        from google.genai import types
        response = client.models.generate_content(
            model=config.GEMINI_MODEL,
            contents=_build_prompt(text, draft_dict, history, now),
            config=types.GenerateContentConfig(
                response_mime_type="application/json",
                response_schema=Turn,
                temperature=0,
            ),
        )
    except Exception as error:
        raise GeminiError(f"{type(error).__name__}: {str(error)[:120]}")

    turn = getattr(response, "parsed", None)
    if turn is None:
        try:
            turn = Turn(**json.loads(response.text))
        except Exception as error:
            raise GeminiError(f"Gemini returned invalid JSON: {str(error)[:120]}")

    if turn.intent not in INTENTS:
        turn.intent = "unclear"
    return turn
