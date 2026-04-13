"""
Scraper entry point.

Usage:
    uv run run_scraper.py                    # scrapes Japan (default)
    uv run run_scraper.py --region japan
    uv run run_scraper.py --region japan --debug
"""

import asyncio
import argparse
import os
from pathlib import Path

from dotenv import load_dotenv
from playwright.async_api import async_playwright
from pymongo import MongoClient

from db.client import ensure_indexes
from scrapers.base import save_jobs
from scrapers.japan.rikunabi import RikunabiScraper
from scrapers.korea.wanted import WantedScraper
from scrapers.thailand.jobsdb import JobsDBScraper

load_dotenv()

# Map each region to its (scraper class, browser locale) pair.
REGION_CONFIG: dict[str, tuple] = {
    "japan":    (RikunabiScraper, "ja-JP"),
    "korea":    (WantedScraper,   "ko-KR"),
    "thailand": (JobsDBScraper,   "en-US"),
}

CHROME_UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/120.0.0.0 Safari/537.36"
)

# Script injected into every page before any JS runs — hides Playwright's
# automation fingerprint from bot-detection systems like Saramin's.
ANTI_DETECTION_SCRIPT = """
    Object.defineProperty(navigator, 'webdriver', {get: () => undefined});
    window.chrome = { runtime: {} };
    Object.defineProperty(navigator, 'plugins', {get: () => [1, 2, 3]});
    Object.defineProperty(navigator, 'languages', {get: () => ['ko-KR', 'ko', 'en-US', 'en']});
"""


async def run(region: str, mongo_client: MongoClient):
    scraper_cls, locale = REGION_CONFIG[region]
    data_dir = f"data/{region}"
    Path(data_dir).mkdir(parents=True, exist_ok=True)

    async with async_playwright() as pw:
        browser = await pw.chromium.launch(
            headless=True,
            args=[
                "--no-sandbox",
                "--disable-setuid-sandbox",
                # Prevents sites from detecting Chrome is automation-controlled
                "--disable-blink-features=AutomationControlled",
            ],
        )
        context = await browser.new_context(
            locale=locale,
            user_agent=CHROME_UA,
            viewport={"width": 1920, "height": 1080},
        )
        await context.add_init_script(ANTI_DETECTION_SCRIPT)

        scraper = scraper_cls(await context.new_page())
        jobs    = await scraper.scrape()
        await browser.close()

    save_jobs(jobs, output_dir=data_dir, region=region, mongo_client=mongo_client)


def main():
    parser = argparse.ArgumentParser(description="JobAgent scraper")
    parser.add_argument(
        "--region",
        choices=list(REGION_CONFIG.keys()),
        default="japan",
        help="Region to scrape (default: japan)",
    )
    # parse_known_args so --debug passes through to sys.argv (read by scrapers/base.py)
    args, _ = parser.parse_known_args()

    mongo_uri    = os.getenv("MONGODB_URI", "")
    mongo_client = MongoClient(mongo_uri) if mongo_uri else None
    if mongo_client:
        ensure_indexes()

    asyncio.run(run(args.region, mongo_client))

    if mongo_client:
        mongo_client.close()


if __name__ == "__main__":
    main()
