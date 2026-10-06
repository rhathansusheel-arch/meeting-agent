"""
gemini_parser.py
----------------
Understands your message using Google's FREE Gemini API, and returns the same
ParsedMeeting object the rest of the bot already uses.

Design points:
  - We send ONLY your message text (plus the current date/time for context).
    We never send tokens, credentials, or any calendar data.
  - We ask Gemini for strict JSON (structured output) so the reply is always
    parseable.
  - Python - not Gemini - does the real verification (email format, real date,
    past date, duration maths). Gemini only extracts; we trust nothing blindly.
  - If ANYTHING goes wrong (no key, no internet, rate limit, timeout, bad JSON),
    we raise GeminiError so the bot can fall back to the rule-based parser.
"""

import logging
import re
from datetime import datetime
from typing import List, Optional

from pydantic import BaseModel

import config
from message_parser import TZ, ParsedMeeting, _looks_like_valid_email

logger = logging.getLogger(__name__)

# The SDK logs a noisy (harmless) warning about automatic function calling; quiet it.
logging.getLogger("google_genai").setLevel(logging.ERROR)

# Placeholder "names" that are not real customer names.
_NON_NAMES = {"someone", "somebody", "a client", "the client", "a customer",
              "the customer", "a person", "client", "customer"}


class GeminiError(Exception):
    """Raised when Gemini can't be used, so the bot falls back to basic parsing."""


# The exact JSON shape we ask Gemini to return.
class MeetingExtraction(BaseModel):
    customer_name: Optional[str] = None
    customer_email: Optional[str] = None
    date: Optional[str] = None          # YYYY-MM-DD
    start_time: Optional[str] = None    # HH:MM (24h)
    end_time: Optional[str] = None      # HH:MM (24h) or null
    duration_minutes: Optional[int] = None
    missing_fields: List[str] = []


# We build the client once and reuse it.
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
            http_options={"timeout": config.GEMINI_TIMEOUT_SECONDS * 1000},  # milliseconds
        )
    return _client


def _build_prompt(text, now):
    now_str = now.strftime("%A, %d %B %Y, %H:%M") + " IST"
    return f"""You extract meeting-booking details from a message written by the owner of the meeting (a businessperson booking a customer).

Right now it is {now_str}. Use this as the reference point for every relative date.

Return the structured fields only. Follow these rules exactly:
- Never invent information. If a field is not clearly present, set it to null and add its name to missing_fields.
- customer_name: the OTHER person's real name only (e.g. "Rahul Sharma"). Never include words like "meeting", "schedule", "call", "book", "with". If there is no real name, or it is a placeholder like "someone" / "a client" / "the customer", set it to null.
- customer_email: only if a valid email address is present; otherwise null.
- date: resolve to an absolute date in YYYY-MM-DD using the reference time above.
    * "today" = the reference date; "tomorrow" = +1 day; "day after tomorrow" = +2 days.
    * "in N days" / "N days from now" = reference date + N days.
    * "this <weekday>" or a bare weekday = the soonest upcoming occurrence of that weekday.
    * "next <weekday>" = that weekday in NEXT week (7 days later than the soonest upcoming one).
    * Explicit dates like "20th October" use the current year unless a year is given.
- start_time: 24-hour "HH:MM". For a bare hour with no am/pm, assume business hours: 8-11 => morning, 12 => noon, 1-7 => afternoon/evening (e.g. "at 4" = 16:00).
- end_time: "HH:MM" if a time range is given (e.g. "3pm to 6pm", "2-3:30pm"); otherwise null.
- duration_minutes: an integer ONLY if a duration is explicitly stated (e.g. "1 hour" = 60, "45 minutes" = 45); otherwise null.
- missing_fields: include any of ["customer_name","customer_email","date","start_time"] you could not fill. Do NOT list end_time or duration_minutes.

Message:
\"\"\"{text}\"\"\"
"""


def _to_minutes(hhmm):
    h, m = hhmm
    return h * 60 + m


def _parse_hhmm(value):
    """Turn 'HH:MM' into (hour, minute), or None."""
    if not value or not isinstance(value, str):
        return None
    match = re.fullmatch(r"(\d{1,2}):(\d{2})", value.strip())
    if not match:
        return None
    hour, minute = int(match.group(1)), int(match.group(2))
    if 0 <= hour <= 23 and 0 <= minute <= 59:
        return (hour, minute)
    return None


def _to_parsed_meeting(data: MeetingExtraction, now) -> ParsedMeeting:
    """Convert Gemini's JSON into the bot's ParsedMeeting, verifying in Python."""
    result = ParsedMeeting(now_ref=now)

    # Name (reject placeholders).
    name = (data.customer_name or "").strip()
    if name and name.lower() not in _NON_NAMES:
        result.name = name

    # Email (verify the format ourselves).
    email = (data.customer_email or "").strip()
    if email and _looks_like_valid_email(email):
        result.email = email

    # Date (must be a real YYYY-MM-DD).
    if data.date:
        try:
            result.the_date = datetime.strptime(data.date.strip(), "%Y-%m-%d").date()
        except (ValueError, TypeError):
            result.the_date = None

    # Times.
    result.the_time = _parse_hhmm(data.start_time)
    result.the_end_time = _parse_hhmm(data.end_time)

    # Duration: explicit wins; else from range; else default.
    if isinstance(data.duration_minutes, int) and data.duration_minutes > 0:
        result.duration_minutes = data.duration_minutes
        result.duration_explicit = True
    elif result.the_time and result.the_end_time:
        diff = _to_minutes(result.the_end_time) - _to_minutes(result.the_time)
        if diff > 0:
            result.duration_minutes = diff
            result.duration_from_range = True
        else:
            result.end_before_start = True
    # else: leave the config default.

    return result


def parse_with_gemini(text, now=None):
    """
    Parse `text` with Gemini and return a ParsedMeeting.
    Raises GeminiError on any failure (so the caller can fall back).
    """
    if now is None:
        now = datetime.now(TZ)

    client = _get_client()

    try:
        from google.genai import types
        response = client.models.generate_content(
            model=config.GEMINI_MODEL,
            contents=_build_prompt(text, now),
            config=types.GenerateContentConfig(
                response_mime_type="application/json",
                response_schema=MeetingExtraction,
                temperature=0,
            ),
        )
    except Exception as error:
        # Network, rate limit (429), model unavailable (503), bad key, etc.
        raise GeminiError(f"{type(error).__name__}: {str(error)[:120]}")

    # Prefer the SDK's parsed object; fall back to parsing the raw text.
    data = getattr(response, "parsed", None)
    if data is None:
        import json
        try:
            data = MeetingExtraction(**json.loads(response.text))
        except Exception as error:
            raise GeminiError(f"Gemini returned invalid JSON: {str(error)[:120]}")

    return _to_parsed_meeting(data, now)
