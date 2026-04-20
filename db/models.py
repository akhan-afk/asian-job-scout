"""
Pydantic models for MongoDB documents.

JobDocument   — one raw scraped listing
SummaryDocument — one AI-generated summary, linked to a JobDocument via job_id
"""

from __future__ import annotations

from datetime import datetime
from typing import Optional

from pydantic import BaseModel, Field, field_validator


class JobDocument(BaseModel):
    """
    Raw scraped job listing stored in the `jobs` collection.

    job_id is a stable identifier derived from source + URL hash so the same
    listing can be upserted without creating duplicates across scrape runs.
    """
    job_id:         str
    source:         str          # rikunabi | wanted | jobsdb
    region:         str          # japan | korea | thailand
    url:            str

    scraped_at:     datetime
    scraped_date:   str          # "YYYY-MM-DD" — easy range queries
    is_active:      bool = True

    # Raw fields (original language)
    title:          str = ""
    company:        str = ""
    location:       str = ""
    salary:         str = ""
    deadline:       str = ""
    description:    str = ""

    # Translated fields
    title_en:       str = ""
    company_en:     str = ""
    location_en:    str = ""
    salary_en:      str = ""
    deadline_en:    str = ""
    description_en: str = ""

    def to_mongo(self) -> dict:
        return self.model_dump()


class SummaryDocument(BaseModel):
    """
    AI-generated summary stored in the `summaries` collection.

    Linked to JobDocument via job_id. One summary per job.
    The `embedding` field is reserved for Phase 2 (Atlas Vector Search).
    """
    job_id:           str
    source:           str
    region:           str
    url:              str

    summarized_at:    datetime
    summarized_date:  str        # "YYYY-MM-DD"
    model:            str        # e.g. "mistral-small-latest"

    summary:          str        # full markdown summary text

    # Structured metadata extracted in the same Mistral call
    tags:             list[str] = Field(default_factory=list)
    # e.g. ["backend", "fintech", "seoul", "java"]
    stack:            list[str] = Field(default_factory=list)
    # e.g. ["java", "kotlin", "spring"]
    experience_years: Optional[str] = None   # "2-6" | "3+" | None
    job_type:         Optional[str] = None   # "full-time" | "part-time" | "contract"
    remote:           Optional[bool] = None  # True | False | None (unknown)

    # Phase 2: vector embedding for semantic search via Atlas Vector Search
    embedding:        Optional[list[float]] = None

    def to_mongo(self) -> dict:
        d = self.model_dump()
        # Don't store a null embedding field — saves space until Phase 2
        if d.get("embedding") is None:
            d.pop("embedding", None)
        return d


class UserDocument(BaseModel):
    """
    Telegram user who has subscribed to job alert notifications.
    Stored in the `users` collection.
    """
    user_id:        int
    username:       Optional[str] = None
    first_name:     Optional[str] = None
    subscribed:     bool = True
    regions:        list[str] = Field(default_factory=lambda: ["japan", "korea", "thailand"])
    subscribed_at:  datetime
    last_notified:  Optional[datetime] = None  # tracks last successful notification

    def to_mongo(self) -> dict:
        return self.model_dump()
