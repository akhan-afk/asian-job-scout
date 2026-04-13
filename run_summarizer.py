"""
Summarizer entry point.

Usage:
    uv run run_summarizer.py                              # summarizes Japan jobs (most recent file)
    uv run run_summarizer.py --region japan
    uv run run_summarizer.py --region japan --file data/japan/jobs_2026-03-05.txt
"""

import os
import sys
import argparse
from pathlib import Path

from dotenv import load_dotenv
from pymongo import MongoClient

from db.client import ensure_indexes
from summarizers.summarizer import parse_jobs_file, summarise_jobs, save_summaries

load_dotenv()

MISTRAL_API_KEY = os.getenv("MISTRAL_API_KEY", "")
MISTRAL_MODEL   = os.getenv("MISTRAL_MODEL", "mistral-small-latest")
MONGODB_URI     = os.getenv("MONGODB_URI", "")

REGIONS = ["japan", "korea", "thailand"]


def _latest_jobs_file(data_dir: str) -> str | None:
    """Return path to the most recent jobs_*.txt file in data_dir."""
    candidates = sorted(Path(data_dir).glob("jobs_*.txt"), reverse=True)
    return str(candidates[0]) if candidates else None


def main():
    parser = argparse.ArgumentParser(description="JobAgent summarizer")
    parser.add_argument(
        "--region",
        choices=REGIONS,
        default="japan",
        help="Region to summarize (default: japan)",
    )
    parser.add_argument(
        "--file",
        default=None,
        help="Path to a specific jobs file (default: most recent file for the region)",
    )
    args = parser.parse_args()

    data_dir  = f"data/{args.region}"
    jobs_file = args.file or _latest_jobs_file(data_dir)

    if not jobs_file or not Path(jobs_file).exists():
        print(f"[error] No jobs file found in {data_dir}")
        print(f"        Run the scraper first: uv run run_scraper.py --region {args.region}")
        sys.exit(1)

    mongo_client = MongoClient(MONGODB_URI) if MONGODB_URI else None
    if mongo_client:
        ensure_indexes()

    print(f"[summariser] Reading {jobs_file}...")
    jobs = parse_jobs_file(jobs_file)
    print(f"[summariser] Found {len(jobs)} job listings. Sending to Mistral...\n")

    summaries = summarise_jobs(
        jobs,
        api_key      = MISTRAL_API_KEY,
        model        = MISTRAL_MODEL,
        region       = args.region,
        mongo_client = mongo_client,
    )
    save_summaries(summaries, output_dir=data_dir)

    if mongo_client:
        mongo_client.close()


if __name__ == "__main__":
    main()
