import hashlib
import sys
from abc import ABC, abstractmethod
from datetime import date, datetime, timezone
from pathlib import Path

from bs4 import BeautifulSoup
from deep_translator import GoogleTranslator
from playwright.async_api import Page

SEPARATOR = "=" * 80
DEBUG = "--debug" in sys.argv

_translator = GoogleTranslator(source="auto", target="en")


def translate(text: str) -> str:
    """Translate text to English. Returns original text if translation fails."""
    if not text or not text.strip():
        return text
    try:
        if len(text) > 5000:
            text = text[:5000]
        result = _translator.translate(text)
        return result if result else text
    except Exception:
        return text


def make_job_id(source: str, url: str) -> str:
    """
    Generate a stable, unique job identifier from source name + URL hash.
    e.g. "wanted_a3f8b2c1d5", "rikunabi_b7e4f2a0c1"
    Safe to use as a MongoDB upsert key.
    """
    url_hash = hashlib.sha1(url.encode()).hexdigest()[:10]
    return f"{source}_{url_hash}"


# ─────────────────────────────────────────
#  DATA MODEL
# ─────────────────────────────────────────

class JobListing:
    """
    Scraper output model. Holds raw fields as scraped from each site.

    Translations are computed lazily and cached so they are only called once
    regardless of whether we write to a text file, MongoDB, or both.
    """

    def __init__(
        self,
        source:      str,
        url:         str,
        title:       str = "",
        company:     str = "",
        location:    str = "",
        salary:      str = "",
        deadline:    str = "",
        description: str = "",
    ):
        self.source      = source
        self.url         = url
        self.title       = title
        self.company     = company
        self.location    = location
        self.salary      = salary
        self.deadline    = deadline
        self.description = description

        self._t: dict[str, str] = {}  # translation cache

    def _tr(self, text: str, key: str) -> str:
        """Return cached translation for `key`, computing it on first call."""
        if key not in self._t:
            self._t[key] = translate(text)
        return self._t[key]

    def to_text(self) -> str:
        """Render as the legacy text-file block format (kept for debugging/backup)."""
        return (
            f"{SEPARATOR}\n"
            f"SOURCE:       {self.source}\n"
            f"URL:          {self.url}\n"
            f"TITLE:        {self.title}\n"
            f"TITLE [EN]:   {self._tr(self.title, 'title')}\n"
            f"COMPANY:      {self.company}\n"
            f"COMPANY [EN]: {self._tr(self.company, 'company')}\n"
            f"LOCATION:     {self.location}\n"
            f"LOCATION [EN]:{self._tr(self.location, 'location')}\n"
            f"SALARY:       {self.salary}\n"
            f"SALARY [EN]:  {self._tr(self.salary, 'salary')}\n"
            f"DEADLINE:     {self.deadline}\n"
            f"DEADLINE [EN]:{self._tr(self.deadline, 'deadline')}\n"
            f"DESCRIPTION:\n{self.description}\n"
            f"DESCRIPTION [EN]:\n{self._tr(self.description, 'description')}\n"
        )

    def to_document(self, region: str) -> "JobDocument":
        """
        Convert to a JobDocument Pydantic model ready for MongoDB upsert.
        Reuses cached translations so calling to_text() first avoids any
        redundant Google Translate API calls.
        """
        from db.models import JobDocument  # local import avoids circular deps

        return JobDocument(
            job_id       = make_job_id(self.source, self.url),
            source       = self.source,
            region       = region,
            url          = self.url,
            scraped_at   = datetime.now(timezone.utc),
            scraped_date = str(date.today()),
            title        = self.title,
            title_en     = self._tr(self.title,       "title"),
            company      = self.company,
            company_en   = self._tr(self.company,     "company"),
            location     = self.location,
            location_en  = self._tr(self.location,    "location"),
            salary       = self.salary,
            salary_en    = self._tr(self.salary,      "salary"),
            deadline     = self.deadline,
            deadline_en  = self._tr(self.deadline,    "deadline"),
            description  = self.description,
            description_en = self._tr(self.description, "description"),
        )


# Type alias for the Pydantic model imported above
try:
    from db.models import JobDocument
except ImportError:
    JobDocument = None  # type: ignore


# ─────────────────────────────────────────
#  BASE SCRAPER
# ─────────────────────────────────────────

