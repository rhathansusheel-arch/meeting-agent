"""
test_calendar.py
----------------
A one-time test for Stage 2.

Run it with:  python test_calendar.py

What it does:
  1. Logs you in to Google (a browser window opens the first time).
  2. Creates a TEST meeting on your calendar, 1 hour from now, with a Meet link.
  3. Prints the event link and the Meet link.

If you see those links and the event shows up in Google Calendar, Stage 2 works.
You can safely delete the test event afterwards.
"""

import logging
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

import config
from calendar_service import (
    CalendarError,
    create_meeting_event,
    extract_meet_link,
    get_calendar_service,
)

# Show friendly progress messages while it runs.
logging.basicConfig(level=logging.INFO, format="%(asctime)s  %(message)s", datefmt="%H:%M:%S")


def main():
    print("\n--- Meeting Assistant: Google Calendar test ---\n")

    # Step 1: log in.
    print("Step 1/2: Connecting to Google (a browser window may open)...")
    try:
        service = get_calendar_service()
    except CalendarError as error:
        print(f"\n[X] Login problem: {error}\n")
        return
    print("        Connected.\n")

    # Step 2: build a test event 1 hour from now.
    timezone = ZoneInfo(config.TIMEZONE)
    start = (datetime.now(timezone) + timedelta(hours=1)).replace(second=0, microsecond=0)
    end = start + timedelta(minutes=config.DEFAULT_MEETING_MINUTES)

    nice_time = start.strftime("%A, %d %B %Y at %I:%M %p")
    print(f"Step 2/2: Creating a test event for {nice_time} ({config.TIMEZONE})...")

    try:
        event = create_meeting_event(
            service,
            summary="TEST - Meeting Assistant setup",
            start_dt=start,
            end_dt=end,
            description="This is a test event created while setting up your "
                        "Meeting Assistant. It is safe to delete.",
            # No attendee on the test, so no one gets an email.
        )
    except CalendarError as error:
        print(f"\n[X] Could not create the event: {error}\n")
        return

    meet_link = extract_meet_link(event)

    print("\n[OK] Success! Your test event was created.\n")
    print(f"   When:       {nice_time}")
    print(f"   In calendar: {event.get('htmlLink', '(no link returned)')}")
    print(f"   Meet link:   {meet_link or '(no Meet link found - tell your assistant)'}")
    print("\nOpen Google Calendar to see it. You can delete the test event now.\n")


if __name__ == "__main__":
    main()
