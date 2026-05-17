"""Telegram bot handlers."""

import logging
import os
import tomllib
from pathlib import Path

from telegram import Bot, InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.ext import (
    Application,
    CallbackQueryHandler,
    CommandHandler,
    ContextTypes,
)

from alone_bot.db import (
    add_activity,
    get_chat_id,
    get_stats,
    list_activities,
    log_suggestion,
    recent_session_activity_ids,
    set_state,
    update_suggestion_completion,
    update_suggestion_response,
)
from alone_bot.selector import pick_activity
from alone_bot.scheduler import schedule_followup


logger = logging.getLogger(__name__)


def _load_session_window() -> int:
    with Path("/app/config.toml").open("rb") as f:
        config = tomllib.load(f)
    return config["behavior"]["session_window_minutes"]


def _suggestion_keyboard(suggestion_id: int) -> InlineKeyboardMarkup:
    """Build the Accept/Another/Reject keyboard for a suggestion."""
    return InlineKeyboardMarkup([[
        InlineKeyboardButton("✅ Accept", callback_data=f"accepted:{suggestion_id}"),
        InlineKeyboardButton("🔁 Another", callback_data=f"another:{suggestion_id}"),
        InlineKeyboardButton("❌ Reject", callback_data=f"rejected:{suggestion_id}"),
    ]])


def _gate_keyboard(gate_suggestion_id: int) -> InlineKeyboardMarkup:
    """Build the Yes/No keyboard for the 'Are you alone?' gate.

    Uses a dedicated 'gate' namespace in callback_data so the button
    handler can distinguish gate answers from suggestion answers.
    """
    return InlineKeyboardMarkup([[
        InlineKeyboardButton("Yes", callback_data=f"gate_yes:{gate_suggestion_id}"),
        InlineKeyboardButton("No", callback_data=f"gate_no:{gate_suggestion_id}"),
    ]])


async def _send_suggestion(bot: Bot, chat_id: int, trigger: str) -> None:
    """Pick an activity, log it, send it with buttons. Shared by /suggest
    and the gate-Yes path so the suggestion flow is identical.

    trigger is 'on_demand' for /suggest or 'scheduled' for gate-Yes.
    """
    activity = pick_activity()

    if activity is None:
        await bot.send_message(
            chat_id=chat_id,
            text="No eligible activities right now — everything's on cooldown. "
                 "Try again later or /add a new one.",
        )
        return

    suggestion_id = log_suggestion(
        activity_id=activity["id"], trigger=trigger
    )

    await bot.send_message(
        chat_id=chat_id,
        text=f"How about: *{activity['text']}*",
        reply_markup=_suggestion_keyboard(suggestion_id),
        parse_mode="Markdown",
    )

    logger.info(
        f"Sent suggestion id={suggestion_id} activity_id={activity['id']} "
        f"text={activity['text']!r} trigger={trigger}"
    )


async def send_alone_gate(bot: Bot) -> None:
    """Send the 'Are you alone?' question. Called by the scheduler.

    Logs a gate-suggestion row up-front (with no activity_id) so we
    can record the eventual gate response against it. If the user
    answers Yes, that row's response becomes the entry-point for
    deciding to actually pick an activity.
    """
    chat_id = get_chat_id()
    if chat_id is None:
        logger.error("Scheduled ping fired but no chat_id bound — has /start been run?")
        return

    # activity_id=NULL because no activity has been picked yet.
    # If the user answers Yes, a separate suggestion row is logged
    # for the actual activity. If they answer No, this gate row
    # gets response='not_alone' and we're done.
    gate_suggestion_id = log_suggestion(activity_id=None, trigger="scheduled")

    await bot.send_message(
        chat_id=chat_id,
        text="Hey — are you alone right now?",
        reply_markup=_gate_keyboard(gate_suggestion_id),
    )

    logger.info(f"Sent alone-gate id={gate_suggestion_id}")


async def start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Handle /start: bind this chat as the single-user chat for the bot."""
    chat_id = update.effective_chat.id
    user = update.effective_user

    set_state("chat_id", str(chat_id))
    logger.info(f"Bound chat_id={chat_id} (user: {user.username or user.first_name})")

    await update.message.reply_text(
        f"Hey {user.first_name}, alone-bot is alive. "
        f"You're bound as the chat at id {chat_id}.\n\n"
        f"Try /suggest to get an activity."
    )


async def suggest(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Handle /suggest: pick an activity and send it with inline buttons."""
    await _send_suggestion(
        context.bot, update.effective_chat.id, trigger="on_demand"
    )


