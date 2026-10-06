"""
message_parser.py
-----------------
Reads a plain-language message like:

    "Meeting with Rahul Sharma, rahul@gmail.com, tomorrow 4pm, 1 hour"

and pulls out the pieces we need, WITHOUT using any AI:

  - email    : found with a regex, and checked that it looks valid
  - date     : found with small regexes ("tomorrow", "next Friday", "20 Oct"...)
  - time     : found with small regexes ("4pm", "10:30am", "at 11", "noon"...)
  - duration : found with a regex ("1 hour", "30 min"); defaults to your setting
  - name     : whatever sensible words are left over

The date and time are handled SEPARATELY (not as one phrase) because that is far
more reliable. We lean on the `dateparser` library only for the day part, which it
handles well. Bare hours like "at 11" use a business-hours rule (see _bare_hour).

Because this is rule-based (not AI), very unusual phrasings may not parse. When a
piece is missing, the bot simply asks you for that one piece.
"""

import re
import logging
from dataclasses import dataclass
from datetime import datetime, timedelta, date as date_type
from typing import Optional, Tuple
from zoneinfo import ZoneInfo

import dateparser

import config

logger = logging.getLogger(__name__)

TZ = ZoneInfo(config.TIMEZONE)


# ----------------------------------------------------------------------
# Patterns
# ----------------------------------------------------------------------

EMAIL_RE = re.compile(r"[A-Za-z0-9._%+\-]+@[A-Za-z0-9.\-]+\.[A-Za-z]{2,}")

DURATION_RE = re.compile(
    r"(\d+(?:\.\d+)?)\s*(hours?|hrs?|h|minutes?|mins?|m)\b",
    re.IGNORECASE,
)

# --- Date machinery ----------------------------------------------------
# All date maths is done from a "now" we pass in, so it always uses the real
# current date (and so tests can fake "now").

_MONTHS = "jan|feb|mar|apr|may|jun|jul|aug|sep|sept|oct|nov|dec"

# Weekday name -> Python weekday index (Monday = 0). Longer names listed first so
# the regex prefers "tuesday" over "tue".
WEEKDAY_IDX = {
    "monday": 0, "mon": 0,
    "tuesday": 1, "tues": 1, "tue": 1,
    "wednesday": 2, "wed": 2,
    "thursday": 3, "thurs": 3, "thur": 3, "thu": 3,
    "friday": 4, "fri": 4,
    "saturday": 5, "sat": 5,
    "sunday": 6, "sun": 6,
}
_WD = r"(monday|mon|tuesday|tues|tue|wednesday|wed|thursday|thurs|thur|thu|friday|fri|saturday|sat|sunday|sun)"

# Number words we understand in phrases like "in two days" / "in a week".
NUM_WORDS = {
    "a": 1, "an": 1, "one": 1, "two": 2, "three": 3, "four": 4, "five": 5,
    "six": 6, "seven": 7, "eight": 8, "nine": 9, "ten": 10, "eleven": 11, "twelve": 12,
}
_NUM = r"(\d+|a|an|one|two|three|four|five|six|seven|eight|nine|ten|eleven|twelve)"

# "in N days", "after N weeks", OR "N days from now", "N weeks later" ...
REL_OFFSET_RE = re.compile(
    r"\b(?:in|after)\s+" + _NUM + r"\s+(days?|weeks?)\b"
    r"|\b" + _NUM + r"\s+(days?|weeks?)\s+(?:from\s+now|from\s+today|later|ahead)\b",
    re.IGNORECASE,
)

# "next week Tuesday"  and  "Tuesday next week"
NEXT_WEEK_RE = re.compile(r"\bnext\s+week\s+" + _WD + r"\b", re.IGNORECASE)
NEXT_WEEK_RE2 = re.compile(r"\b" + _WD + r"\s+next\s+week\b", re.IGNORECASE)

# "today"/"tomorrow"/"day after tomorrow" -> a fixed number of days from now.
DAYWORD_HANDLERS = [
    (re.compile(r"\bday\s+after\s+tomorrow\b", re.IGNORECASE), 2),
    (re.compile(r"\b(?:today|tonight)\b", re.IGNORECASE), 0),
    (re.compile(r"\b(?:tomorrow|tmrw|tmr)\b", re.IGNORECASE), 1),
]

