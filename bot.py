"""
bot.py
------
The Telegram bot (Stage 4 - the full booking flow).

What it does:
  - Replies ONLY to you (your chat ID from .env). Strangers get "This is a private bot."
  - Parses each message FRESH. Nothing carries over from a previous booking; it only
    remembers context while it is actively asking you for a missing piece.
  - Asks for any missing piece, one at a time.
  - Rejects times in the past (without silently changing the date) and asks for a new one.
  - Warns (but allows) times outside your working hours.
  - Shows you the exact date it understood, then checks Google Calendar for clashes.
  - If you're busy, it names the clashing events and offers: Book anyway / Pick another
    time / Cancel.
  - Otherwise shows a summary with Confirm / Edit / Cancel buttons.
  - On Confirm / Book anyway: creates the event with a Google Meet link, invites the
    customer by email, sends you the Meet link + details, AND a forwardable message.
  - /cancel stops the current booking at any time.

Run it with:  python bot.py       Stop it with: Ctrl + C
"""

import asyncio
import logging
from datetime import datetime, timedelta

from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.ext import (
    Application,
    CallbackQueryHandler,
    CommandHandler,
    ContextTypes,
    MessageHandler,
    filters,
)

import calendar_service
import config
import gemini_parser
from calendar_service import CalendarError
from message_parser import TZ, ParsedMeeting, format_duration, merge, parse_message

# ----------------------------------------------------------------------
# Logging
# ----------------------------------------------------------------------
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s  %(levelname)s  %(message)s",
    datefmt="%H:%M:%S",
)
logging.getLogger("httpx").setLevel(logging.WARNING)
logger = logging.getLogger("bot")


# ----------------------------------------------------------------------
# Security: only talk to the owner
# ----------------------------------------------------------------------

def is_owner(update: Update) -> bool:
    chat = update.effective_chat
    return chat is not None and chat.id in config.OWNER_CHAT_IDS


async def reject_stranger(update: Update):
    who = update.effective_chat.id if update.effective_chat else "unknown"
    logger.warning("Ignoring message from non-owner chat id %s", who)
    if update.message:
        await update.message.reply_text("This is a private bot.")
    elif update.callback_query:
        await update.callback_query.answer("This is a private bot.", show_alert=True)


# ----------------------------------------------------------------------
# Google Calendar connection (built once, then cached)
# ----------------------------------------------------------------------

async def get_service(context: ContextTypes.DEFAULT_TYPE):
    service = context.application.bot_data.get("calendar_service")
    if service is None:
        service = await asyncio.to_thread(calendar_service.get_calendar_service)
        context.application.bot_data["calendar_service"] = service
    return service


# ----------------------------------------------------------------------
# Understanding a message: try Gemini (AI) first, fall back to basic parsing
# ----------------------------------------------------------------------

async def parse_incoming(text, now):
    """
    Return (ParsedMeeting, source) where source is "gemini" or "fallback".
    Never raises: if Gemini fails for any reason, we use the rule-based parser.
    A hard timeout guarantees it can't hang.
    """
    try:
        meeting = await asyncio.wait_for(
            asyncio.to_thread(gemini_parser.parse_with_gemini, text, now),
            timeout=config.GEMINI_TIMEOUT_SECONDS + 5,
        )
        logger.info("Message understood via Gemini")
        return meeting, "gemini"
    except Exception as error:
        logger.warning("Gemini unavailable (%s); using basic parser", type(error).__name__)
        return parse_message(text, now), "fallback"


# ----------------------------------------------------------------------
# Formatting helpers
# ----------------------------------------------------------------------

def fmt_day(dt: datetime) -> str:
    return dt.strftime("%A, %d %B %Y")


def fmt_time(dt: datetime) -> str:
    return dt.strftime("%I:%M %p").lstrip("0")


def fmt_slot(iso_or_date: str) -> str:
    """Format a clash event's time; handles timed and all-day events."""
    if not iso_or_date:
        return "?"
    if "T" in iso_or_date:
        try:
            return fmt_time(datetime.fromisoformat(iso_or_date).astimezone(TZ))
        except Exception:
            return iso_or_date
    return "all day"


def ask_for(piece: str) -> str:
    if piece == "email":
        return "Got it. What's the customer's email address?"
    if piece == "time":
        return "When should the meeting be? For example: 'tomorrow 4pm' or '3pm to 4pm'."
    if piece == "date":
        return "Which date should it be? For example: '2 November' or 'next Monday'."
    if piece == "name":
        return "What's the customer's name?"
    return "Could you give me a bit more detail?"


