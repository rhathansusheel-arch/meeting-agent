"""
bot.py
------
The Telegram bot - a conversational scheduling assistant.

Flow for every message:
  1. Owner-only check (ignore everyone else).
  2. Expire the draft if it's been idle > 30 minutes.
  3. Ask Gemini (with the draft + recent history) what the owner means (intent)
     and what, if anything, this message adds/changes. Fall back to the rule-based
     parser if Gemini is unavailable.
  4. Act on the intent: chat, answer a calendar question, or build a booking.
  5. For bookings: ADD to the draft (never reset it), verify in Python, ask for
     anything missing, then show a summary with Confirm / Edit / Cancel. Clashes
     offer "Book anyway". Only the Confirm button creates the event.

Run:  python bot.py     Stop: Ctrl + C
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
from message_parser import TZ, ParsedMeeting, format_duration, parse_message

# ----------------------------------------------------------------------
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s  %(levelname)s  %(message)s",
    datefmt="%H:%M:%S",
)
logging.getLogger("httpx").setLevel(logging.WARNING)
logger = logging.getLogger("bot")

IDLE_SECONDS = 30 * 60  # clear a half-finished booking after 30 minutes idle


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
# Google Calendar connection (built once, cached)
# ----------------------------------------------------------------------

async def get_service(context: ContextTypes.DEFAULT_TYPE):
    service = context.application.bot_data.get("calendar_service")
    if service is None:
        service = await asyncio.to_thread(calendar_service.get_calendar_service)
        context.application.bot_data["calendar_service"] = service
    return service


# ----------------------------------------------------------------------
# Formatting helpers
# ----------------------------------------------------------------------

def fmt_day(dt: datetime) -> str:
    return dt.strftime("%A, %d %B %Y")


def fmt_time(dt: datetime) -> str:
    return dt.strftime("%I:%M %p").lstrip("0")


def fmt_slot(iso_or_date: str) -> str:
    if not iso_or_date:
        return "?"
    if "T" in iso_or_date:
        try:
            return fmt_time(datetime.fromisoformat(iso_or_date).astimezone(TZ))
        except Exception:
            return iso_or_date
    return "all day"


def days_away(start: datetime) -> int:
    return (start.date() - datetime.now(TZ).date()).days


def how_far_phrase(n: int) -> str:
    if n == 0:
        return "today"
    if n == 1:
        return "tomorrow"
    return f"in {n} days"


def ask_for(piece: str) -> str:
    return {
        "email": "What's the customer's email address?",
        "time": "What time should the meeting be? (e.g. 'tomorrow 4pm' or '3pm to 4pm')",
        "name": "What's the customer's name?",
    }.get(piece, "Could you give me a bit more detail?")


def working_hours_warnings(start: datetime, end: datetime):
    warnings = []
    if start.weekday() not in config.WORK_DAYS:
        warnings.append("it's outside your usual working days")
    if not (config.WORK_START_HOUR <= start.hour < config.WORK_END_HOUR):
        warnings.append(f"it's outside your usual hours ({config.WORK_START_HOUR}:00-{config.WORK_END_HOUR}:00)")
    elif end.hour > config.WORK_END_HOUR or (end.hour == config.WORK_END_HOUR and end.minute > 0):
        warnings.append("it runs past your usual finish time")
    return warnings


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
        lines += ["", "⚠️ This is far away. Is that right?"]
    if warnings:
        lines += ["", "⚠️ Heads-up: " + "; ".join(warnings) + ". You can still confirm."]
    lines += ["", "Confirm to book it (I'll invite the customer and add a Meet link)."]
    return "\n".join(lines)


def confirm_keyboard():
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("✅ Confirm", callback_data="confirm")],
        [InlineKeyboardButton("✏️ Edit", callback_data="edit"),
         InlineKeyboardButton("❌ Cancel", callback_data="cancel")],
    ])


def clash_keyboard():
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("📌 Book anyway", callback_data="book_anyway")],
        [InlineKeyboardButton("🕒 Pick another time", callback_data="pick_time"),
         InlineKeyboardButton("❌ Cancel", callback_data="cancel")],
    ])


def make_pending(draft, start, end):
    return {"name": draft.name, "email": draft.email,
            "start": start, "end": end, "duration": draft.duration_minutes}


# ----------------------------------------------------------------------
# Draft + history state (per chat)
# ----------------------------------------------------------------------

def get_draft(context, now) -> ParsedMeeting:
    draft = context.user_data.get("draft") or ParsedMeeting(now_ref=now)
    draft.now_ref = now
    return draft


def draft_to_dict(draft: ParsedMeeting):
    hm = lambda t: f"{t[0]:02d}:{t[1]:02d}" if t else None
    dur = draft.duration_minutes if (draft.duration_explicit or draft.duration_from_range) else None
    return {
        "customer_name": draft.name,
        "customer_email": draft.email,
        "date": draft.the_date.isoformat() if draft.the_date else None,
        "start_time": hm(draft.the_time),
        "end_time": hm(draft.the_end_time),
        "duration_minutes": dur,
    }


def push_history(context, role, text):
    history = context.user_data.get("history", [])
    history.append((role, text))
    context.user_data["history"] = history[-6:]


def apply_fields(draft, *, name=None, email=None, the_date=None,
                 the_time=None, the_end_time=None, duration=None):
    """ADD fields to the draft (overwrite only when a new value is given)."""
    if name is not None:
        draft.name = name
    if email is not None:
        draft.email = email
    if the_date is not None:
        draft.the_date = the_date
    if the_time is not None:
        draft.the_time = the_time
        draft.end_before_start = False
    if the_end_time is not None:
        draft.the_end_time = the_end_time
    if duration is not None:
        draft.duration_minutes = duration
        draft.duration_explicit = True
    # Work out duration from a time range when no explicit duration was given.
    if draft.the_time and draft.the_end_time and not draft.duration_explicit:
        diff = (draft.the_end_time[0] * 60 + draft.the_end_time[1]) - (draft.the_time[0] * 60 + draft.the_time[1])
        if diff > 0:
            draft.duration_minutes = diff
            draft.duration_from_range = True
            draft.end_before_start = False
        else:
            draft.end_before_start = True


def apply_turn(draft, turn):
    """Apply Gemini's extracted fields (each verified in Python) to the draft."""
    apply_fields(
        draft,
        name=gemini_parser.valid_name(turn.customer_name) if turn.customer_name else None,
        email=gemini_parser.valid_email(turn.customer_email) if turn.customer_email else None,
        the_date=gemini_parser.parse_date_str(turn.date) if turn.date else None,
        the_time=gemini_parser.parse_hhmm(turn.start_time) if turn.start_time else None,
        the_end_time=gemini_parser.parse_hhmm(turn.end_time) if turn.end_time else None,
        duration=turn.duration_minutes if isinstance(turn.duration_minutes, int) and turn.duration_minutes > 0 else None,
    )