# Plain weekday, optionally with next/this/coming (all mean the upcoming one).
WEEKDAY_RE = re.compile(r"\b(?:next|this|coming)?\s*" + _WD + r"\b", re.IGNORECASE)

# EXPLICIT calendar dates ("2 October", "Oct 2", "20/10") -> keep the stated date
# in the CURRENT year. We never silently jump these to a future year; if the result
# is in the past, the bot tells you and asks for a new date.
EXPLICIT_DATE_PATTERNS = [
    re.compile(r"\b\d{4}-\d{2}-\d{2}\b"),                                  # 2026-10-20
    re.compile(r"\b\d{1,2}[/-]\d{1,2}(?:[/-]\d{2,4})?\b"),                 # 20/10 or 20-10-2026
    re.compile(r"\b(?:" + _MONTHS + r")[a-z]*\.?\s+\d{1,2}(?:st|nd|rd|th)?\b", re.IGNORECASE),  # Oct 20 / October 2
    re.compile(r"\b\d{1,2}(?:st|nd|rd|th)?\s+(?:" + _MONTHS + r")[a-z]*\b", re.IGNORECASE),     # 20 Oct / 2nd October
]

# --- Time patterns ---
# A single clock time, e.g. "4pm", "3:30 pm". Used as a building block below.
_ONE_TIME = r"(\d{1,2})(?::(\d{2}))?\s*([ap]\.?\s*m\.?)?"

# A time RANGE, e.g. "3pm to 6pm", "3-6pm", "3 to 6 pm", "from 3pm till 6pm".
TIME_RANGE_RE = re.compile(
    r"(?:from\s+)?" + _ONE_TIME +
    r"\s*(?:to|till|until|thru|through|\-|–|—)\s*" + _ONE_TIME,
    re.IGNORECASE,
)

TIME_WORD_RE = re.compile(r"\b(noon|midday|midnight)\b", re.IGNORECASE)
TIME_AMPM_RE = re.compile(r"\b(\d{1,2})(?::(\d{2}))?\s*([ap])\.?\s*m\.?\b", re.IGNORECASE)  # 4pm, 3:30 pm
TIME_COLON_RE = re.compile(r"\b(\d{1,2}):(\d{2})\b")                                        # 15:30, 9:30
TIME_AT_RE = re.compile(r"\bat\s+(\d{1,2})(?::(\d{2}))?\b", re.IGNORECASE)                  # at 11
TIME_BARE_RE = re.compile(r"\b(\d{1,2})(?::(\d{2}))?\b")                                    # lone 11 (last resort)

# Words that are never part of a customer's name.
NAME_FILLERS = {
    # intent / filler phrases
    "i", "need", "want", "would", "like", "to", "schedule", "scheduled", "set",
    "up", "setup", "arrange", "book", "booking", "please", "can", "you", "the",
    "a", "an", "my", "our", "customer", "named", "and",
    # meeting words
    "meeting", "meet", "appointment", "call", "with", "for",
    # date / time connector words
    "on", "at", "from", "now", "today", "tonight", "tomorrow", "in", "after",
    "later", "ahead", "day", "days", "week", "weeks", "next", "this", "coming",
    "am", "pm", "till", "until", "thru", "through", "noon", "midday", "midnight",
    # meeting-type words
    "lunch", "coffee", "dinner", "breakfast", "sync", "demo", "intro", "catchup",
}


# ----------------------------------------------------------------------
# The result object
# ----------------------------------------------------------------------