class BaseScraper(ABC):
    source_name: str = ""

    def __init__(self, page: Page):
        self.page = page

    @abstractmethod
    async def get_listing_urls(self) -> list[str]:
        """Return a list of individual job listing URLs to scrape."""

    @abstractmethod
    async def parse_listing(self, url: str) -> JobListing:
        """Parse a single job listing page and return a JobListing."""

    async def scrape(self) -> list[JobListing]:
        print(f"[{self.source_name}] Fetching listing URLs...")
        urls = await self.get_listing_urls()
        print(f"[{self.source_name}] Found {len(urls)} listings.")
        jobs = []
        for i, url in enumerate(urls, 1):
            print(f"[{self.source_name}] Parsing {i}/{len(urls)}: {url}")
            try:
                job = await self.parse_listing(url)
                jobs.append(job)
            except Exception as e:
                print(f"[{self.source_name}] Error parsing {url}: {e}")
        return jobs

    def _clean(self, text: str | None) -> str:
        if not text:
            return ""
        return " ".join(text.split())

    def _debug_links(self, soup: BeautifulSoup, label: str):
        if not DEBUG:
            return
        title = soup.title.string.strip() if soup.title and soup.title.string else "(no title)"
        body_text = soup.get_text()[:300].replace("\n", " ").strip()
        print(f"\n[DEBUG {label}] Page title: {title}")
        print(f"[DEBUG {label}] Page preview: {body_text}")
        all_hrefs = [a.get("href", "") for a in soup.select("a[href]")]
        unique = sorted(set(h for h in all_hrefs if h and not h.startswith("#")))
        print(f"\n[DEBUG {label}] {len(unique)} unique links found:")
        for href in unique[:60]:
            print(f"  {href}")

    def _debug_detail(self, soup: BeautifulSoup):
        if not DEBUG:
            return
        keywords = {
            "salary":   ["給与", "月給", "年収", "賃金", "給料", "급여", "월급", "연봉"],
            "location": ["勤務地", "勤務場所", "就業場所", "근무지", "근무장소"],
            "deadline": ["締切", "応募締切", "エントリー締切", "마감", "지원마감"],
        }
        print("\n[DEBUG detail] Searching for field elements:")
        for field, words in keywords.items():
            for el in soup.find_all(True):
                text = el.get_text()
                if any(w in text for w in words) and len(text) < 200:
                    classes = " ".join(el.get("class", []))
                    tag = el.name
                    print(f"  [{field}] <{tag} class='{classes}'> → {text[:80].strip()}")


# ─────────────────────────────────────────
#  OUTPUT
# ─────────────────────────────────────────

def save_jobs(
    jobs:         list[JobListing],
    output_dir:   str = ".",
    region:       str = "",
    mongo_client=None,
) -> str:
    """
    Save scraped jobs to:
      1. A dated text file (always — useful for debugging and legacy tools)
      2. MongoDB `jobs` collection (when mongo_client is provided)

    MongoDB uses upsert on job_id so re-running the scraper won't duplicate
    listings — it will update them instead.

    Returns the path of the text file written.
    """
    Path(output_dir).mkdir(parents=True, exist_ok=True)
    output_file = str(Path(output_dir) / f"jobs_{date.today()}.txt")

    # ── Text file ──
    with open(output_file, "w", encoding="utf-8") as f:
        f.write(f"Job Scrape Results — {date.today()}\n")
        f.write(f"Total listings: {len(jobs)}\n\n")
        for job in jobs:
            f.write(job.to_text())   # translations cached here
            f.write("\n")
    print(f"[done] Saved {len(jobs)} jobs to {output_file}")

    # ── MongoDB ──
    if mongo_client is not None and region:
        from pymongo import UpdateOne
        db  = mongo_client["jobagent"]
        col = db["jobs"]

        ops = []
        for job in jobs:
            doc = job.to_document(region).to_mongo()  # reuses cached translations
            ops.append(UpdateOne(
                {"job_id": doc["job_id"]},
                {"$set": doc},
                upsert=True,
            ))

        if ops:
            result = col.bulk_write(ops)
            print(
                f"[db] jobs: {result.upserted_count} inserted, "
                f"{result.modified_count} updated"
            )

    return output_file
