"""
Summarizer — reads raw job listings, calls Mistral once per job to produce
both a human-readable summary AND structured metadata (tags, stack, remote,
job_type, experience_years) in a single API call, then saves to MongoDB and
a backup text file.
"""

import json
import re
from datetime import date, datetime, timezone
from pathlib import Path

from mistralai import Mistral

from scrapers.base import make_job_id

SEPARATOR = "=" * 80

# ─────────────────────────────────────────
#  PROMPTS
# ─────────────────────────────────────────

SYSTEM_PROMPT = """\
You are a professional job listing summarizer for an Asian job market platform.

Given raw job listing data, respond with a single valid JSON object — no markdown
code fences, no extra text, just the JSON.

Schema:
{
  "summary": "<markdown summary — see rules below>",
  "tags": ["<tag1>", "<tag2>", ...],
  "stack": ["<tech1>", "<tech2>", ...],
  "experience_years": "<X-Y or X+ or null>",
  "job_type": "<full-time|part-time|contract|null>",
  "remote": <true|false|null>
}

Rules for `summary`:
- Use EXACTLY these bold field names (Telegram markdown — single asterisks):
  *Job Title:* / *Company:* / *Salary:* / *Location:* / *About:*
- If a field is missing write "Not specified"
- Keep *About:* to 1-2 sentences — concise and factual
- Use ¥ for Japan, ₩ for Korea, ฿ for Thailand
- No intro, outro, or extra commentary

Rules for metadata:
- tags: 3-6 lowercase keywords describing the role, industry, location, perks
  e.g. ["backend", "fintech", "seoul", "java", "4.5-day-week"]
- stack: technology stack only — languages, frameworks, databases, tools
  e.g. ["java", "kotlin", "spring", "mysql"] — empty list for non-tech roles
- experience_years: "2-6", "3+", "0-2" or null if not mentioned
- job_type: "full-time", "part-time", or "contract" — null if unclear
- remote: true if remote/hybrid is explicit, false if explicitly on-site, null if unknown
"""


# ─────────────────────────────────────────
#  PARSER (reads legacy txt files)
# ─────────────────────────────────────────

def parse_jobs_file(filepath: str) -> list[dict]:
    """
    Parse a raw jobs txt file into a list of field dicts.
    Used when reading from the legacy text-file pipeline.
    """
    text   = Path(filepath).read_text(encoding="utf-8")
    blocks = text.split(SEPARATOR)
    jobs   = []

    for block in blocks:
        block = block.strip()
        if not block or block.startswith("Job Scrape"):
            continue

        job           = {}
        lines         = block.splitlines()
        desc_en_lines = []
        in_desc_en    = False

        for line in lines:
            if line.startswith("DESCRIPTION [EN]:"):
                in_desc_en = True
                continue
            if in_desc_en:
                desc_en_lines.append(line)
                continue

            for field in ["SOURCE", "URL", "TITLE [EN]", "COMPANY [EN]",
                          "LOCATION [EN]", "SALARY [EN]", "DEADLINE [EN]"]:
                prefix = f"{field}:"
                if line.startswith(prefix):
                    job[field] = line[len(prefix):].strip()

        job["DESCRIPTION [EN]"] = " ".join(desc_en_lines).strip()[:1500]

        if job.get("TITLE [EN]") or job.get("COMPANY [EN]"):
            jobs.append(job)

    return jobs


# ─────────────────────────────────────────
#  SUMMARISER
# ─────────────────────────────────────────

def _build_user_message(job: dict) -> str:
    return (
        f"Job Title: {job.get('TITLE [EN]', 'N/A')}\n"
        f"Company: {job.get('COMPANY [EN]', 'N/A')}\n"
        f"Salary: {job.get('SALARY [EN]', 'N/A')}\n"
        f"Location: {job.get('LOCATION [EN]', 'N/A')}\n"
        f"Description: {job.get('DESCRIPTION [EN]', 'N/A')}"
    )


def _parse_mistral_json(content: str) -> dict:
    """
    Extract JSON from the model response.
    Handles cases where the model wraps output in ```json ... ``` code fences.
    """
    content = content.strip()
    # Strip markdown code fences if present
    content = re.sub(r"^```(?:json)?\s*", "", content)
    content = re.sub(r"\s*```$", "", content)
    return json.loads(content.strip())


