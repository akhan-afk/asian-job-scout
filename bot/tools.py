"""
LangGraph tool definitions available to the job-search agent.

Data source priority:
  1. MongoDB `summaries` collection (primary — rich metadata, cross-date)
  2. Text file fallback (used automatically if MongoDB has no data yet)
"""

from __future__ import annotations

from datetime import date
from pathlib import Path
from typing import Optional

from langchain_core.tools import tool

# ─────────────────────────────────────────
#  Config
# ─────────────────────────────────────────

SEPARATOR = "=" * 80

DATA_REGIONS: dict[str, str] = {
    "japan":    "data/japan",
    "korea":    "data/korea",
    "thailand": "data/thailand",
}


# ─────────────────────────────────────────
#  MongoDB helpers
# ─────────────────────────────────────────

def _mongo_summaries(region: Optional[str] = None) -> list[dict]:
    """
    Query MongoDB for the most recent summaries.
    Returns [] if the collection is unreachable or empty.
    """
    try:
        from db.client import get_summaries_collection
        col   = get_summaries_collection()
        query = {}
        if region:
            query["region"] = region

        # Find the most recent summarized_date available
        latest = col.find_one(query, sort=[("summarized_date", -1)])
        if not latest:
            return []

        query["summarized_date"] = latest["summarized_date"]
        docs = list(col.find(query, sort=[("summarized_date", -1)]))

        return [
            {
                "source":    d.get("source", ""),
                "url":       d.get("url", ""),
                "body":      d.get("summary", ""),
                "region":    d.get("region", ""),
                "data_date": d.get("summarized_date", ""),
                "tags":      d.get("tags", []),
                "stack":     d.get("stack", []),
                "remote":    d.get("remote"),
            }
            for d in docs
        ]
    except Exception:
        return []


# ─────────────────────────────────────────
#  Text-file fallback helpers
# ─────────────────────────────────────────

def _load_summaries_txt(filepath: str) -> list[dict]:
    path = Path(filepath)
    if not path.exists():
        return []
    text    = path.read_text(encoding="utf-8")
    blocks  = text.split(SEPARATOR)
    results = []
    for block in blocks:
        block = block.strip()
        if not block or block.startswith("Job Summaries"):
            continue
        lines      = block.splitlines()
        source     = ""
        url        = ""
        body_lines = []
        for line in lines:
            if line.startswith("SOURCE:"):
                source = line.replace("SOURCE:", "").strip()
            elif line.startswith("URL:"):
                url = line.replace("URL:", "").strip()
            elif line.strip():
                body_lines.append(line)
        body = "\n".join(body_lines).strip()
        if body:
            results.append({"source": source, "url": url, "body": body,
                             "tags": [], "stack": [], "remote": None})
    return results


def _latest_txt_file(data_dir: str) -> tuple[Path | None, str | None]:
    """Return the most recent summaries_*.txt and its date string."""
    today_file = Path(data_dir) / f"summaries_{date.today()}.txt"
    if today_file.exists():
        return today_file, str(date.today())
    candidates = sorted(Path(data_dir).glob("summaries_*.txt"), reverse=True)
    if candidates:
        date_str = candidates[0].stem.replace("summaries_", "")
        return candidates[0], date_str
    return None, None


def _txt_summaries(region: Optional[str] = None) -> list[dict]:
    regions = {region: DATA_REGIONS[region]} if region and region in DATA_REGIONS else DATA_REGIONS
    all_summaries: list[dict] = []
    for reg, data_dir in regions.items():
        filepath, date_str = _latest_txt_file(data_dir)
        if filepath is None:
            continue
        for s in _load_summaries_txt(str(filepath)):
            s["region"]    = reg
            s["data_date"] = date_str
            all_summaries.append(s)
    return all_summaries


def _get_summaries(region: Optional[str] = None) -> list[dict]:
    """Try MongoDB first; fall back to text files transparently."""
    docs = _mongo_summaries(region)
    if docs:
        return docs
    return _txt_summaries(region)


# ─────────────────────────────────────────
#  Tools
# ─────────────────────────────────────────

@tool
def get_regions() -> str:
    """
    List all available job regions and the date of the most recent summaries.
    Call this when the user asks what regions or countries are available.
    """
    try:
        from db.client import get_summaries_collection
        col   = get_summaries_collection()
        lines = []
        for reg in DATA_REGIONS:
            latest = col.find_one({"region": reg}, sort=[("summarized_date", -1)])
            if latest:
                d = latest["summarized_date"]
                flag = "✓" if d == str(date.today()) else f"⚠ last scraped {d}"
                lines.append(f"- {reg}: {flag}")
            else:
                lines.append(f"- {reg}: ✗ no data yet")
        return "Available regions:\n" + "\n".join(lines)
    except Exception:
        # Txt fallback
        lines = []
        for reg, data_dir in DATA_REGIONS.items():
            _, date_str = _latest_txt_file(data_dir)
            if date_str is None:
                lines.append(f"- {reg}: ✗ no data yet")
            elif date_str == str(date.today()):
                lines.append(f"- {reg}: ✓ up to date ({date_str})")
            else:
                lines.append(f"- {reg}: ⚠ last scraped {date_str}")
        return "Available regions:\n" + "\n".join(lines)