def apply_parsed(draft, parsed):
    """Apply the rule-based parser's result (fallback) to the draft."""
    apply_fields(
        draft,
        name=parsed.name,
        email=parsed.email,
        the_date=parsed.the_date,
        the_time=parsed.the_time,
        the_end_time=parsed.the_end_time,
        duration=parsed.duration_minutes if parsed.duration_explicit else None,
    )


# ----------------------------------------------------------------------
# Command handlers
# ----------------------------------------------------------------------

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_owner(update):
        return await reject_stranger(update)
    context.user_data.clear()
    await update.message.reply_text(
        "Hi! I'm your meeting assistant. 👋\n\n"
        "Just chat with me naturally - tell me about a meeting, e.g.\n"
        "   Meeting with Rahul Sharma, rahul@gmail.com, tomorrow 4pm\n\n"
        "You can also ask things like \"what's on my calendar tomorrow?\".\n"
        "Use /cancel anytime to start over."
    )


async def help_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_owner(update):
        return await reject_stranger(update)
    await update.message.reply_text(
        "How to use me:\n"
        "• Chat naturally to arrange a meeting - I'll ask for anything missing.\n"
        "• Correct me anytime (\"make it 5pm\", \"change the email to ...\").\n"
        "• Ask about your schedule (\"am I free Friday at 3?\").\n"
        "• I check for clashes, then show Confirm / Edit / Cancel before booking.\n"
        "• /upcoming - your next meetings.   /cancel - start over."
    )


