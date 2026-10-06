"""
calendar_service.py
--------------------
Everything that talks to Google Calendar lives here.

Two main jobs:
  1. Log in to your Google account (once), and remember you afterwards.
  2. Create a calendar event that has a Google Meet link.

The first time you run anything that uses this file, a browser window opens
asking you to approve access. After that, a file called token.json remembers
you, so you won't be asked again.
"""

import logging
import uuid
from datetime import datetime, timezone

from google.auth.transport.requests import Request
from google.oauth2.credentials import Credentials
from google_auth_oauthlib.flow import InstalledAppFlow
from googleapiclient.discovery import build
from googleapiclient.errors import HttpError

import config

logger = logging.getLogger(__name__)


class CalendarError(Exception):
    """A friendly, human-readable problem we can show instead of a raw crash."""


# ----------------------------------------------------------------------
# Logging in to Google
# ----------------------------------------------------------------------

def get_calendar_service():
    """
    Return a ready-to-use Google Calendar connection.

    Handles three cases automatically:
      - You've logged in before and the saved token is still good -> use it.
      - The token expired but can be refreshed -> refresh it quietly.
      - No token, or it can't be refreshed -> open a browser to log in.
    """
    creds = None

    # Do we already have a saved login?
    if config.GOOGLE_TOKEN_FILE.exists():
        try:
            creds = Credentials.from_authorized_user_file(
                str(config.GOOGLE_TOKEN_FILE), config.GOOGLE_SCOPES
            )
        except Exception as error:
            logger.warning("Saved token was unreadable (%s); will log in again.", error)
            creds = None

    # If we have no valid login, get one.
    if not creds or not creds.valid:
        if creds and creds.expired and creds.refresh_token:
            logger.info("Google login expired; refreshing quietly...")
            try:
                creds.refresh(Request())
            except Exception as error:
                logger.warning("Could not refresh token (%s); starting a fresh login.", error)
                creds = None

        if not creds or not creds.valid:
            if not config.GOOGLE_CREDENTIALS_FILE.exists():
                raise CalendarError(
                    "I couldn't find credentials.json in the project folder. "
                    "Please re-download it from Google Cloud and put it here."
                )
            logger.info("Opening a browser window for you to log in to Google...")
            try:
                flow = InstalledAppFlow.from_client_secrets_file(
                    str(config.GOOGLE_CREDENTIALS_FILE), config.GOOGLE_SCOPES
                )
                creds = flow.run_local_server(port=0)
            except Exception as error:
                raise CalendarError(
                    f"The Google login didn't complete. Please try again. (Details: {error})"
                )

        # Save the login so we don't have to ask again next time.
        try:
            config.GOOGLE_TOKEN_FILE.write_text(creds.to_json(), encoding="utf-8")
            logger.info("Saved your Google login to %s", config.GOOGLE_TOKEN_FILE.name)
        except Exception as error:
            logger.warning("Logged in, but couldn't save the token file (%s).", error)

    try:
        # cache_discovery=False avoids a harmless but noisy warning in the logs.
        return build("calendar", "v3", credentials=creds, cache_discovery=False)
    except Exception as error:
        raise CalendarError(f"Couldn't connect to Google Calendar: {error}")


# ----------------------------------------------------------------------
# Checking for conflicts (free/busy)
# ----------------------------------------------------------------------

def check_busy(service, start_dt, end_dt):
    """
    Return a list of busy time-slots on your calendar that overlap
    [start_dt, end_dt]. An empty list means you are free.
    Each item looks like {"start": "...", "end": "..."} (RFC3339 strings).
    """
    body = {
        "timeMin": start_dt.isoformat(),
        "timeMax": end_dt.isoformat(),
        "timeZone": config.TIMEZONE,
        "items": [{"id": config.CALENDAR_ID}],
    }
    try:
        response = service.freebusy().query(body=body).execute()
    except HttpError as error:
        status = getattr(error.resp, "status", "?")
        raise CalendarError(f"Google Calendar wouldn't answer the availability check (error {status}).")
    except Exception as error:
        raise CalendarError(f"Couldn't check your calendar for clashes: {error}")

    calendar = response.get("calendars", {}).get(config.CALENDAR_ID, {})
    return calendar.get("busy", [])


