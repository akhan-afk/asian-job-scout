import asyncio
import os

from dotenv import load_dotenv
from pymongo import MongoClient
from telegram.error import TelegramError
from telegram.ext import (
    Application,
    CommandHandler,
    MessageHandler,
    filters,
)

from bot.agent import MISTRAL_MODEL, MONGODB_URI, create_agent
from db.client import ensure_indexes, set_client
from bot.handlers import (
    cmd_clear,
    cmd_help,
    cmd_jobs,
    cmd_search,
    cmd_start,
    cmd_subscribe,
    cmd_unsubscribe,
    handle_message,
)
from bot.health import self_ping_loop, start_health_server
from pipeline.orchestrator import run_pipeline_scheduler

load_dotenv()

BOT_TOKEN         = os.getenv("BOT_TOKEN", "")
PIPELINE_INTERVAL = int(os.getenv("PIPELINE_INTERVAL_DAYS", "3"))


async def test_connection(app: Application, mongo_client: MongoClient):
    try:
        me = await app.bot.get_me()
        print(f"[ok] Telegram connected: @{me.username}")
    except TelegramError as e:
        # Log but don't exit — health server stays up so Render/UptimeRobot
        # don't register a crash. The bot will retry on the next poll cycle.
        print(f"[warn] Telegram connection check failed: {e}")

    try:
        mongo_client.admin.command("ping")
        print("[ok] MongoDB Atlas connected")
    except Exception as e:
        print(f"[warn] MongoDB connection check failed: {e} — will retry on first request")

    print(f"[ok] Mistral model: {MISTRAL_MODEL}")
    print("[ready] Bot is running. Send it a message on Telegram!\n")


async def main():
    # Start health-check server immediately so Render sees a bound port.
    # On local runs this is a no-op (binds to 8080, daemon thread exits with process).
    start_health_server()

    if not MONGODB_URI:
        print("[error] MONGODB_URI is not set — bot will start but database features will fail.")
        print("[error] Set MONGODB_URI in Render's environment variables and redeploy.")

    mongo_client = MongoClient(MONGODB_URI)

    # Share the client with db.client so bot/tools.py queries the same connection
    set_client(mongo_client)
    ensure_indexes()

    agent = create_agent(mongo_client)

    app = Application.builder().token(BOT_TOKEN).build()

    # Make the agent available to all handlers via context.bot_data
    app.bot_data["agent"] = agent

    app.add_handler(CommandHandler("start",       cmd_start))
    app.add_handler(CommandHandler("help",        cmd_help))
    app.add_handler(CommandHandler("jobs",        cmd_jobs))
    app.add_handler(CommandHandler("search",      cmd_search))
    app.add_handler(CommandHandler("subscribe",   cmd_subscribe))
    app.add_handler(CommandHandler("unsubscribe", cmd_unsubscribe))
    app.add_handler(CommandHandler("clear",       cmd_clear))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message))

    await app.initialize()
    await test_connection(app, mongo_client)
    await app.start()
    await app.updater.start_polling(drop_pending_updates=True)

    # Keep Render's free tier awake by pinging our own public URL every 4 min.
    # This goes through Render's reverse proxy and resets the inactivity timer,
    # so the service never sleeps even if UptimeRobot backs off its interval.
    asyncio.create_task(self_ping_loop())

    # Launch pipeline scheduler as a background task in the same event loop.
    asyncio.create_task(
        run_pipeline_scheduler(
            bot           = app.bot,
            mongo_client  = mongo_client,
            interval_days = PIPELINE_INTERVAL,
        )
    )

    print(f"[pipeline] Auto-refresh every {PIPELINE_INTERVAL} day(s). Use /subscribe for alerts.")
    print("Press Ctrl+C to stop.")
    try:
        await asyncio.Event().wait()
    except (KeyboardInterrupt, SystemExit):
        pass
    finally:
        await app.updater.stop()
        await app.stop()
        await app.shutdown()
        mongo_client.close()


if __name__ == "__main__":
    asyncio.run(main())
