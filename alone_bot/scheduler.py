"""APScheduler jobs for proactive bot pings.

Two job types:
- Recurring: weekday/weekend gate pings via cron
- One-shot: followup "did you do it?" 1.5h after an accept
"""

import logging
import tomllib
from datetime import datetime, timedelta, timezone
from pathlib import Path

from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.cron import CronTrigger
from apscheduler.triggers.date import DateTrigger
from telegram import Bot, InlineKeyboardButton, InlineKeyboardMarkup

from alone_bot.db import get_chat_id, get_pending_followups, sweep_stale_suggestions


logger = logging.getLogger(__name__)

# Module-level reference so bot.py can schedule followups when /accept fires.
# Set by build_scheduler() during startup.
_scheduler: AsyncIOScheduler | None = None

async def _heartbeat_job(bot: Bot) -> None:
    """Daily 'I'm alive' message. If you don't get it, the bot is down."""
    chat_id = get_chat_id()
    if chat_id is None:
        logger.error("Heartbeat fired but no chat_id bound")
        return

    await bot.send_message(
        chat_id=chat_id,
        text="Alone-bot heartbeat ✓ — still alive.",
    )
    logger.info("Sent heartbeat")

async def _sweep_stale_job(timeout_hours: int) -> None:
    """Hourly job: mark old unanswered suggestions as 'no_response'."""
    swept = sweep_stale_suggestions(timeout_hours)
    if swept > 0:
        logger.info(f"Swept {swept} stale suggestion(s) to no_response")

def _load_config() -> dict:
    with Path("/app/config.toml").open("rb") as f:
        return tomllib.load(f)


def _followup_keyboard(suggestion_id: int) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([[
        InlineKeyboardButton("Yes, did it", callback_data=f"followup_yes:{suggestion_id}"),
        InlineKeyboardButton("No, didn't", callback_data=f"followup_no:{suggestion_id}"),
    ]])


async def _scheduled_ping(bot: Bot, label: str) -> None:
    """Wrap the gate question with logging."""
    # Local import to avoid circular: bot.py imports from scheduler later
    from alone_bot.bot import send_alone_gate
    logger.info(f"Scheduled ping firing: {label}")
    await send_alone_gate(bot)


async def _send_followup(bot: Bot, suggestion_id: int) -> None:
    """Send the 'did you do it?' question for an accepted suggestion."""
    chat_id = get_chat_id()
    if chat_id is None:
        logger.error(f"Followup for id={suggestion_id} fired but no chat_id bound")
        return

    await bot.send_message(
        chat_id=chat_id,
        text="Quick check-in — did you end up doing it?",
        reply_markup=_followup_keyboard(suggestion_id),
    )
    # Stamp it as sent so a restart doesn't re-fire this followup.
    # Local import to avoid circular dependency.
    from alone_bot.db import mark_followup_sent
    mark_followup_sent(suggestion_id)
    logger.info(f"Sent followup for suggestion_id={suggestion_id}")


def schedule_followup(bot: Bot, suggestion_id: int, hours: float) -> None:
    """Schedule a one-shot followup. Called by bot.py when /accept fires."""
    if _scheduler is None:
        logger.error("schedule_followup called before scheduler was built")
        return

    run_at = datetime.now(timezone.utc) + timedelta(hours=hours)
    _scheduler.add_job(
        _send_followup,
        trigger=DateTrigger(run_date=run_at),
        args=(bot, suggestion_id),
        id=f"followup-{suggestion_id}",
        replace_existing=True,
    )
    logger.info(
        f"Scheduled followup for suggestion_id={suggestion_id} at {run_at.isoformat()}"
    )


def _restore_pending_followups(bot: Bot, hours: float) -> None:
    """At startup, re-schedule followups for accepted suggestions whose
    followup hasn't fired yet.
    """
    pending = get_pending_followups(within_hours=24)
    if not pending:
        logger.info("No pending followups to restore")
        return

    for row in pending:
        # response_at is stored as a SQLite timestamp string in UTC.
        # Parse it and compute when the followup should fire.
        response_at = datetime.fromisoformat(row["response_at"]).replace(
            tzinfo=timezone.utc
        )
        run_at = response_at + timedelta(hours=hours)

        if run_at <= datetime.now(timezone.utc):
            # Followup is already overdue — fire it now (next tick).
            run_at = datetime.now(timezone.utc) + timedelta(seconds=5)

        _scheduler.add_job(
            _send_followup,
            trigger=DateTrigger(run_date=run_at),
            args=(bot, row["id"]),
            id=f"followup-{row['id']}",
            replace_existing=True,
        )
        logger.info(
            f"Restored followup for suggestion_id={row['id']} at {run_at.isoformat()}"
        )


def build_scheduler(bot: Bot) -> AsyncIOScheduler:
    """Construct the scheduler with all configured cron jobs + restore pending followups.

    Caller is responsible for calling .start() and .shutdown().
    """
    global _scheduler

    config = _load_config()
    schedule = config["schedule"]
    tz = "America/New_York"

    _scheduler = AsyncIOScheduler(timezone=tz)

    for i, cron in enumerate(schedule.get("weekday_pings", [])):
        _scheduler.add_job(
            _scheduled_ping,
            trigger=CronTrigger.from_crontab(cron, timezone=tz),
            args=(bot, f"weekday[{i}] {cron}"),
            id=f"weekday-ping-{i}",
        )
        logger.info(f"Registered weekday ping {i}: {cron}")

    for i, cron in enumerate(schedule.get("weekend_pings", [])):
        _scheduler.add_job(
            _scheduled_ping,
            trigger=CronTrigger.from_crontab(cron, timezone=tz),
            args=(bot, f"weekend[{i}] {cron}"),
            id=f"weekend-ping-{i}",
        )
        logger.info(f"Registered weekend ping {i}: {cron}")

    # Restore any pending followups from before the bot restarted
    followup_hours = schedule.get("followup_hours", 1.5)
    _restore_pending_followups(bot, followup_hours)
    
    # Hourly sweep for stale unanswered suggestions
    timeout_hours = config["behavior"]["suggestion_timeout_hours"]
    _scheduler.add_job(
        _sweep_stale_job,
        trigger=CronTrigger.from_crontab("0 * * * *", timezone=tz),  # top of every hour
        args=(timeout_hours,),
        id="sweep-stale",
    )
    logger.info(f"Registered sweep-stale job (timeout={timeout_hours}h)")
    
    # Daily heartbeat
    heartbeat_cron = schedule.get("heartbeat")
    if heartbeat_cron:
        _scheduler.add_job(
            _heartbeat_job,
            trigger=CronTrigger.from_crontab(heartbeat_cron, timezone=tz),
            args=(bot,),
            id="heartbeat",
        )
        logger.info(f"Registered heartbeat: {heartbeat_cron}")

    return _scheduler