"""
MongoDB client singleton and collection accessors.

Usage
─────
Typical entry points (run_scraper.py, run_summarizer.py):

    from db.client import get_client, ensure_indexes
    client = get_client()          # lazy-initialised from MONGODB_URI env var
    ensure_indexes()               # idempotent — safe to call every run

Bot (bot/main.py) — reuse the already-created MongoClient:

    from db.client import set_client, ensure_indexes
    set_client(mongo_client)       # share the same connection with LangGraph
    ensure_indexes()

Tools / anywhere else:

    from db.client import get_jobs_collection, get_summaries_collection
    docs = list(get_summaries_collection().find({"region": "korea"}))
"""

from __future__ import annotations

import os

from dotenv import load_dotenv
from pymongo import ASCENDING, DESCENDING, MongoClient
from pymongo.collection import Collection
from pymongo.database import Database

load_dotenv()

DB_NAME     = "jobagent"
MONGODB_URI = os.getenv("MONGODB_URI", "")

_client: MongoClient | None = None


# ─────────────────────────────────────────
#  Client management
# ─────────────────────────────────────────

def set_client(client: MongoClient) -> None:
    """Inject an already-created MongoClient (used by the bot to avoid two connections)."""
    global _client
    _client = client


def get_client() -> MongoClient:
    """Return the singleton MongoClient, creating it lazily if needed."""
    global _client
    if _client is None:
        if not MONGODB_URI:
            raise RuntimeError(
                "MONGODB_URI is not set. Add it to your .env file."
            )
        _client = MongoClient(MONGODB_URI)
    return _client


def get_db() -> Database:
    return get_client()[DB_NAME]


# ─────────────────────────────────────────
#  Collection accessors
# ─────────────────────────────────────────

def get_jobs_collection() -> Collection:
    return get_db()["jobs"]


def get_summaries_collection() -> Collection:
    return get_db()["summaries"]


def get_users_collection() -> Collection:
    return get_db()["users"]


# ─────────────────────────────────────────
#  Index management
# ─────────────────────────────────────────

def ensure_indexes() -> None:
    """
    Create all required indexes. Safe to call on every startup — MongoDB
    skips creation if the index already exists.
    """
    jobs      = get_jobs_collection()
    summaries = get_summaries_collection()

    # jobs ── unique per listing, fast region+date lookups
    jobs.create_index([("job_id", ASCENDING)], unique=True)
    jobs.create_index([("region", ASCENDING), ("scraped_date", DESCENDING)])
    jobs.create_index([("is_active", ASCENDING)])

    # summaries ── unique per job, fast region+date+tag lookups
    summaries.create_index([("job_id", ASCENDING)], unique=True)
    summaries.create_index([("region", ASCENDING), ("summarized_date", DESCENDING)])
    summaries.create_index([("tags", ASCENDING)])
    summaries.create_index([("remote", ASCENDING)])
    summaries.create_index([("stack", ASCENDING)])

    # users ── unique per Telegram user, fast subscription lookups
    users = get_users_collection()
    users.create_index([("user_id", ASCENDING)], unique=True)
    users.create_index([("subscribed", ASCENDING)])

    # Phase 2: Atlas Vector Search index on "embedding" must be created via
    # the Atlas UI or API — pymongo cannot create vector search indexes.

    print("[db] Indexes verified.")
