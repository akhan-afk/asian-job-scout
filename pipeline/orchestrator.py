"""
Pipeline orchestrator — LangGraph StateGraph that automates:
  1. check_freshness  — decide which regions need a refresh
  2. scrape_summarize — scrape + summarize each stale region (parallel fan-out)
  3. notify_users     — Telegram broadcast to subscribed users (fan-in)

The pipeline runs:
  • Automatically as a background asyncio task inside the bot process
  • Manually via `uv run run_pipeline.py` (no Telegram notifications)

External dependencies (bot object, mongo client, API keys) are injected via
LangGraph's RunnableConfig so the graph state stays JSON-serializable.
"""

from __future__ import annotations

import asyncio
import operator
import os
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Annotated, Optional

from langchain_core.runnables import RunnableConfig
from langgraph.graph import END, START, StateGraph
from langgraph.types import Send
from playwright.async_api import async_playwright
from typing_extensions import TypedDict

from db.client import get_jobs_collection, get_users_collection
from run_scraper import ANTI_DETECTION_SCRIPT, CHROME_UA, REGION_CONFIG
from scrapers.base import save_jobs
from summarizers.summarizer import save_summaries, summarise_jobs

# ─────────────────────────────────────────
#  State
# ─────────────────────────────────────────

class PipelineState(TypedDict):
    regions:       list[str]   # which regions to consider
    force_refresh: bool        # skip freshness check if True
    stale_regions: list[str]   # computed by check_freshness
    # fan-in accumulator: each parallel branch appends one result dict
    results: Annotated[list[dict], operator.add]


# ─────────────────────────────────────────
#  Helpers
# ─────────────────────────────────────────

def _is_stale(region: str, interval_days: int, force: bool) -> bool:
    """Return True if the region needs a fresh scrape."""
    if force:
        return True
    latest = get_jobs_collection().find_one(
        {"region": region}, sort=[("scraped_date", -1)]
    )
    if latest is None:
        return True
    cutoff = date.today() - timedelta(days=interval_days - 1)
    return latest["scraped_date"] < str(cutoff)


def _job_to_dict(job) -> dict:
    """
    Convert a JobListing object to the dict format that summarise_jobs() expects.
    Reuses translations already cached during save_jobs() — no extra API calls.
    """
    return {
        "SOURCE":           job.source,
        "URL":              job.url,
        "TITLE [EN]":       job._tr(job.title,       "title"),
        "COMPANY [EN]":     job._tr(job.company,     "company"),
        "LOCATION [EN]":    job._tr(job.location,    "location"),
        "SALARY [EN]":      job._tr(job.salary,      "salary"),
        "DEADLINE [EN]":    job._tr(job.deadline,    "deadline"),
        "DESCRIPTION [EN]": job._tr(job.description, "description")[:1500],
    }


# ─────────────────────────────────────────
#  Nodes
# ─────────────────────────────────────────

async def check_freshness(state: PipelineState, config: RunnableConfig) -> dict:
    """Determine which regions have stale data."""
    interval_days = config.get("configurable", {}).get("interval_days", 3)
    force         = state.get("force_refresh", False)

    stale = [
        r for r in state["regions"]
        if _is_stale(r, interval_days, force)
    ]

    if stale:
        print(f"[pipeline] Stale regions: {', '.join(stale)}")
    else:
        print("[pipeline] All regions are up to date — nothing to do.")

    return {"stale_regions": stale}


def route_after_freshness(state: PipelineState):
    """Fan out with Send for each stale region, or go straight to END if all fresh."""
    if not state.get("stale_regions"):
        return END
    return [
        Send("scrape_summarize", {"region": r, "results": []})
        for r in state["stale_regions"]
    ]