async def add(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Handle /add <text>: append a new activity to the list."""
    if not context.args:
        await update.message.reply_text(
            "Usage: /add <activity text>\n"
            "Example: /add Take a long walk"
        )
        return

    text = " ".join(context.args)
    success, message = add_activity(text)
    await update.message.reply_text(message)
    logger.info(f"/add: success={success} text={text!r}")


async def list_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Handle /list: show all active activities."""
    activities = list_activities()
    if not activities:
        await update.message.reply_text("No activities in the list. Use /add to add one.")
        return

    lines = [f"{a['id']}. {a['text']}" for a in activities]
    message = f"*Activities ({len(activities)}):*\n" + "\n".join(lines)
    await update.message.reply_text(message, parse_mode="Markdown")


async def stats(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Handle /stats: show summary statistics."""
    s = get_stats()

    accept_rate_str = (
        f"{s['accept_rate']:.0f}%" if s["accept_rate"] is not None else "n/a"
    )
    completion_rate_str = (
        f"{s['completion_rate']:.0f}%" if s["completion_rate"] is not None else "n/a"
    )

    lines = [
        "*alone-bot stats*",
        "",
        f"Activities in pool: {s['total_activities']}",
        f"Suggestions sent: {s['total_suggestions']}",
        "",
        f"Accepted: {s['accepted']} · Rejected: {s['rejected']}",
        f"Acceptance rate: {accept_rate_str}",
        "",
        f"Completed: {s['completed_yes']} · Skipped: {s['completed_no']}",
        f"Completion rate: {completion_rate_str}",
    ]

    if s["top_accepted"]:
        lines += ["", "*Top accepted:*"]
        lines += [f"  • {r['text']} ({r['n']})" for r in s["top_accepted"]]

    if s["top_rejected"]:
        lines += ["", "*Top rejected:*"]
        lines += [f"  • {r['text']} ({r['n']})" for r in s["top_rejected"]]

    await update.message.reply_text("\n".join(lines), parse_mode="Markdown")

async def on_button(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Handle button taps from inline keyboards.

    callback_data formats:
        gate_yes:<id>  — user answered Yes to "are you alone?"
        gate_no:<id>   — user answered No
        accepted:<id>  — user accepted a suggestion
        another:<id>   — user wants a different suggestion
        rejected:<id>  — user rejected a suggestion
    """
    query = update.callback_query
    await query.answer()

    try:
        action, suggestion_id_str = query.data.split(":", 1)
        suggestion_id = int(suggestion_id_str)
    except (ValueError, AttributeError):
        logger.error(f"Malformed callback_data: {query.data!r}")
        return

    # Gate responses (Yes/No to "are you alone?")
    if action == "gate_yes":
        update_suggestion_response(suggestion_id, "gate_yes")  # gate accepted
        await query.edit_message_reply_markup(reply_markup=None)
        logger.info(f"Gate id={suggestion_id} answered yes — sending suggestion")
        await _send_suggestion(
            context.bot, update.effective_chat.id, trigger="scheduled"
        )
        return

    if action == "gate_no":
        update_suggestion_response(suggestion_id, "not_alone")
        await query.edit_message_reply_markup(reply_markup=None)
        await query.message.reply_text("All good. Catch you next time.")
        logger.info(f"Gate id={suggestion_id} answered no")
        return
    
    # Followup responses (did you actually do it?)
    if action == "followup_yes":
        from alone_bot.db import update_suggestion_completion
        updated = update_suggestion_completion(suggestion_id, completed=True)
        await query.edit_message_reply_markup(reply_markup=None)
        await query.message.reply_text("Love that. Nice work.")
        logger.info(
            f"Followup id={suggestion_id} marked completed=True "
            f"activity={updated['activity_text']!r}"
        )
        return

    if action == "followup_no":
        from alone_bot.db import update_suggestion_completion
        updated = update_suggestion_completion(suggestion_id, completed=False)
        await query.edit_message_reply_markup(reply_markup=None)
        await query.message.reply_text("No worries. Catch the next one.")
        logger.info(
            f"Followup id={suggestion_id} marked completed=False "
            f"activity={updated['activity_text']!r}"
        )
        return

    # Suggestion responses (Accept/Another/Reject)
    if action not in {"accepted", "rejected", "another"}:
        logger.error(f"Unknown callback action: {action!r}")
        return

    updated = update_suggestion_response(suggestion_id, action)
    if updated is None:
        logger.error(f"No suggestion row found for id={suggestion_id}")
        return

    activity_text = updated["activity_text"] or "<unknown>"
    logger.info(
        f"Button tap: suggestion_id={suggestion_id} action={action} "
        f"activity={activity_text!r}"
    )

    if action == "accepted":
        await query.edit_message_reply_markup(reply_markup=None)
        # Schedule the 'did you do it?' followup
        config_path = Path("/app/config.toml")
        with config_path.open("rb") as f:
            followup_hours = tomllib.load(f)["schedule"]["followup_hours"]
        schedule_followup(context.bot, suggestion_id, followup_hours)

        await query.message.reply_text(
            f"Nice. Go do it. I'll check in in {followup_hours} hours."
        )

    elif action == "rejected":
        await query.edit_message_reply_markup(reply_markup=None)
        await query.message.reply_text("Got it. Maybe next time.")

    elif action == "another":
        session_window = _load_session_window()
        exclude = recent_session_activity_ids(session_window)
        new_activity = pick_activity(exclude_ids=exclude)

        await query.edit_message_reply_markup(reply_markup=None)

        if new_activity is None:
            await query.message.reply_text(
                "Out of fresh options for now — everything's been on the "
                "table recently. Try again later or /add new ones."
            )
            return

        new_suggestion_id = log_suggestion(
            activity_id=new_activity["id"], trigger="rerolled"
        )

        await query.message.reply_text(
            f"How about: *{new_activity['text']}*",
            reply_markup=_suggestion_keyboard(new_suggestion_id),
            parse_mode="Markdown",
        )

        logger.info(
            f"Re-rolled to suggestion id={new_suggestion_id} "
            f"activity_id={new_activity['id']} text={new_activity['text']!r}"
        )


def build_application() -> Application:
    """Construct and configure the Telegram Application."""
    token = os.environ["TELEGRAM_BOT_TOKEN"]
    app = Application.builder().token(token).build()
    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("suggest", suggest))
    app.add_handler(CommandHandler("add", add))
    app.add_handler(CommandHandler("list", list_cmd))
    app.add_handler(CommandHandler("stats", stats))
    app.add_handler(CallbackQueryHandler(on_button))
    return app