@dataclass
class ParsedMeeting:
    name: Optional[str] = None
    email: Optional[str] = None
    the_date: Optional[date_type] = None
    the_time: Optional[Tuple[int, int]] = None       # start (hour, minute) in 24h
    the_end_time: Optional[Tuple[int, int]] = None    # end of a time range, if given
    end_before_start: bool = False                    # True if a range came out reversed
    duration_minutes: int = config.DEFAULT_MEETING_MINUTES
    duration_explicit: bool = False                   # True if YOU stated a duration
    duration_from_range: bool = False                 # True if duration came from a range
    now_ref: Optional[datetime] = None                # the "now" this was parsed against

    @property
    def start(self) -> Optional[datetime]:
        """Combine date + time into a timezone-aware datetime, or None."""
        if self.the_time is None:
            return None
        base_now = self.now_ref or datetime.now(TZ)
        hour, minute = self.the_time
        day = self.the_date or base_now.date()
        when = datetime(day.year, day.month, day.day, hour, minute, tzinfo=TZ)
        # If no explicit day was given and that time already passed today, use tomorrow.
        if self.the_date is None and when < base_now:
            when += timedelta(days=1)
        return when

    def missing(self):
        gaps = []
        if not self.email:
            gaps.append("email")
        if self.the_time is None:
            gaps.append("time")
        if not self.name:
            gaps.append("name")
        return gaps

    def is_complete(self):
        return not self.missing()


# ----------------------------------------------------------------------
# Small helpers
# ----------------------------------------------------------------------

def _looks_like_valid_email(address):
    return bool(re.fullmatch(r"[A-Za-z0-9._%+\-]+@[A-Za-z0-9.\-]+\.[A-Za-z]{2,}", address))


def _duration_to_minutes(number_text, unit_text):
    number = float(number_text)
    return max(1, round(number * 60)) if unit_text.lower().startswith("h") else max(1, round(number))


def _ampm_hour(hour, ampm):
    ampm = ampm.lower()
    if ampm == "a":
        return 0 if hour == 12 else hour
    return 12 if hour == 12 else hour + 12


def _bare_hour(hour):
    """
    Turn an am/pm-less hour into a 24h hour using a business-hours rule:
      8-11  -> morning (as-is)      e.g. 'at 11' -> 11:00
      12    -> noon
      1-7   -> afternoon/evening    e.g. 'at 4'  -> 16:00
      13-23 -> already 24-hour
    """
    if hour == 0 or hour == 12:
        return 12 if hour == 12 else 0
    if 1 <= hour <= 7:
        return hour + 12
    if 8 <= hour <= 23:
        return hour
    return None


def _cut(text, match):
    """Remove a matched span from text, leaving a space in its place."""
    return text[:match.start()] + " " + text[match.end():]


def _resolve_time(hour_text, minute_text, ampm_token):
    """Turn regex pieces into a (hour24, minute) tuple, or None."""
    hour = int(hour_text)
    minute = int(minute_text) if minute_text else 0
    if ampm_token:
        hour = _ampm_hour(hour, ampm_token.strip().lower()[0])
    else:
        bare = _bare_hour(hour)
        if bare is None:
            return None
        hour = bare
    return (hour, minute)


def _name_like(token):
    """A plausible name word: letters (plus . - ') and not a filler word."""
    if token.lower() in NAME_FILLERS:
        return False
    return bool(re.fullmatch(r"[A-Za-z][A-Za-z.\-']*", token))


def _extract_name(leftover):
    """
    Work out the customer's name from the leftover text (after the date, time,
    range, duration and email have been removed).

    - If the text contains "with", take the words right after it.
    - Otherwise use the whole leftover.
    Then keep up to 3 consecutive name-like words, skipping filler words.
    Returns None if nothing sensible remains (so the bot will ask).
    """
    match = re.search(r"\bwith\b", leftover, re.IGNORECASE)
    candidate = leftover[match.end():] if match else leftover

    tokens = [t for t in re.split(r"[^A-Za-z.\-']+", candidate) if t]

    collected = []
    for token in tokens:
        if _name_like(token):
            collected.append(token)
            if len(collected) >= 3:
                break
        elif collected:
            break  # stop at the first filler once we've started a name
        # else: skip leading filler words and keep looking

    name = " ".join(collected).strip(" -")
    return name or None


# ----------------------------------------------------------------------
# Extractors
# ----------------------------------------------------------------------

