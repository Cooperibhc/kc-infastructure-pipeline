"""Orchestrates one run: choose a reader, store meetings/items, process PDFs."""

import logging
from dataclasses import dataclass, field
from datetime import date, timedelta

from .legistar_api import LegistarAPIReader, LegistarAPIUnavailable
from .legistar_scraper import LegistarScraper, LegistarScrapeError
from .pdfs import process_documents

log = logging.getLogger(__name__)


@dataclass
class RunSummary:
    city: str = ""
    source: str = ""
    start: date = None
    end: date = None
    meetings_found: int = 0
    new_meetings: int = 0
    new_items: int = 0
    pdfs_processed: int = 0
    non_pdf_attachments: int = 0
    pdf_errors: int = 0
    meeting_errors: int = 0
    unmatched_bodies: list = field(default_factory=list)

    def render(self):
        lines = [
            f"Summary for {self.city} ({self.start} to {self.end}, source: {self.source})",
            f"  Meetings found:     {self.meetings_found} ({self.new_meetings} new)",
            f"  New items added:    {self.new_items}",
            f"  PDFs processed:     {self.pdfs_processed}",
        ]
        if self.non_pdf_attachments:
            lines.append(f"  Non-PDF attachments skipped: {self.non_pdf_attachments}")
        if self.pdf_errors:
            lines.append(f"  PDF errors (will retry next run): {self.pdf_errors}")
        if self.meeting_errors:
            lines.append(f"  Meetings whose agenda could not be read: {self.meeting_errors}")
        if self.unmatched_bodies:
            lines.append(f"  Bodies not found at source: {', '.join(self.unmatched_bodies)}")
        return "\n".join(lines)


def find_meetings(city, http, start, end, bodies, source="auto"):
    """Return ``(reader, meetings)``, preferring the Web API and falling back to scraping."""
    if source in ("auto", "api"):
        api = LegistarAPIReader(city, http)
        try:
            meetings = api.fetch_meetings(start, end, bodies)
            log.info("Legistar Web API OK: %d matching meetings", len(meetings))
            return api, meetings
        except LegistarAPIUnavailable as exc:
            if source == "api":
                raise
            log.warning("Legistar Web API unavailable (%s); falling back to scraping %s",
                        exc, city.site_base + "/Calendar.aspx")
    scraper = LegistarScraper(city, http)
    return scraper, scraper.fetch_meetings(start, end, bodies)


def run(city, db, http, docs_dir, days=14, source="auto", bodies=None,
        process_pdfs=True, today=None):
    end = today or date.today()
    start = end - timedelta(days=days)
    bodies = list(bodies or city.bodies)
    summary = RunSummary(city=city.name, start=start, end=end)

    reader, meetings = find_meetings(city, http, start, end, bodies, source)
    summary.source = reader.source
    summary.meetings_found = len(meetings)
    summary.unmatched_bodies = list(reader.unmatched_bodies)

    for meeting in meetings:
        meeting_id, is_new = db.add_meeting(city.slug, meeting)
        summary.new_meetings += is_new
        if not is_new and db.meeting_items_complete(meeting_id):
            log.info("Already stored: %s %s", meeting.meeting_date, meeting.body_name)
            continue
        log.info("Fetching agenda: %s %s", meeting.meeting_date, meeting.body_name)
        try:
            items = reader.fetch_items(meeting)
        except (LegistarAPIUnavailable, LegistarScrapeError) as exc:
            log.warning("Could not read agenda for %s %s: %s",
                        meeting.meeting_date, meeting.body_name, exc)
            summary.meeting_errors += 1
            continue
        for item in items:
            summary.new_items += db.add_item(meeting_id, item)
        db.mark_items_fetched(meeting_id)

    if process_pdfs:
        counts = process_documents(db, http, docs_dir)
        summary.pdfs_processed = counts["processed"]
        summary.non_pdf_attachments = counts["not_pdf"]
        summary.pdf_errors = counts["errors"]
    return summary
