"""Reader for the public Legistar Web API (https://webapi.legistar.com)."""

import logging
import os
from datetime import date, datetime

import requests

from .models import AgendaItem, Attachment, Meeting, clean_text, normalize_name

log = logging.getLogger(__name__)


class LegistarAPIUnavailable(Exception):
    """The Web API could not be used for this city; callers should fall back."""


class LegistarAPIReader:
    source = "api"

    def __init__(self, city, http, token=None):
        self.city = city
        self.http = http
        self.base = city.api_base
        # Some Legistar clients require a token; most public ones do not.
        self.token = token if token is not None else os.environ.get("LEGISTAR_TOKEN")
        self.unmatched_bodies = []

    def _get_json(self, path, params=None):
        params = dict(params or {})
        if self.token:
            params["token"] = self.token
        url = f"{self.base}{path}"
        try:
            resp = self.http.get(url, params=params, headers={"Accept": "application/json"})
        except requests.RequestException as exc:
            raise LegistarAPIUnavailable(f"request to {url} failed: {exc}") from exc
        if resp.status_code != 200:
            raise LegistarAPIUnavailable(
                f"{url} returned HTTP {resp.status_code}: {resp.text[:200]!r}")
        try:
            return resp.json()
        except ValueError as exc:
            raise LegistarAPIUnavailable(f"{url} did not return JSON") from exc

    def _resolve_bodies(self, wanted):
        """Verify the API responds and map the wanted body names to BodyIds."""
        bodies = self._get_json("/bodies")
        if not isinstance(bodies, list) or not bodies:
            raise LegistarAPIUnavailable(f"/bodies returned no data: {str(bodies)[:200]!r}")
        by_name = {normalize_name(b.get("BodyName")): b for b in bodies}
        matched = {}
        self.unmatched_bodies = []
        for name in wanted:
            body = by_name.get(normalize_name(name))
            if body is None:
                self.unmatched_bodies.append(name)
            else:
                matched[body["BodyId"]] = body["BodyName"]
        if self.unmatched_bodies:
            log.warning("API: no body named %s. Available bodies: %s", self.unmatched_bodies,
                        sorted(b.get("BodyName") or "" for b in bodies))
        if not matched:
            raise LegistarAPIUnavailable("none of the requested bodies exist in the API")
        return matched

    def fetch_meetings(self, start, end, bodies):
        """Return meetings for ``bodies`` dated ``start``..``end`` (inclusive)."""
        body_ids = self._resolve_bodies(bodies)
        events = self._get_json("/events", {
            "$filter": (f"EventDate ge datetime'{start.isoformat()}' "
                        f"and EventDate lt datetime'{_next_day(end)}'"),
            "$orderby": "EventDate asc",
        })
        if not isinstance(events, list):
            raise LegistarAPIUnavailable(f"/events returned unexpected data: {str(events)[:200]!r}")
        meetings = []
        for ev in events:
            if ev.get("EventBodyId") not in body_ids:
                continue
            meeting_date = _parse_date(ev.get("EventDate"))
            if meeting_date is None or not (start <= meeting_date <= end):
                continue
            meetings.append(Meeting(
                source=self.source,
                external_id=str(ev["EventId"]),
                body_name=ev.get("EventBodyName") or body_ids[ev["EventBodyId"]],
                meeting_date=meeting_date,
                meeting_time=ev.get("EventTime") or "",
                location=clean_text(ev.get("EventLocation")),
                agenda_url=ev.get("EventAgendaFile") or "",
                detail_url=ev.get("EventInSiteURL") or "",
                fetch_ref=ev["EventId"],
            ))
        return meetings

    def fetch_items(self, meeting):
        rows = self._get_json(f"/events/{meeting.fetch_ref}/eventitems",
                              {"AgendaNote": 1, "MinutesNote": 0, "Attachments": 1})
        if not isinstance(rows, list):
            raise LegistarAPIUnavailable(f"eventitems returned unexpected data: {str(rows)[:200]!r}")
        rows.sort(key=lambda r: (r.get("EventItemAgendaSequence") or 0))
        items = []
        for row in rows:
            title = clean_text(row.get("EventItemTitle"))
            if not title:
                continue
            attachments = []
            for att in row.get("EventItemMatterAttachments") or []:
                url = att.get("MatterAttachmentHyperlink")
                if url:
                    attachments.append(Attachment(
                        name=clean_text(att.get("MatterAttachmentName")) or url, url=url))
            items.append(AgendaItem(
                agenda_number=clean_text(row.get("EventItemAgendaNumber")),
                title=title,
                matter_file=clean_text(row.get("EventItemMatterFile")),
                attachments=attachments,
            ))
        return items


def _parse_date(value):
    if not value:
        return None
    try:
        return datetime.fromisoformat(value[:19]).date()
    except ValueError:
        return None


def _next_day(d):
    return date.fromordinal(d.toordinal() + 1).isoformat()