def summarise_jobs(
    jobs:         list[dict],
    api_key:      str,
    model:        str,
    region:       str = "",
    mongo_client=None,
) -> list[dict]:
    """
    Summarize each job with a single Mistral call that returns both the
    human-readable summary and structured metadata as JSON.

    Returns a list of plain dicts (for backward compatibility with save_summaries),
    and also saves SummaryDocument objects to MongoDB if mongo_client is provided.
    """
    from db.models import SummaryDocument  # local import

    client    = Mistral(api_key=api_key)
    summaries = []
    mongo_docs: list[SummaryDocument] = []

    for i, job in enumerate(jobs, 1):
        title = job.get("TITLE [EN]", "?")[:60]
        print(f"[summariser] {i}/{len(jobs)}: {title}")

        try:
            response = client.chat.complete(
                model=model,
                messages=[
                    {"role": "system", "content": SYSTEM_PROMPT},
                    {"role": "user",   "content": _build_user_message(job)},
                ],
            )
            raw     = response.choices[0].message.content.strip()
            parsed  = _parse_mistral_json(raw)

            summary_text      = parsed.get("summary", "").strip()
            tags              = parsed.get("tags", [])
            stack             = parsed.get("stack", [])
            experience_years  = parsed.get("experience_years")
            job_type          = parsed.get("job_type")
            remote            = parsed.get("remote")

        except Exception as e:
            print(f"[summariser] Error on '{title}': {e}")
            summary_text     = f"*Job Title:* {title}\n(Summary unavailable)"
            tags             = []
            stack            = []
            experience_years = None
            job_type         = None
            remote           = None

        url    = job.get("URL", "")
        source = job.get("SOURCE", "")

        # Legacy dict output (for txt file + backward compat)
        summaries.append({
            "url":              url,
            "source":           source,
            "summary":          summary_text,
            "tags":             tags,
            "stack":            stack,
            "experience_years": experience_years,
            "job_type":         job_type,
            "remote":           remote,
        })

        # Pydantic model for MongoDB
        if mongo_client is not None:
            mongo_docs.append(SummaryDocument(
                job_id           = make_job_id(source, url),
                source           = source,
                region           = region,
                url              = url,
                summarized_at    = datetime.now(timezone.utc),
                summarized_date  = str(date.today()),
                model            = model,
                summary          = summary_text,
                tags             = [t.lower() for t in tags] if tags else [],
                stack            = [s.lower() for s in stack] if stack else [],
                experience_years = experience_years,
                job_type         = job_type,
                remote           = remote,
            ))

    # ── MongoDB upsert ──
    if mongo_client and mongo_docs:
        from pymongo import UpdateOne
        db  = mongo_client["jobagent"]
        col = db["summaries"]
        ops = [
            UpdateOne(
                {"job_id": doc.job_id},
                {"$set": doc.to_mongo()},
                upsert=True,
            )
            for doc in mongo_docs
        ]
        result = col.bulk_write(ops)
        print(
            f"[db] summaries: {result.upserted_count} inserted, "
            f"{result.modified_count} updated"
        )

    return summaries


# ─────────────────────────────────────────
#  OUTPUT (txt backup)
# ─────────────────────────────────────────

def save_summaries(summaries: list[dict], output_dir: str = ".") -> str:
    """Save summaries to a dated text file. Returns the output filepath."""
    Path(output_dir).mkdir(parents=True, exist_ok=True)
    output_file = str(Path(output_dir) / f"summaries_{date.today()}.txt")

    with open(output_file, "w", encoding="utf-8") as f:
        f.write(f"Job Summaries — {date.today()}\n")
        f.write(f"Total: {len(summaries)}\n\n")
        for s in summaries:
            f.write(SEPARATOR + "\n")
            f.write(f"SOURCE: {s['source']}\n")
            f.write(f"URL:    {s['url']}\n\n")
            f.write(s["summary"])
            f.write("\n\n")

    print(f"[done] Saved {len(summaries)} summaries to {output_file}")
    return output_file
