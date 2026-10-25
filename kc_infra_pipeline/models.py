"""Source-independent data records produced by the readers."""

import re
from dataclasses import dataclass, field
from datetime import date
from typing import List, Optional


@dataclass
class Attachment:
    name: str
    url: str


@dataclass
class AgendaItem:
    agenda_number: str
    title: str
    matter_file: str = ""
    detail_url: str = ""
    attachments: List[Attachment] = field(default_factory=list)


@dataclass
class Meeting:
    source: str  # "api" or "scrape"
    external_id: str  # Legistar EventId when known
    body_name: str
    meeting_date: date
    meeting_time: str = ""
    location: str = ""
    agenda_url: str = ""
    detail_url: str = ""
    # Opaque reader-specific handle used to fetch the agenda items later.
    fetch_ref: Optional[object] = None


def normalize_name(name: str) -> str:
    """Normalize a body name for comparison ("Public Works  &  Planning" -> "public works and planning")."""
    name = (name or "").replace("&", " and ").lower()
    return re.sub(r"\s+", " ", name).strip()


def clean_text(text: str) -> str:
    return re.sub(r"\s+", " ", text or "").strip()
