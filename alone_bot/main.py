"""alone-bot entrypoint."""

import asyncio
import logging

from alone_bot.bot import build_application
from alone_bot.db import init_db, seed_activities
from alone_bot.scheduler import build_scheduler


logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)
logging.getLogger("httpx").setLevel(logging.WARNING)
logging.getLogger("telegram").setLevel(logging.INFO)
logging.getLogger("apscheduler").setLevel(logging.INFO)

logger = logging.getLogger(__name__)


async def main_async() -> None:
    """Async entrypoint — runs the bot and scheduler on the same loop."""
    logger.info("Initializing database...")
    init_db()

    seeded = seed_activities()
    if seeded > 0:
        logger.info(f"Seeded {seeded} activities into empty database.")
    else:
        logger.info("Database already seeded, skipping.")

    app = build_application()
    scheduler = build_scheduler(app.bot)

    # Initialize PTB, start scheduler, start polling. Order matters:
    # the scheduler depends on app.bot being usable, which requires
    # initialize() to have run.
    await app.initialize()
    scheduler.start()
    await app.start()
    await app.updater.start_polling(allowed_updates=["message", "callback_query"])

    logger.info("Bot + scheduler running. Idling.")

    # Keep the process alive forever. asyncio.Event() never set =
    # await blocks until cancellation (SIGTERM from docker stop).
    stop_event = asyncio.Event()
    try:
        await stop_event.wait()
    finally:
        logger.info("Shutting down...")
        await app.updater.stop()
        await app.stop()
        scheduler.shutdown()
        await app.shutdown()


def main() -> None:
    asyncio.run(main_async())


if __name__ == "__main__":
    main()