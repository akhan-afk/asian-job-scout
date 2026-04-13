"""
Pipeline entry point — manually trigger the full scrape + summarize + notify pipeline.

Usage:
    uv run run_pipeline.py                            # all regions, respect 3-day interval
    uv run run_pipeline.py --regions korea thailand   # specific regions only
    uv run run_pipeline.py --force                    # skip freshness check, always run
    uv run run_pipeline.py --interval 4               # treat data as stale after 4 days
    uv run run_pipeline.py --no-notify                # skip Telegram notifications

Notifications:
    Subscribed users are notified automatically if BOT_TOKEN is set in .env.
    Pass --no-notify to suppress this (e.g. for test runs).
"""

import asyncio
import argparse
import os
import sys

from dotenv import load_dotenv
from pymongo import MongoClient

from db.client import ensure_indexes, set_client
from pipeline.orchestrator import REGION_CONFIG, run_pipeline

load_dotenv()


async def _run(args, mongo_client):
    """Async wrapper so the Bot and pipeline share the same event loop."""

    # ── Telegram Bot for notifications ───────────────────────────────────────
    bot       = None
    bot_token = os.getenv("BOT_TOKEN", "")

    if not args.no_notify and bot_token:
        try:
            from telegram import Bot
            bot = Bot(token=bot_token)
            # Verify the token is valid before running the whole pipeline
            me = await bot.get_me()
            print(f"[pipeline] Notifications enabled → @{me.username}")
        except Exception as exc:
            print(f"[pipeline] Could not initialise bot ({exc}) — notifications disabled.")
            bot = None
    elif args.no_notify:
        print("[pipeline] --no-notify flag set — skipping notifications.")
    else:
        print("[pipeline] BOT_TOKEN not set — skipping notifications.")

    # ── Run pipeline ──────────────────────────────────────────────────────────
    try:
        results = await run_pipeline(
            regions       = args.regions,
            force_refresh = args.force,
            bot           = bot,
            mongo_client  = mongo_client,
            interval_days = args.interval,
        )
    finally:
        # Cleanly shut down the httpx session inside the Bot
        if bot is not None:
            try:
                await bot.shutdown()
            except Exception:
                pass

    return results


def main():
    parser = argparse.ArgumentParser(description="JobAgent pipeline runner")
    parser.add_argument(
        "--regions",
        nargs   = "+",
        choices = list(REGION_CONFIG.keys()),
        default = list(REGION_CONFIG.keys()),
        help    = "Regions to run (default: all)",
    )
    parser.add_argument(
        "--force",
        action = "store_true",
        help   = "Force refresh even if data is already fresh",
    )
    parser.add_argument(
        "--interval",
        type    = int,
        default = int(os.getenv("PIPELINE_INTERVAL_DAYS", "3")),
        help    = "Freshness threshold in days (default: PIPELINE_INTERVAL_DAYS or 3)",
    )
    parser.add_argument(
        "--no-notify",
        action = "store_true",
        help   = "Skip Telegram notifications even if BOT_TOKEN is set",
    )
    args = parser.parse_args()

    # ── MongoDB ───────────────────────────────────────────────────────────────
    mongo_uri    = os.getenv("MONGODB_URI", "")
    mongo_client = MongoClient(mongo_uri) if mongo_uri else None

    if mongo_client:
        set_client(mongo_client)
        ensure_indexes()
    else:
        print("[warning] MONGODB_URI not set — data will only be saved to txt files.")

    print(f"[pipeline] Regions:  {', '.join(args.regions)}")
    print(f"[pipeline] Interval: {args.interval} day(s)")
    print(f"[pipeline] Force:    {args.force}\n")

    # ── Execute ───────────────────────────────────────────────────────────────
    results = asyncio.run(_run(args, mongo_client))

    if mongo_client:
        mongo_client.close()

    if not results:
        print("\n[pipeline] All regions were already up to date — nothing scraped.")
        sys.exit(0)

    print("\n[pipeline] Results:")
    all_ok = True
    for r in results:
        if r.get("error"):
            print(f"  ✗ {r['region']}: {r['error']}")
            all_ok = False
        else:
            print(f"  ✓ {r['region']}: {r['jobs']} jobs scraped, {r['summaries']} summaries saved")

    sys.exit(0 if all_ok else 1)


if __name__ == "__main__":
    main()