@tool
def list_jobs(region: str) -> str:
    """
    Return the latest available job summaries for a specific region.
    Use this when the user asks to see jobs in a country without any filter keyword.
    Returns whatever data is currently in the database — not restricted to today's date.
    Accepted values for region: japan, korea, thailand.
    """
    region = region.lower().strip()
    if region not in DATA_REGIONS:
        return f"Unknown region '{region}'. Choose from: {', '.join(DATA_REGIONS)}."

    summaries = _get_summaries(region)
    if not summaries:
        return (
            f"No job listings are currently available for {region}. "
            "The database is empty for this region — the pipeline needs to be run to fetch fresh data."
        )

    data_date = summaries[0].get("data_date", "")
    freshness = f" (data from {data_date})" if data_date else ""
    parts     = [f"*{len(summaries)} jobs in {region}{freshness}*\n"]
    for s in summaries:
        parts.append(f"{s['body']}\nURL: {s['url']}\n")
    return "\n---\n".join(parts)


@tool
def search_jobs(query: str, region: Optional[str] = None) -> str:
    """
    Search job summaries for a keyword, skill, role, or topic.
    Examples: 'Python', 'remote', 'marketing', 'Java backend'.
    Optionally restrict to one region (japan, korea, thailand).
    Leave region blank to search all regions.
    """
    query  = query.lower().strip()
    region = region.lower().strip() if region else None

    if region and region not in DATA_REGIONS:
        return f"Unknown region '{region}'. Choose from: {', '.join(DATA_REGIONS)}."

    summaries = _get_summaries(region)

    # Search body text AND tags/stack for richer matching
    def _matches(s: dict) -> bool:
        haystack = s["body"].lower()
        haystack += " " + " ".join(s.get("tags", []))
        haystack += " " + " ".join(s.get("stack", []))
        return query in haystack

    matches = [s for s in summaries if _matches(s)]

    if not matches:
        scope = f" in {region}" if region else " across all regions"
        return f"No jobs matching '{query}'{scope} in the available data."

    dates     = sorted({s.get("data_date", "") for s in matches if s.get("data_date")})
    date_note = f" (data from: {', '.join(dates)})" if dates else ""
    parts     = [f"*{len(matches)} job(s) matching '{query}'{date_note}*\n"]
    for s in matches:
        reg_label = f"[{s.get('region', '')}] " if not region else ""
        remote_badge = " 🌐 remote" if s.get("remote") else ""
        parts.append(f"{reg_label}{s['body']}{remote_badge}\nURL: {s['url']}\n")
    return "\n---\n".join(parts)


@tool
def trigger_refresh(region: str) -> str:
    """
    Called when the user asks to refresh or update job listings for a region.
    Accepted values for region: japan, korea, thailand.
    """
    region = region.lower().strip()
    if region not in DATA_REGIONS:
        return f"Unknown region '{region}'. Choose from: {', '.join(DATA_REGIONS)}."

    return (
        f"Refreshing job data is not available through the bot — "
        f"the scraper requires a local environment with a browser installed. "
        f"The database will be updated the next time the pipeline is run manually. "
        f"In the meantime, I can show you whatever listings are currently available for {region}."
    )


@tool
def check_subscription_status(user_id: int) -> str:
    """
    Check whether a Telegram user is subscribed to job alert notifications.
    Call this when the user asks if they are subscribed, their notification status,
    or anything about their job alert subscription with this bot.
    Use the current user's Telegram ID provided in the system context.
    """
    try:
        from db.client import get_users_collection
        user = get_users_collection().find_one({"user_id": user_id})
        if user is None or not user.get("subscribed"):
            return (
                "You are *not subscribed* to job notifications. "
                "Send /subscribe to start receiving alerts when new listings are added."
            )
        regions      = user.get("regions", [])
        last_notified = user.get("last_notified")
        last_str     = f" Last alert sent: {last_notified.strftime('%Y-%m-%d')}" if last_notified else ""
        return (
            f"You *are subscribed* to job alerts for: {', '.join(regions)}.{last_str} "
            "Send /unsubscribe if you want to stop."
        )
    except Exception as e:
        return f"Could not check subscription status: {e}"


ALL_TOOLS = [get_regions, list_jobs, search_jobs, trigger_refresh, check_subscription_status]