def _extract_range(text):
    """
    Find a time range like "3pm to 6pm".
    Returns (text_without_it, start_hm, end_hm, end_before_start).
    If no range is found, start_hm and end_hm are None.
    """
    m = TIME_RANGE_RE.search(text)
    if not m:
        return text, None, None, False

    s_h, s_m, s_ap, e_h, e_m, e_ap = m.groups()
    # If only one side states am/pm, apply it to both (e.g. "3-6pm" = 3pm to 6pm).
    start = _resolve_time(s_h, s_m, s_ap or e_ap)
    end = _resolve_time(e_h, e_m, e_ap or s_ap)
    if start is None or end is None:
        return text, None, None, False

    reversed_range = (end[0] * 60 + end[1]) <= (start[0] * 60 + start[1])
    return _cut(text, m), start, end, reversed_range


def _num_value(num_text):
    """Turn '2' or 'two' or 'a' into an integer, or None."""
    if num_text.isdigit():
        return int(num_text)
    return NUM_WORDS.get(num_text.lower())


def _extract_date(text, now):
    """
    Find the date in `text`, using `now` (a timezone-aware datetime) as the
    reference point. Returns (text_without_it, date_or_None).

    Handlers are tried in order; the first that matches wins. Everything is
    computed by simple arithmetic from `now` so the result is always correct
    and testable - we don't let the parser "guess" relative dates.
    """
    today = now.date()

    # A) Offsets: "in 2 days", "after 3 days", "2 days from now", "in a week", "in 2 weeks"
    m = REL_OFFSET_RE.search(text)
    if m:
        num = m.group(1) or m.group(3)
        unit = m.group(2) or m.group(4)
        n = _num_value(num)
        if n is not None:
            days = n * 7 if unit.lower().startswith("week") else n
            return _cut(text, m), today + timedelta(days=days)

    # B) "next week <weekday>" / "<weekday> next week" -> that weekday in next week
    for pattern in (NEXT_WEEK_RE, NEXT_WEEK_RE2):
        m = pattern.search(text)
        if m:
            idx = WEEKDAY_IDX[m.group(1).lower()]
            next_monday = today + timedelta(days=(7 - now.weekday()))
            return _cut(text, m), next_monday + timedelta(days=idx)

    # C) "today" / "tomorrow" / "day after tomorrow"
    for pattern, offset in DAYWORD_HANDLERS:
        m = pattern.search(text)
        if m:
            return _cut(text, m), today + timedelta(days=offset)

    # D) A weekday (bare, or with next/this/coming) -> the next upcoming one
    m = WEEKDAY_RE.search(text)
    if m:
        idx = WEEKDAY_IDX[m.group(1).lower()]
        delta = (idx - now.weekday()) % 7   # 0 = today, else days until that weekday
        return _cut(text, m), today + timedelta(days=delta)

    # E) Explicit calendar dates ("2 October", "Oct 2", "20/10", "2026-11-05")
    best = None
    for pattern in EXPLICIT_DATE_PATTERNS:
        mm = pattern.search(text)
        if mm and (best is None or mm.start() < best.start()):
            best = mm
    if best:
        phrase = best.group(0)
        cleaned = re.sub(r"\b(next|this|coming|at|on)\b", " ", phrase, flags=re.IGNORECASE).strip()
        parsed = dateparser.parse(
            cleaned,
            languages=["en"],
            settings={
                "TIMEZONE": config.TIMEZONE,
                "TO_TIMEZONE": config.TIMEZONE,
                "RETURN_AS_TIMEZONE_AWARE": True,
                "RELATIVE_BASE": now.replace(tzinfo=None),  # keep the stated day in the current year
            },
        )
        if parsed:
            return _cut(text, best), parsed.date()

    return text, None