def working_hours_warnings(start: datetime, end: datetime):
    warnings = []
    if start.weekday() not in config.WORK_DAYS:
        warnings.append("it's outside your usual working days")
    if not (config.WORK_START_HOUR <= start.hour < config.WORK_END_HOUR):
        warnings.append(
            f"it's outside your usual hours "
            f"({config.WORK_START_HOUR}:00-{config.WORK_END_HOUR}:00)"
        )
    elif end.hour > config.WORK_END_HOUR or (end.hour == config.WORK_END_HOUR and end.minute > 0):
        warnings.append("it runs past your usual finish time")
    return warnings


def days_away(start: datetime) -> int:
    """Whole days between today and the meeting date (0 = today)."""
    return (start.date() - datetime.now(TZ).date()).days


def how_far_phrase(n: int) -> str:
    if n == 0:
        return "today"
    if n == 1:
        return "tomorrow"
    return f"in {n} days"


def summary_text(name, email, start, end, duration_minutes, warnings) -> str:
    n = days_away(start)
    lines = [
        "Please check these details:",
        "",
        f"👤 Name:     {name}",
        f"✉️ Email:    {email}",
        f"📅 Date:     {fmt_day(start)} ({how_far_phrase(n)})",
        f"🕒 Time:     {fmt_time(start)} - {fmt_time(end)} ({config.TIMEZONE})",
        f"⏱️ Duration: {format_duration(duration_minutes)}",
    ]
    if n > 60:
        lines.append("")
        lines.append("⚠️ This is far away. Is that right?")
    if warnings:
        lines.append("")
        lines.append("⚠️ Heads-up: " + "; ".join(warnings) + ". You can still confirm.")
    lines.append("")
    lines.append("Confirm to book it (I'll invite the customer and add a Meet link).")
    return "\n".join(lines)


def confirm_keyboard():
    return InlineKeyboardMarkup(
        [
            [InlineKeyboardButton("✅ Confirm", callback_data="confirm")],
            [
                InlineKeyboardButton("✏️ Edit", callback_data="edit"),
                InlineKeyboardButton("❌ Cancel", callback_data="cancel"),
            ],
        ]
    )


def clash_keyboard():
    return InlineKeyboardMarkup(
        [
            [InlineKeyboardButton("📌 Book anyway", callback_data="book_anyway")],
            [
                InlineKeyboardButton("🕒 Pick another time", callback_data="pick_time"),
                InlineKeyboardButton("❌ Cancel", callback_data="cancel"),
            ],
        ]
    )


def present_fields(parsed: ParsedMeeting):
    """Which pieces this single message actually supplied."""
    fields = set()
    if parsed.email:
        fields.add("email")
    if parsed.the_time is not None:
        fields.add("time")
    if parsed.the_date is not None:
        fields.add("date")
    if parsed.name:
        fields.add("name")
    return fields


def make_pending(name, email, start, end, duration):
    return {"name": name, "email": email, "start": start, "end": end, "duration": duration}


# ----------------------------------------------------------------------
# Command handlers
# ----------------------------------------------------------------------

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_owner(update):
        return await reject_stranger(update)
    context.user_data.clear()
    await update.message.reply_text(
        "Hi! I'm your meeting assistant.\n\n"
        "Tell me about a meeting in one message, like:\n"
        "   Meeting with Rahul Sharma, rahul@gmail.com, tomorrow 4pm, 1 hour\n\n"
        "I'll check your calendar and ask you to confirm before booking.\n"
        "Use /cancel anytime to start over."
    )


async def help_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_owner(update):
        return await reject_stranger(update)
    await update.message.reply_text(
        "How to use me:\n"
        "• Send meeting details in plain language (name, email, date, time).\n"
        "• I'll ask for anything that's missing.\n"
        "• I show the date I understood, then check your calendar for clashes.\n"
        "• You confirm with the buttons; then I book it and invite the customer.\n"
        "• /upcoming - show your next meetings.\n"
        "• /cancel - forget the current meeting and start fresh."
    )


