"""Fallback reader that scrapes the public Legistar web site (Calendar.aspx)."""

import json
import logging
import re
from datetime import datetime
from html.parser import HTMLParser
from urllib.parse import parse_qs, urljoin, urlparse

import requests

from .models import AgendaItem, Attachment, Meeting, clean_text, normalize_name

log = logging.getLogger(__name__)

YEARS_FIELD = "ctl00$ContentPlaceHolder1$lstYears"
YEARS_CLIENT_STATE = "ctl00_ContentPlaceHolder1_lstYears_ClientState"


class LegistarScrapeError(Exception):
    pass


# --------------------------------------------------------------------------
# Minimal HTML tree (stdlib only)
# --------------------------------------------------------------------------

VOID_TAGS = {"area", "base", "br", "col", "embed", "hr", "img", "input", "link",
             "meta", "param", "source", "track", "wbr"}


class Node:
    __slots__ = ("tag", "attrs", "children", "parent")

    def __init__(self, tag, attrs=None, parent=None):
        self.tag = tag
        self.attrs = dict(attrs or {})
        self.children = []
        self.parent = parent

    def iter(self, tag=None):
        for child in self.children:
            if isinstance(child, Node):
                if tag is None or child.tag == tag:
                    yield child
                yield from child.iter(tag)

    def find_id(self, element_id):
        return next((n for n in self.iter() if n.attrs.get("id") == element_id), None)

    def text(self):
        parts = []
        for child in self.children:
            parts.append(child.text() if isinstance(child, Node) else child)
        return "".join(parts)

    def links(self):
        return [a for a in self.iter("a") if a.attrs.get("href")]