def _extract_time(text):
    """Find a time, return (text_without_it, (hour, minute)_or_None)."""
    m = TIME_WORD_RE.search(text)
    if m:
        word = m.group(1).lower()
        hm = (0, 0) if word == "midnight" else (12, 0)
        return _cut(text, m), hm

    m = TIME_AMPM_RE.search(text)
    if m:
        hour = _ampm_hour(int(m.group(1)), m.group(3))
        minute = int(m.group(2)) if m.group(2) else 0
        return _cut(text, m), (hour, minute)

    m = TIME_COLON_RE.search(text)
    if m:
        return _cut(text, m), (int(m.group(1)) % 24, int(m.group(2)))

    m = TIME_AT_RE.search(text)
    if m:
        hour = _bare_hour(int(m.group(1)))
        if hour is not None:
            minute = int(m.group(2)) if m.group(2) else 0
            return _cut(text, m), (hour, minute)

    m = TIME_BARE_RE.search(text)
    if m:
        hour = _bare_hour(int(m.group(1)))
        if hour is not None:
            minute = int(m.group(2)) if m.group(2) else 0
            return _cut(text, m), (hour, minute)

    return text, None


# ----------------------------------------------------------------------
# Main
# ----------------------------------------------------------------------

def parse_message(text, now=None):
    """
    Parse `text` into a ParsedMeeting.

    `now` is the reference "current time" (timezone-aware). If not given, we read
    the real system clock in the Asia/Kolkata timezone, freshly, every call.
    Tests pass a fixed `now` so results can be checked.
    """
    if now is None:
        now = datetime.now(TZ)

    result = ParsedMeeting(now_ref=now)
    working = text.strip()

    # 1) Email
    m = EMAIL_RE.search(working)
    if m:
        if _looks_like_valid_email(m.group(0)):
            result.email = m.group(0)
        working = _cut(working, m)

    # 2) Explicit duration (remove before times, so "1 hour" isn't read as a time)
    m = DURATION_RE.search(working)
    if m:
        result.duration_minutes = _duration_to_minutes(m.group(1), m.group(2))
        result.duration_explicit = True
        working = _cut(working, m)

    # 3) Time RANGE (before date, so "3-6pm" isn't mistaken for a date like 3/6)
    working, start, end, reversed_range = _extract_range(working)
    if start is not None:
        result.the_time = start
        result.the_end_time = end
        result.end_before_start = reversed_range
        # Duration from the range, unless you gave an explicit duration (that wins).
        if not reversed_range and not result.duration_explicit:
            minutes = (end[0] * 60 + end[1]) - (start[0] * 60 + start[1])
            result.duration_minutes = minutes
            result.duration_from_range = True

    # 4) Date (remove before single time, so a date number isn't read as a time)
    working, result.the_date = _extract_date(working, now)

    # 5) Single time, only if we didn't already get one from a range
    if result.the_time is None:
        working, result.the_time = _extract_time(working)

    # 6) Name = whatever sensible words remain
    result.name = _extract_name(working)

    logger.info(
        "Parsed -> name=%r email=%r date=%s time=%s end=%s rev=%s dur=%smin(explicit=%s,range=%s)",
        result.name, result.email, result.the_date, result.the_time, result.the_end_time,
        result.end_before_start, result.duration_minutes, result.duration_explicit,
        result.duration_from_range,
    )
    return result


def merge(draft, new):
    """Fill gaps in `draft` from a newer parse (used when you reply with one piece)."""
    draft.now_ref = new.now_ref  # always use the freshest "now"
    if not draft.email and new.email:
        draft.email = new.email
    if not draft.name and new.name:
        draft.name = new.name
    if draft.the_date is None and new.the_date is not None:
        draft.the_date = new.the_date
    if draft.the_time is None and new.the_time is not None:
        draft.the_time = new.the_time
        draft.the_end_time = new.the_end_time
        draft.end_before_start = new.end_before_start
        if new.duration_from_range and not draft.duration_explicit:
            draft.duration_minutes = new.duration_minutes
            draft.duration_from_range = True
    if new.duration_explicit and not draft.duration_explicit:
        draft.duration_minutes = new.duration_minutes
        draft.duration_explicit = True
    return draft


def format_duration(minutes):
    hours, mins = divmod(minutes, 60)
    parts = []
    if hours:
        parts.append(f"{hours} hour" + ("s" if hours != 1 else ""))
    if mins:
        parts.append(f"{mins} min")
    return " ".join(parts) if parts else f"{minutes} min"