async def scrape_summarize(state: dict, config: RunnableConfig) -> dict:
    """
    Scrape + summarize a single region. Each parallel Send invocation runs
    this node independently with its own Playwright browser instance.
    """
    region       = state["region"]
    cfg          = config.get("configurable", {})
    mongo_client = cfg.get("mongo_client")
    api_key      = cfg.get("mistral_api_key", os.getenv("MISTRAL_API_KEY", ""))
    model        = cfg.get("mistral_model",   os.getenv("MISTRAL_MODEL", "mistral-small-latest"))
    data_dir     = f"data/{region}"

    Path(data_dir).mkdir(parents=True, exist_ok=True)
    print(f"[pipeline] [{region}] Starting scrape...")

    result = {"region": region, "jobs": 0, "summaries": 0, "error": None}

    try:
        scraper_cls, locale = REGION_CONFIG[region]

        async with async_playwright() as pw:
            browser = await pw.chromium.launch(
                headless=True,
                args=[
                    "--no-sandbox",
                    "--disable-setuid-sandbox",
                    "--disable-blink-features=AutomationControlled",
                ],
            )
            context = await browser.new_context(
                locale     = locale,
                user_agent = CHROME_UA,
                viewport   = {"width": 1920, "height": 1080},
            )
            await context.add_init_script(ANTI_DETECTION_SCRIPT)
            scraper = scraper_cls(await context.new_page())
            jobs    = await scraper.scrape()
            await browser.close()

        # Save to txt + MongoDB (translations cached inside JobListing objects)
        save_jobs(jobs, output_dir=data_dir, region=region, mongo_client=mongo_client)
        result["jobs"] = len(jobs)

        print(f"[pipeline] [{region}] Scraped {len(jobs)} jobs. Summarizing...")

        # Convert JobListing objects → dicts for summariser (no txt re-read needed)
        job_dicts = [_job_to_dict(j) for j in jobs]
        summaries = summarise_jobs(
            job_dicts,
            api_key      = api_key,
            model        = model,
            region       = region,
            mongo_client = mongo_client,
        )
        save_summaries(summaries, output_dir=data_dir)
        result["summaries"] = len(summaries)

        print(f"[pipeline] [{region}] Done — {len(jobs)} jobs, {len(summaries)} summaries.")

    except Exception as e:
        result["error"] = str(e)
        print(f"[pipeline] [{region}] Error: {e}")

    return {"results": [result]}


async def notify_users(state: PipelineState, config: RunnableConfig) -> dict:
    """Broadcast a Telegram message to all subscribed users."""
    cfg     = config.get("configurable", {})
    bot     = cfg.get("bot")
    results = state.get("results", [])

    successful = [r for r in results if not r.get("error")]
    failed     = [r for r in results if r.get("error")]

    for r in failed:
        print(f"[pipeline] [{r['region']}] Failed: {r['error']}")

    if not successful:
        print("[pipeline] No successful regions — skipping notifications.")
        return {}

    if bot is None:
        print("[pipeline] No bot provided — skipping Telegram notifications.")
        return {}

    try:
        users = list(get_users_collection().find({"subscribed": True}))
        if not users:
            print("[pipeline] No subscribed users.")
            return {}

        lines = ["*New job listings are available!* 🗂\n"]
        for r in successful:
            lines.append(f"• *{r['region'].title()}:* {r['summaries']} new listings")
        lines.append("\nAsk me about any region to see the latest listings.")
        message = "\n".join(lines)

        notified = 0
        for user in users:
            try:
                await bot.send_message(
                    chat_id    = user["user_id"],
                    text       = message,
                    parse_mode = "Markdown",
                )
                # Update last_notified timestamp
                get_users_collection().update_one(
                    {"user_id": user["user_id"]},
                    {"$set": {"last_notified": datetime.now(timezone.utc)}},
                )
                notified += 1
            except Exception as e:
                print(f"[pipeline] Failed to notify user {user['user_id']}: {e}")

        print(f"[pipeline] Notified {notified}/{len(users)} subscribed user(s).")

    except Exception as e:
        print(f"[pipeline] Notification error: {e}")

    return {}