async def cancel(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_owner(update):
        return await reject_stranger(update)
    context.user_data.clear()
    await update.message.reply_text("Okay, cancelled. Send me new details whenever you like.")


async def upcoming(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_owner(update):
        return await reject_stranger(update)
    try:
        service = await get_service(context)
        events = await asyncio.to_thread(calendar_service.list_upcoming, service, 10)
    except CalendarError as error:
        await update.message.reply_text(f"😕 I couldn't fetch your meetings: {error}")
        return

    if not events:
        await update.message.reply_text("You have no upcoming meetings. 🎉")
        return

    lines = ["Your next meetings:\n"]
    for ev in events:
        iso = ev["start"]
        if iso and "T" in iso:
            dt = datetime.fromisoformat(iso).astimezone(TZ)
            when = f"{dt.strftime('%a %d %b')}, {fmt_time(dt)}"
        else:
            when = f"{iso} (all day)"
        line = f"• {ev['summary']} - {when}"
        if ev.get("meet"):
            line += f"\n  🔗 {ev['meet']}"
        lines.append(line)
    await update.message.reply_text("\n".join(lines))


# ----------------------------------------------------------------------
# The main message handler
# ----------------------------------------------------------------------

async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_owner(update):
        return await reject_stranger(update)

    text = update.message.text or ""
    now = datetime.now(TZ)
    new, source = await parse_incoming(text, now)
    if source == "fallback":
        await update.message.reply_text("AI unavailable, used basic parsing.")

    # Decide: is this a reply to a question we asked, or a brand-new booking?
    # We only keep old data if we were waiting for a specific piece AND this message
    # provides only that kind of piece. Otherwise we start completely fresh.
    awaiting = context.user_data.get("awaiting")
    draft = context.user_data.get("draft")
    fields = present_fields(new)

    if draft and awaiting and fields and fields.issubset(awaiting):
        draft = merge(draft, new)
    else:
        draft = new
        context.user_data.pop("awaiting", None)
        context.user_data.pop("pending", None)
    context.user_data["draft"] = draft

    # Reversed time range -> ask again instead of guessing.
    if draft.end_before_start:
        draft.the_time = None
        draft.the_end_time = None
        draft.end_before_start = False
        context.user_data["draft"] = draft
        context.user_data["awaiting"] = {"time"}
        await update.message.reply_text(
            "The end time looks earlier than the start time. "
            "Could you resend just the time? For example: '3pm to 6pm'."
        )
        return

    # Ask for any missing piece (one at a time).
    missing = draft.missing()
    if missing:
        context.user_data["awaiting"] = set(missing)
        await update.message.reply_text(ask_for(missing[0]))
        return

    # Everything's present -> validate, check calendar, then show confirmation.
    await review_and_present(update, context, draft)


async def review_and_present(update, context, draft: ParsedMeeting):
    start = draft.start
    end = start + timedelta(minutes=draft.duration_minutes)

    # 1) Reject past times WITHOUT silently moving the date.
    if start < datetime.now(TZ):
        draft.the_date = None  # clear just the date; keep the time they gave
        context.user_data["draft"] = draft
        context.user_data["awaiting"] = {"date"}
        context.user_data.pop("pending", None)
        passed = f"{start.day} {start.strftime('%B %Y')}"
        await update.message.reply_text(
            f"{passed} has already passed. Please send a new date."
        )
        return

    # 2) Always show the exact date understood, BEFORE checking the calendar.
    await update.message.reply_text(
        f"📅 Understood: {fmt_day(start)} ({how_far_phrase(days_away(start))}), "
        f"{fmt_time(start)} - {fmt_time(end)} ({config.TIMEZONE}).\n"
        "Checking your calendar for clashes..."
    )

    # 3) Check the calendar for clashes.
    try:
        service = await get_service(context)
        clashes = await asyncio.to_thread(calendar_service.get_events_between, service, start, end)
    except CalendarError as error:
        logger.warning("Conflict check failed: %s", error)
        clashes = []
        await update.message.reply_text(
            "⚠️ I couldn't reach Google Calendar to check for clashes right now, "
            "so I can't guarantee you're free. You can still confirm below."
        )

    if clashes:
        # Keep a pending booking (for "Book anyway") AND a draft with the time cleared
        # (so "Pick another time" / typing a new time works).
        context.user_data["pending"] = make_pending(
            draft.name, draft.email, start, end, draft.duration_minutes
        )
        draft.the_time = None
        draft.the_end_time = None
        context.user_data["draft"] = draft
        context.user_data["awaiting"] = {"time"}

        lines = [
            f"⚠️ You already have something on {fmt_day(start)} that overlaps "
            f"{fmt_time(start)} - {fmt_time(end)}:"
        ]
        for ev in clashes:
            lines.append(f"• {ev['summary']} ({fmt_slot(ev['start'])} - {fmt_slot(ev['end'])})")
        lines.append("")
        lines.append("What would you like to do?")
        await update.message.reply_text("\n".join(lines), reply_markup=clash_keyboard())
        return

    # 4) Free -> warnings (allowed) and the confirm buttons.
    warnings = working_hours_warnings(start, end)
    context.user_data["pending"] = make_pending(
        draft.name, draft.email, start, end, draft.duration_minutes
    )
    context.user_data.pop("draft", None)
    context.user_data.pop("awaiting", None)

    await update.message.reply_text(
        summary_text(draft.name, draft.email, start, end, draft.duration_minutes, warnings),
        reply_markup=confirm_keyboard(),
    )


# ----------------------------------------------------------------------
# Button handler
# ----------------------------------------------------------------------

async def handle_button(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    if not is_owner(update):
        return await reject_stranger(update)

    await query.answer()
    choice = query.data
    pending = context.user_data.get("pending")

    if choice == "cancel":
        context.user_data.clear()
        await query.edit_message_text("❌ Cancelled. Nothing was booked.")
        return

    if choice == "edit":
        context.user_data.clear()
        await query.edit_message_text("✏️ Okay, let's redo it. Send me the meeting details again.")
        return

    if choice == "pick_time":
        context.user_data.pop("pending", None)
        context.user_data["awaiting"] = {"time"}
        await query.edit_message_text("🕒 Okay. Send me a different time (e.g. 'tomorrow 5pm').")
        return

    if choice in ("confirm", "book_anyway"):
        if not pending:
            await query.edit_message_text("This booking has expired. Please send the details again.")
            return
        note = "⏳ Booking it with Google Calendar..."
        if choice == "book_anyway":
            note = "⏳ Booking it anyway (despite the clash)..."
        await query.edit_message_text(note)
        await do_booking(context, pending, query.message.chat.id)
        return


async def do_booking(context, pending, chat_id):
    name = pending["name"]
    email = pending["email"]
    start = pending["start"]
    end = pending["end"]

    try:
        service = await get_service(context)
        event = await asyncio.to_thread(
            calendar_service.create_meeting_event,
            service,
            f"Meeting with {name}",
            start,
            end,
            "Scheduled via your meeting assistant.",
            email,   # attendee
            "all",   # email the customer the invite
        )
    except CalendarError as error:
        await context.bot.send_message(
            chat_id=chat_id,
            text=f"😕 Sorry, I couldn't book it: {error}\nNothing was created. Please try again.",
        )
        return
    except Exception as error:
        logger.exception("Unexpected booking error")
        await context.bot.send_message(
            chat_id=chat_id,
            text=f"😕 Something unexpected went wrong while booking: {error}\nPlease try again.",
        )
        return

    context.user_data.clear()
    meet_link = calendar_service.extract_meet_link(event) or "(no Meet link returned)"
    event_link = event.get("htmlLink", "")

    # Message 1: your confirmation.
    await context.bot.send_message(
        chat_id=chat_id,
        text=(
            "✅ Booked!\n\n"
            f"👤 {name}\n"
            f"✉️ {email}  (invite emailed)\n"
            f"📅 {fmt_day(start)}\n"
            f"🕒 {fmt_time(start)} - {fmt_time(end)} ({config.TIMEZONE})\n"
            f"🔗 Meet: {meet_link}\n"
            + (f"📎 Event: {event_link}\n" if event_link else "")
        ),
    )

    # Message 2: a tidy message you can forward to the customer.
    first_name = name.split()[0] if name else "there"
    await context.bot.send_message(
        chat_id=chat_id,
        text="👇 You can forward this message to the customer:",
    )
    await context.bot.send_message(
        chat_id=chat_id,
        text=(
            f"Hi {first_name}, your meeting is confirmed. 🎉\n\n"
            f"📅 {fmt_day(start)}\n"
            f"🕒 {fmt_time(start)} - {fmt_time(end)} (India time / {config.TIMEZONE})\n"
            f"🔗 Join on Google Meet: {meet_link}\n\n"
            "A calendar invite has also been emailed to you. Looking forward to speaking!"
        ),
    )


# ----------------------------------------------------------------------
# Catch-all error handler (keeps the bot alive 24/7)
# ----------------------------------------------------------------------

async def on_error(update, context):
    logger.exception("Unhandled error", exc_info=context.error)
    # Notify the owner involved if we can tell who it was; else the primary owner.
    chat_id = config.OWNER_CHAT_ID
    if isinstance(update, Update) and update.effective_chat and update.effective_chat.id in config.OWNER_CHAT_IDS:
        chat_id = update.effective_chat.id
    try:
        await context.bot.send_message(
            chat_id=chat_id,
            text="😕 Something went wrong just now, but I'm still running. Please try again.",
        )
    except Exception:
        pass  # never let the error handler itself crash


# ----------------------------------------------------------------------
# Start the bot
# ----------------------------------------------------------------------

def main():
    if not config.TELEGRAM_BOT_TOKEN or config.OWNER_CHAT_ID is None:
        print("Please fill in TELEGRAM_BOT_TOKEN and OWNER_CHAT_ID in your .env file.")
        return

    app = Application.builder().token(config.TELEGRAM_BOT_TOKEN).build()

    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("help", help_command))
    app.add_handler(CommandHandler("cancel", cancel))
    app.add_handler(CommandHandler("upcoming", upcoming))
    app.add_handler(CallbackQueryHandler(handle_button))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message))
    app.add_error_handler(on_error)

    logger.info("Bot is starting. Press Ctrl + C to stop.")
    app.run_polling(allowed_updates=Update.ALL_TYPES)


if __name__ == "__main__":
    main()