class _TreeBuilder(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.root = Node("#document")
        self.current = self.root

    def handle_starttag(self, tag, attrs):
        node = Node(tag, [(k, v or "") for k, v in attrs], self.current)
        self.current.children.append(node)
        if tag not in VOID_TAGS:
            self.current = node

    def handle_startendtag(self, tag, attrs):
        node = Node(tag, [(k, v or "") for k, v in attrs], self.current)
        self.current.children.append(node)

    def handle_endtag(self, tag):
        node = self.current
        while node is not self.root and node.tag != tag:
            node = node.parent
        if node is not self.root:  # ignore unmatched end tags
            self.current = node.parent

    def handle_data(self, data):
        self.current.children.append(data)


def parse_html(html):
    builder = _TreeBuilder()
    builder.feed(html)
    builder.close()
    return builder.root


def grid_rows(doc, table_id_suffix):
    """Yield ``{header: cell_node}`` for each data row of a Telerik RadGrid table."""
    table = next((t for t in doc.iter("table")
                  if t.attrs.get("id", "").endswith(table_id_suffix)), None)
    if table is None:
        return
    headers = [clean_text(th.text()) for th in table.iter("th")]
    for tr in table.iter("tr"):
        classes = tr.attrs.get("class", "").split()
        if "rgRow" not in classes and "rgAltRow" not in classes:
            continue
        cells = [td for td in tr.children if isinstance(td, Node) and td.tag == "td"]
        yield {headers[i] if i < len(headers) else str(i): cell for i, cell in enumerate(cells)}


def _cell(row, *names):
    for name in names:
        if name in row:
            return row[name]
    return None


def _first_href(node, pattern):
    if node is None:
        return ""
    for a in node.links():
        if re.search(pattern, a.attrs["href"], re.I):
            return a.attrs["href"]
    return ""


def _query_id(url):
    qs = parse_qs(urlparse(url).query)
    for key in ("ID", "LEGID"):
        if qs.get(key):
            return qs[key][0]
    return ""


def hidden_form_fields(doc):
    return {i.attrs["name"]: i.attrs.get("value", "")
            for i in doc.iter("input")
            if i.attrs.get("type", "").lower() == "hidden" and i.attrs.get("name")}


# --------------------------------------------------------------------------
# Reader
# --------------------------------------------------------------------------

class LegistarScraper:
    source = "scrape"

    def __init__(self, city, http):
        self.city = city
        self.http = http
        self.base = city.site_base + "/"
        self.calendar_url = urljoin(self.base, "Calendar.aspx")
        self.unmatched_bodies = []

    def _fetch(self, url, method="GET", data=None):
        try:
            resp = self.http.request(method, url, data=data)
        except requests.RequestException as exc:
            raise LegistarScrapeError(f"{method} {url} failed: {exc}") from exc
        if resp.status_code != 200:
            raise LegistarScrapeError(f"{method} {url} returned HTTP {resp.status_code}")
        return parse_html(resp.text)

    def _calendar_pages(self, start, end):
        """The default calendar view plus "This Month"/"Last Month" (which together
        always cover a window of up to ~4 weeks ending today)."""
        first = self._fetch(self.calendar_url)
        yield first
        form = hidden_form_fields(first)
        periods = ["This Month"]
        if (start.year, start.month) != (end.year, end.month):
            periods.append("Last Month")
        for period in periods:
            payload = dict(form)
            payload["__EVENTTARGET"] = YEARS_FIELD
            payload["__EVENTARGUMENT"] = ""
            payload[YEARS_FIELD] = period
            payload[YEARS_CLIENT_STATE] = json.dumps({
                "logEntries": [], "value": period, "text": period, "enabled": True,
                "checkedIndices": [], "checkedItemsTextOverridden": False})
            try:
                yield self._fetch(self.calendar_url, "POST", payload)
            except LegistarScrapeError as exc:
                log.warning("Calendar filter %r failed: %s", period, exc)

    def fetch_meetings(self, start, end, bodies):
        wanted = {normalize_name(b): b for b in bodies}
        seen_bodies = set()
        meetings = {}
        any_rows = False
        for doc in self._calendar_pages(start, end):
            for row in grid_rows(doc, "gridCalendar_ctl00"):
                any_rows = True
                body = clean_text(_cell(row, "Name").text()) if _cell(row, "Name") else ""
                seen_bodies.add(normalize_name(body))
                if normalize_name(body) not in wanted:
                    continue
                date_cell = _cell(row, "Meeting Date", "Date")
                meeting_date = _parse_us_date(date_cell.text() if date_cell else "")
                if meeting_date is None or not (start <= meeting_date <= end):
                    continue
                time_cell = _cell(row, "Meeting Time", "Time")
                loc_cell = _cell(row, "Meeting Location", "Location")
                detail = _first_href(_cell(row, "Meeting Details", "Details"), r"MeetingDetail\.aspx")
                agenda = _first_href(_cell(row, "Agenda"), r"View\.ashx")
                meeting_time = clean_text(time_cell.text()) if time_cell else ""
                detail_url = urljoin(self.base, detail) if detail else ""
                external_id = _query_id(detail_url) or f"{body}|{meeting_date}|{meeting_time}"
                meetings.setdefault(external_id, Meeting(
                    source=self.source,
                    external_id=external_id,
                    body_name=body,
                    meeting_date=meeting_date,
                    meeting_time=meeting_time,
                    location=clean_text(loc_cell.text()) if loc_cell else "",
                    agenda_url=urljoin(self.base, agenda) if agenda else "",
                    detail_url=detail_url,
                    fetch_ref=detail_url,
                ))
        if not any_rows:
            raise LegistarScrapeError("no calendar rows found on " + self.calendar_url)
        self.unmatched_bodies = [b for n, b in wanted.items() if n not in seen_bodies]
        return sorted(meetings.values(), key=lambda m: (m.meeting_date, m.body_name))

    def fetch_items(self, meeting):
        if not meeting.fetch_ref:
            return []
        doc = self._fetch(meeting.fetch_ref)
        items = []
        for row in grid_rows(doc, "gridMain_ctl00"):
            title_cell = _cell(row, "Title")
            title = clean_text(title_cell.text()) if title_cell else ""
            if not title:
                continue
            number_cell = _cell(row, "Agenda #", "Agenda Number")
            file_cell = _cell(row, "File #", "File")
            legislation = _first_href(file_cell, r"LegislationDetail\.aspx")
            item = AgendaItem(
                agenda_number=clean_text(number_cell.text()) if number_cell else "",
                title=title,
                matter_file=clean_text(file_cell.text()) if file_cell else "",
                detail_url=urljoin(meeting.fetch_ref, legislation) if legislation else "",
            )
            if item.detail_url:
                try:
                    item.attachments = self._fetch_attachments(item.detail_url)
                except LegistarScrapeError as exc:
                    log.warning("Could not load attachments for %s: %s", item.matter_file, exc)
            items.append(item)
        return items

    def _fetch_attachments(self, legislation_url):
        doc = self._fetch(legislation_url)
        container = doc.find_id("ctl00_ContentPlaceHolder1_lblAttachments2") or doc
        attachments, seen = [], set()
        for a in container.links():
            if not re.search(r"View\.ashx\?M=F", a.attrs["href"], re.I):
                continue
            url = urljoin(legislation_url, a.attrs["href"])
            if url not in seen:
                seen.add(url)
                attachments.append(Attachment(name=clean_text(a.text()) or url, url=url))
        return attachments


def _parse_us_date(value):
    value = clean_text(value)
    try:
        return datetime.strptime(value, "%m/%d/%Y").date()
    except ValueError:
        return None