# ─────────────────────────────────────────
#  Graph
# ─────────────────────────────────────────

def _build_pipeline():
    graph = StateGraph(PipelineState)

    graph.add_node("check_freshness",  check_freshness)
    graph.add_node("scrape_summarize", scrape_summarize)
    graph.add_node("notify_users",     notify_users)

    graph.add_edge(START, "check_freshness")
    graph.add_conditional_edges(
        "check_freshness",
        route_after_freshness,
        ["scrape_summarize", END],
    )
    # All parallel scrape_summarize branches fan-in here automatically
    graph.add_edge("scrape_summarize", "notify_users")
    graph.add_edge("notify_users", END)

    return graph.compile()  # no checkpointer — pipeline is stateless/one-shot


_pipeline = _build_pipeline()


# ─────────────────────────────────────────
#  Public API
# ─────────────────────────────────────────

async def run_pipeline(
    regions:       list[str] | None = None,
    force_refresh: bool             = False,
    bot                             = None,
    mongo_client                    = None,
    interval_days: int              = 3,
) -> list[dict]:
    """
    Run the full pipeline and return a list of per-region result dicts.

    Args:
        regions:       Regions to check (default: all three)
        force_refresh: Skip freshness check and always scrape
        bot:           Telegram Bot object for notifications (None = skip)
        mongo_client:  MongoClient to use for all DB writes
        interval_days: How many days of freshness to require before re-scraping
    """
    if regions is None:
        regions = list(REGION_CONFIG.keys())

    result = await _pipeline.ainvoke(
        {
            "regions":       regions,
            "force_refresh": force_refresh,
            "stale_regions": [],
            "results":       [],
        },
        config={
            "configurable": {
                "bot":            bot,
                "mongo_client":   mongo_client,
                "interval_days":  interval_days,
                "mistral_api_key": os.getenv("MISTRAL_API_KEY", ""),
                "mistral_model":   os.getenv("MISTRAL_MODEL", "mistral-small-latest"),
            }
        },
    )
    return result.get("results", [])


async def run_pipeline_scheduler(
    bot          = None,
    mongo_client = None,
    interval_days: int = 3,
) -> None:
    """
    Background asyncio task: run the pipeline on startup (if data is stale)
    then sleep and repeat every `interval_days` days.

    Designed to be launched with asyncio.create_task() inside bot/main.py.
    """
    print(f"[pipeline] Scheduler started (interval: {interval_days} day(s)).")

    while True:
        try:
            # Find the oldest scrape across all regions
            most_recent: date | None = None
            col = get_jobs_collection()
            for region in REGION_CONFIG:
                latest = col.find_one({"region": region}, sort=[("scraped_date", -1)])
                if latest:
                    d = date.fromisoformat(latest["scraped_date"])
                    if most_recent is None or d > most_recent:
                        most_recent = d

            days_since = (date.today() - most_recent).days if most_recent else interval_days + 1

        except Exception as e:
            print(f"[pipeline] Scheduler freshness check error: {e}")
            days_since = interval_days + 1

        if days_since >= interval_days:
            print(f"[pipeline] Data is {days_since} day(s) old — running pipeline...")
            try:
                results = await run_pipeline(
                    bot           = bot,
                    mongo_client  = mongo_client,
                    interval_days = interval_days,
                )
                ok  = [r for r in results if not r.get("error")]
                err = [r for r in results if r.get("error")]
                print(
                    f"[pipeline] Completed: {len(ok)} succeeded"
                    + (f", {len(err)} failed" if err else "")
                )
            except Exception as e:
                print(f"[pipeline] Run error: {e}")

            sleep_secs = interval_days * 24 * 3600

        else:
            remaining  = interval_days - days_since
            next_run   = date.today() + timedelta(days=remaining)
            sleep_secs = remaining * 24 * 3600
            print(f"[pipeline] Data is fresh. Next run in {remaining} day(s) ({next_run}).")

        await asyncio.sleep(sleep_secs)