def get_events_between(service, start_dt, end_dt):
    """
    Return the real events overlapping [start_dt, end_dt], each as
    {"summary": title, "start": "...", "end": "..."}. Events marked "free"
    (transparent) or cancelled are ignored. Empty list means you are free.
    """
    try:
        response = (
            service.events()
            .list(
                calendarId=config.CALENDAR_ID,
                timeMin=start_dt.isoformat(),
                timeMax=end_dt.isoformat(),
                singleEvents=True,
                orderBy="startTime",
            )
            .execute()
        )
    except HttpError as error:
        status = getattr(error.resp, "status", "?")
        raise CalendarError(f"Google Calendar wouldn't answer the availability check (error {status}).")
    except Exception as error:
        raise CalendarError(f"Couldn't check your calendar for clashes: {error}")

    clashes = []
    for event in response.get("items", []):
        if event.get("status") == "cancelled":
            continue
        if event.get("transparency") == "transparent":  # marked "Free", not "Busy"
            continue
        start = event.get("start", {})
        end = event.get("end", {})
        clashes.append(
            {
                "summary": event.get("summary", "(no title)"),
                "start": start.get("dateTime") or start.get("date"),
                "end": end.get("dateTime") or end.get("date"),
            }
        )
    return clashes


def list_upcoming(service, max_results=10):
    """Return your next upcoming events (from now), soonest first."""
    now_iso = datetime.now(timezone.utc).isoformat()
    try:
        response = (
            service.events()
            .list(
                calendarId=config.CALENDAR_ID,
                timeMin=now_iso,
                maxResults=max_results,
                singleEvents=True,
                orderBy="startTime",
            )
            .execute()
        )
    except HttpError as error:
        status = getattr(error.resp, "status", "?")
        raise CalendarError(f"Google Calendar wouldn't list your events (error {status}).")
    except Exception as error:
        raise CalendarError(f"Couldn't fetch your upcoming meetings: {error}")

    events = []
    for event in response.get("items", []):
        if event.get("status") == "cancelled":
            continue
        start = event.get("start", {})
        events.append(
            {
                "summary": event.get("summary", "(no title)"),
                "start": start.get("dateTime") or start.get("date"),
                "meet": event.get("hangoutLink"),
            }
        )
    return events


# ----------------------------------------------------------------------
# Creating an event with a Google Meet link
# ----------------------------------------------------------------------

def extract_meet_link(event):
    """Pull the Google Meet video link out of a created event, or return None."""
    link = event.get("hangoutLink")
    if link:
        return link
    for entry in event.get("conferenceData", {}).get("entryPoints", []):
        if entry.get("entryPointType") == "video":
            return entry.get("uri")
    return None


def create_meeting_event(
    service,
    summary,
    start_dt,
    end_dt,
    description="",
    attendee_email=None,
    send_updates="all",
):
    """
    Create a calendar event that includes a Google Meet link.

    - start_dt / end_dt: timezone-aware datetime objects.
    - attendee_email: if given, this person is added and Google emails them
      the invite (controlled by send_updates: "all" emails them, "none" is silent).
    Returns the created event (a dictionary from Google).
    """
    event_body = {
        "summary": summary,
        "description": description,
        "start": {"dateTime": start_dt.isoformat(), "timeZone": config.TIMEZONE},
        "end": {"dateTime": end_dt.isoformat(), "timeZone": config.TIMEZONE},
        # This asks Google to generate a fresh Meet link for the event.
        "conferenceData": {
            "createRequest": {
                "requestId": str(uuid.uuid4()),  # must be unique per request
                "conferenceSolutionKey": {"type": "hangoutsMeet"},
            }
        },
    }

    if attendee_email:
        event_body["attendees"] = [{"email": attendee_email}]

    try:
        created = (
            service.events()
            .insert(
                calendarId=config.CALENDAR_ID,
                body=event_body,
                conferenceDataVersion=1,  # REQUIRED for the Meet link to be created
                sendUpdates=send_updates if attendee_email else "none",
            )
            .execute()
        )
    except HttpError as error:
        status = getattr(error.resp, "status", "?")
        raise CalendarError(
            f"Google Calendar refused to create the event (error {status}). "
            "Please check the details and try again."
        )
    except Exception as error:
        raise CalendarError(f"Something went wrong creating the event: {error}")

    return created