async def cancel(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_owner(update):
        return await reject_stranger(update)
    context.user_data.clear()
    await update.message.reply_text("Okay, cancelled. Nothing is booked. Send me new details whenever you like.")


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
# Answering a calendar question
# ----------------------------------------------------------------------

async def answer_calendar_question(context, turn, now):
    the_date = gemini_parser.parse_date_str(turn.date) or now.date()
    the_time = gemini_parser.parse_hhmm(turn.start_time)
    try:
        service = await get_service(context)
        if the_time:
            slot_start = datetime(the_date.year, the_date.month, the_date.day, the_time[0], the_time[1], tzinfo=TZ)
            slot_end = slot_start + timedelta(minutes=30)
            clashes = await asyncio.to_thread(calendar_service.get_events_between, service, slot_start, slot_end)
            when = f"{fmt_day(slot_start)} at {fmt_time(slot_start)}"
            if not clashes:
                return f"✅ You look free on {when}."
            titles = ", ".join(ev["summary"] for ev in clashes)
            return f"⛔ You're busy on {when}: {titles}."
        else:
            day_start = datetime(the_date.year, the_date.month, the_date.day, 0, 0, tzinfo=TZ)
            day_end = day_start + timedelta(days=1)
            events = await asyncio.to_thread(calendar_service.get_events_between, service, day_start, day_end)
            if not events:
                return f"You have nothing on your calendar for {fmt_day(day_start)}. 🎉"
            lines = [f"On {fmt_day(day_start)} you have:"]
            for ev in events:
                lines.append(f"• {ev['summary']} ({fmt_slot(ev['start'])} - {fmt_slot(ev['end'])})")
            return "\n".join(lines)
    except CalendarError as error:
        return f"😕 I couldn't reach Google Calendar: {error}"


# ----------------------------------------------------------------------
# The main message handler
# ----------------------------------------------------------------------

async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_owner(update):
        return await reject_stranger(update)

    text = update.message.text or ""
    now = datetime.now(TZ)

    # Expire a stale half-finished booking.
    last = context.user_data.get("last_active")
    if last and now.timestamp() - last > IDLE_SECONDS:
        context.user_data.clear()
    context.user_data["last_active"] = now.timestamp()

    history = context.user_data.get("history", [])
    draft = get_draft(context, now)

    # Understand the message with Gemini (fall back to rules on any failure).
    turn = None
    try:
        turn = await asyncio.wait_for(
            asyncio.to_thread(gemini_parser.run_turn, text, draft_to_dict(draft), history, now),
            timeout=config.GEMINI_TIMEOUT_SECONDS + 5,
        )
    except Exception as error:
        logger.warning("parsed by: fallback (%s)", type(error).__name__)

    push_history(context, "owner", text)

    if turn is None:
        await update.message.reply_text("AI unavailable, used basic parsing.")
        await handle_booking_fallback(update, context, text, now, draft)
        return

    logger.info("parsed by: gemini | intent=%s", turn.intent)

    # --- non-booking intents ---
    if turn.intent == "cancel":
        context.user_data.clear()
        await update.message.reply_text(turn.reply or "Okay, cancelled. Nothing is booked.")
        return

    if turn.intent in ("greeting", "chitchat", "unclear"):
        msg = turn.reply or ("Hi! Tell me about a meeting to book, e.g. "
                             "'Meeting with Rahul, rahul@example.com, tomorrow 4pm'.")
        push_history(context, "assistant", msg)
        await update.message.reply_text(msg)
        return

    if turn.intent == "calendar_question":
        msg = await answer_calendar_question(context, turn, now)
        push_history(context, "assistant", msg)
        await update.message.reply_text(msg)
        return

    # --- booking intents: new_booking / update_draft / confirm ---
    apply_turn(draft, turn)
    context.user_data["draft"] = draft

    if draft.end_before_start:
        draft.the_time = None
        draft.the_end_time = None
        draft.end_before_start = False
        context.user_data["draft"] = draft
        msg = "The end time looks earlier than the start time. What times did you mean? (e.g. '3pm to 6pm')"
        push_history(context, "assistant", msg)
        await update.message.reply_text(msg)
        return

    missing = draft.missing()
    if missing:
        msg = turn.reply or ask_for(missing[0])
        push_history(context, "assistant", msg)
        await update.message.reply_text(msg)
        return

    await review_and_present(update, context, draft)


async def handle_booking_fallback(update, context, text, now, draft):
    """Used only when Gemini is unavailable: treat the message as booking details."""
    parsed = parse_message(text, now)
    apply_parsed(draft, parsed)
    context.user_data["draft"] = draft

    if draft.end_before_start:
        draft.the_time = None
        draft.the_end_time = None
        draft.end_before_start = False
        context.user_data["draft"] = draft
        await update.message.reply_text("The end time looks earlier than the start time. What times did you mean?")
        return

    missing = draft.missing()
    if missing:
        await update.message.reply_text(ask_for(missing[0]))
        return

    await review_and_present(update, context, draft)


async def review_and_present(update, context, draft: ParsedMeeting):
    start = draft.start
    end = start + timedelta(minutes=draft.duration_minutes)

    # 1) Reject past times WITHOUT silently moving the date.
    if start < datetime.now(TZ):
        draft.the_date = None
        context.user_data["draft"] = draft
        passed = f"{start.day} {start.strftime('%B %Y')}"
        await update.message.reply_text(f"{passed} has already passed. Please send a new date.")
        return

    # 2) Show the exact understood date (verified in Python), before the clash check.
    await update.message.reply_text(
        f"📅 Understood: {fmt_day(start)} ({how_far_phrase(days_away(start))}), "
        f"{fmt_time(start)} - {fmt_time(end)} ({config.TIMEZONE}).\n"
        "Checking your calendar for clashes..."
    )

    # 3) Clash check.
    try:
        service = await get_service(context)
        clashes = await asyncio.to_thread(calendar_service.get_events_between, service, start, end)
    except CalendarError as error:
        logger.warning("Conflict check failed: %s", error)
        clashes = []
        await update.message.reply_text(
            "⚠️ I couldn't reach Google Calendar to check for clashes right now. You can still confirm below."
        )

    if clashes:
        context.user_data["pending"] = make_pending(draft, start, end)
        lines = [f"⚠️ You already have something on {fmt_day(start)} that overlaps "
                 f"{fmt_time(start)} - {fmt_time(end)}:"]
        for ev in clashes:
            lines.append(f"• {ev['summary']} ({fmt_slot(ev['start'])} - {fmt_slot(ev['end'])})")
        lines += ["", "What would you like to do?"]
        await update.message.reply_text("\n".join(lines), reply_markup=clash_keyboard())
        return

    # 4) Free -> warnings + confirm buttons.
    warnings = working_hours_warnings(start, end)
    context.user_data["pending"] = make_pending(draft, start, end)
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
        draft = context.user_data.get("draft")
        if draft:
            draft.the_time = None
            draft.the_end_time = None
            draft.end_before_start = False
            context.user_data["draft"] = draft
        await query.edit_message_text("🕒 Okay. What time would you like instead?")
        return

    if choice in ("confirm", "book_anyway"):
        if not pending:
            await query.edit_message_text("This booking has expired. Please send the details again.")
            return
        await query.edit_message_text(
            "⏳ Booking it anyway (despite the clash)..." if choice == "book_anyway"
            else "⏳ Booking it with Google Calendar..."
        )
        await do_booking(context, pending, query.message.chat.id)
        return


async def do_booking(context, pending, chat_id):
    name, email = pending["name"], pending["email"]
    start, end = pending["start"], pending["end"]

    try:
        service = await get_service(context)
        event = await asyncio.to_thread(
            calendar_service.create_meeting_event,
            service, f"Meeting with {name}", start, end,
            "Scheduled via your meeting assistant.", email, "all",
        )
    except CalendarError as error:
        await context.bot.send_message(chat_id=chat_id,
            text=f"😕 Sorry, I couldn't book it: {error}\nNothing was created. Please try again.")
        return
    except Exception as error:
        logger.exception("Unexpected booking error")
        await context.bot.send_message(chat_id=chat_id,
            text=f"😕 Something unexpected went wrong while booking: {error}\nPlease try again.")
        return

    context.user_data.clear()
    meet_link = calendar_service.extract_meet_link(event) or "(no Meet link returned)"
    event_link = event.get("htmlLink", "")

    await context.bot.send_message(chat_id=chat_id, text=(
        "✅ Booked!\n\n"
        f"👤 {name}\n"
        f"✉️ {email}  (invite emailed)\n"
        f"📅 {fmt_day(start)}\n"
        f"🕒 {fmt_time(start)} - {fmt_time(end)} ({config.TIMEZONE})\n"
        f"🔗 Meet: {meet_link}\n"
        + (f"📎 Event: {event_link}\n" if event_link else "")
    ))

    first_name = name.split()[0] if name else "there"
    await context.bot.send_message(chat_id=chat_id, text="👇 You can forward this message to the customer:")
    await context.bot.send_message(chat_id=chat_id, text=(
        f"Hi {first_name}, your meeting is confirmed. 🎉\n\n"
        f"📅 {fmt_day(start)}\n"
        f"🕒 {fmt_time(start)} - {fmt_time(end)} (India time / {config.TIMEZONE})\n"
        f"🔗 Join on Google Meet: {meet_link}\n\n"
        "A calendar invite has also been emailed to you. Looking forward to speaking!"
    ))


# ----------------------------------------------------------------------
# Error handler + startup
# ----------------------------------------------------------------------

async def on_error(update, context):
    logger.exception("Unhandled error", exc_info=context.error)
    chat_id = config.OWNER_CHAT_ID
    if isinstance(update, Update) and update.effective_chat and update.effective_chat.id in config.OWNER_CHAT_IDS:
        chat_id = update.effective_chat.id
    try:
        await context.bot.send_message(chat_id=chat_id,
            text="😕 Something went wrong just now, but I'm still running. Please try again.")
    except Exception:
        pass


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
