"""Offline tests (no network): python -m unittest discover -s tests"""

import json
import tempfile
import unittest
from datetime import date
from pathlib import Path
from urllib.parse import urlencode

from kc_infra_pipeline.config import CITIES
from kc_infra_pipeline.db import Database
from kc_infra_pipeline.http import PoliteSession
from kc_infra_pipeline.legistar_scraper import LegistarScraper
from kc_infra_pipeline.pipeline import run

CITY = CITIES["lees-summit"]
TODAY = date(2026, 10, 25)
API = "https://webapi.legistar.com/v1/lsmo"
SITE = "https://lsmo.legistar.com/"


def make_pdf(text):
    """Build a minimal one-page PDF containing ``text``."""
    stream = f"BT /F1 12 Tf 72 720 Td ({text}) Tj ET".encode()
    objects = [
        b"<< /Type /Catalog /Pages 2 0 R >>",
        b"<< /Type /Pages /Kids [3 0 R] /Count 1 >>",
        b"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 612 792] "
        b"/Resources << /Font << /F1 5 0 R >> >> /Contents 4 0 R >>",
        b"<< /Length %d >>\nstream\n" % len(stream) + stream + b"\nendstream",
        b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>",
    ]
    out, offsets = bytearray(b"%PDF-1.4\n"), []
    for i, obj in enumerate(objects, 1):
        offsets.append(len(out))
        out += b"%d 0 obj\n" % i + obj + b"\nendobj\n"
    xref = len(out)
    out += b"xref\n0 %d\n0000000000 65535 f \n" % (len(objects) + 1)
    out += b"".join(b"%010d 00000 n \n" % o for o in offsets)
    out += b"trailer\n<< /Size %d /Root 1 0 R >>\nstartxref\n%d\n%%%%EOF\n" % (len(objects) + 1, xref)
    return bytes(out)


class FakeResponse:
    def __init__(self, status=200, body=b""):
        self.status_code = status
        self.content = body if isinstance(body, bytes) else body.encode()
        self.text = self.content.decode("utf-8", "replace")

    def json(self):
        return json.loads(self.text)

    def raise_for_status(self):
        if self.status_code >= 400:
            import requests
            raise requests.HTTPError(f"HTTP {self.status_code}")

    def iter_content(self, chunk_size=1):
        for i in range(0, len(self.content), chunk_size):
            yield self.content[i:i + chunk_size]

    def close(self):
        pass


class FakeHTTP:
    """Stands in for PoliteSession; routes are matched on (method, url-without-query)."""

    def __init__(self, routes):
        self.routes = routes
        self.calls = []

    def request(self, method, url, params=None, data=None, **_):
        full = url + ("?" + urlencode(params) if params else "")
        self.calls.append((method, full, data))
        key = (method, url.split("?")[0] if "View.ashx" not in url else url)
        if key not in self.routes:
            return FakeResponse(404, "not found")
        route = self.routes[key]
        return route(data) if callable(route) else route

    def get(self, url, **kw):
        return self.request("GET", url, **kw)

    def post(self, url, **kw):
        return self.request("POST", url, **kw)


# --------------------------------------------------------------------------
# Fixtures
# --------------------------------------------------------------------------

BODIES = [
    {"BodyId": 138, "BodyName": "City Council"},
    {"BodyId": 200, "BodyName": "Public Works Committee"},
    {"BodyId": 201, "BodyName": "Planning Commission"},
    {"BodyId": 300, "BodyName": "Parks Board"},
]
EVENTS = [
    {"EventId": 1001, "EventBodyId": 138, "EventBodyName": "City Council",
     "EventDate": "2026-10-20T00:00:00", "EventTime": "6:00 PM", "EventLocation": "City Hall",
     "EventAgendaFile": SITE + "View.ashx?M=A&ID=1001", "EventInSiteURL": SITE + "MeetingDetail.aspx?LEGID=1001"},
    {"EventId": 1002, "EventBodyId": 300, "EventBodyName": "Parks Board",
     "EventDate": "2026-10-21T00:00:00", "EventTime": "5:00 PM"},
    {"EventId": 1003, "EventBodyId": 200, "EventBodyName": "Public Works Committee",
     "EventDate": "2026-10-15T00:00:00", "EventTime": "4:30 PM"},
]
PDF_URL = SITE + "View.ashx?M=F&ID=555&GUID=AAA"
DOCX_URL = SITE + "View.ashx?M=F&ID=556&GUID=BBB"
ITEMS_1001 = [
    {"EventItemId": 1, "EventItemAgendaSequence": 1, "EventItemAgendaNumber": None,
     "EventItemTitle": None},
    {"EventItemId": 2, "EventItemAgendaSequence": 3, "EventItemAgendaNumber": "2.",
     "EventItemTitle": "Approval of an agreement for the Pryor Road widening project",
     "EventItemMatterFile": "BILL NO. 26-101",
     "EventItemMatterAttachments": [
         {"MatterAttachmentName": "Staff Report", "MatterAttachmentHyperlink": PDF_URL},
         {"MatterAttachmentName": "Map", "MatterAttachmentHyperlink": DOCX_URL}]},
    {"EventItemId": 3, "EventItemAgendaSequence": 2, "EventItemAgendaNumber": "1.",
     "EventItemTitle": "Roll Call", "EventItemMatterAttachments": []},
]
ITEMS_1003 = [
    {"EventItemId": 9, "EventItemAgendaSequence": 1, "EventItemAgendaNumber": "1.",
     "EventItemTitle": "Stormwater master plan update",
     "EventItemMatterAttachments": [
         {"MatterAttachmentName": "Staff Report", "MatterAttachmentHyperlink": PDF_URL}]},
]

CALENDAR_HTML = """
<html><body><form>
<input type="hidden" name="__VIEWSTATE" value="vs123" />
<input type="hidden" name="__EVENTVALIDATION" value="ev456">
<table id="ctl00_ContentPlaceHolder1_gridCalendar_ctl00" class="rgMasterTable">
<thead><tr><th class="rgHeader"><a>Name</a></th><th>Meeting Date</th><th>&nbsp;</th>
<th>Meeting Time</th><th>Meeting Location</th><th>Meeting Details</th><th>Agenda</th><th>Minutes</th></tr></thead>
<tbody>
<tr class="rgRow"><td><a href="DepartmentDetail.aspx?ID=1">City&nbsp;Council</a></td><td>10/20/2026</td>
 <td><img src="ical.png"></td><td><span>6:00 PM</span></td><td>City Hall<br>Council Chambers</td>
 <td><a href="MeetingDetail.aspx?ID=1001&amp;GUID=X&amp;Options=info|&amp;Search=">Meeting&nbsp;details</a></td>
 <td><a href="View.ashx?M=A&amp;ID=1001&amp;GUID=X">Agenda</a></td><td>Not available</td></tr>
<tr class="rgAltRow"><td><a>Parks Board</a></td><td>10/21/2026</td><td></td><td>5:00 PM</td><td></td>
 <td><a href="MeetingDetail.aspx?ID=1002&amp;GUID=Y">Meeting&nbsp;details</a></td><td></td><td></td></tr>
<tr class="rgRow"><td><a>Planning Commission</a></td><td>9/01/2026</td><td></td><td>5:00 PM</td><td></td>
 <td><a href="MeetingDetail.aspx?ID=900&amp;GUID=Z">Meeting&nbsp;details</a></td><td></td><td></td></tr>
</tbody></table></form></body></html>
"""
CALENDAR_LAST_MONTH_HTML = CALENDAR_HTML  # same rows; must be de-duplicated
MEETING_HTML = """
<html><body><table id="ctl00_ContentPlaceHolder1_gridMain_ctl00">
<thead><tr><th>File #</th><th>Ver.</th><th>Agenda #</th><th>Name</th><th>Type</th><th>Title</th><th>Action</th></tr></thead>
<tbody>
<tr class="rgRow"><td></td><td></td><td>1.</td><td></td><td></td><td>Roll Call</td><td></td></tr>
<tr class="rgAltRow"><td><a href="LegislationDetail.aspx?ID=777&amp;GUID=Q">BILL NO. 26-101</a></td><td>1</td>
 <td>2.</td><td></td><td>Ordinance</td><td>Approval of an agreement for the   Pryor Road widening project</td><td></td></tr>
<tr class="rgRow"><td></td><td></td><td></td><td></td><td></td><td>&nbsp;</td><td></td></tr>
</tbody></table></body></html>
"""
LEGISLATION_HTML = """
<html><body><span id="ctl00_ContentPlaceHolder1_lblAttachments2">
<a href="View.ashx?M=F&amp;ID=555&amp;GUID=AAA">Staff Report</a><br />
<a href="View.ashx?M=F&amp;ID=556&amp;GUID=BBB">Map</a></span>
<a href="View.ashx?M=F&amp;ID=999&amp;GUID=ZZZ">Unrelated link outside attachments</a>
</body></html>
"""


def api_routes():
    return {
        ("GET", API + "/bodies"): FakeResponse(200, json.dumps(BODIES)),
        ("GET", API + "/events"): FakeResponse(200, json.dumps(EVENTS)),
        ("GET", API + "/events/1001/eventitems"): FakeResponse(200, json.dumps(ITEMS_1001)),
        ("GET", API + "/events/1003/eventitems"): FakeResponse(200, json.dumps(ITEMS_1003)),
        ("GET", PDF_URL): FakeResponse(200, make_pdf("Pryor Road widening staff report")),
        ("GET", DOCX_URL): FakeResponse(200, b"PK\x03\x04 not a pdf"),
    }


def scrape_routes():
    return {
        ("GET", SITE + "Calendar.aspx"): FakeResponse(200, CALENDAR_HTML),
        ("POST", SITE + "Calendar.aspx"): FakeResponse(200, CALENDAR_LAST_MONTH_HTML),
        ("GET", SITE + "MeetingDetail.aspx"): FakeResponse(200, MEETING_HTML),
        ("GET", SITE + "LegislationDetail.aspx"): FakeResponse(200, LEGISLATION_HTML),
        ("GET", PDF_URL): FakeResponse(200, make_pdf("Pryor Road widening staff report")),
        ("GET", DOCX_URL): FakeResponse(200, b"PK\x03\x04 not a pdf"),
    }


class PipelineTestCase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.docs = Path(self.tmp.name) / "docs"
        self.db = Database(Path(self.tmp.name) / "pipeline.db")
        self.addCleanup(self.db.close)

    def run_pipeline(self, http, **kw):
        return run(CITY, self.db, http, self.docs, days=14, today=TODAY, **kw)

    def rows(self, sql):
        return [dict(r) for r in self.db.conn.execute(sql)]


class ApiTests(PipelineTestCase):
    def test_api_run_stores_meetings_items_and_pdf_text(self):
        http = FakeHTTP(api_routes())
        summary = self.run_pipeline(http)

        self.assertEqual(summary.source, "api")
        self.assertEqual(summary.meetings_found, 2)  # Parks Board filtered out
        self.assertEqual(summary.new_items, 3)
        self.assertEqual(summary.pdfs_processed, 1)  # shared PDF processed once
        self.assertEqual(summary.non_pdf_attachments, 1)

        meetings = self.rows("SELECT city, body_name, meeting_date, external_id FROM meetings ORDER BY meeting_date")
        self.assertEqual(meetings, [
            {"city": "lees-summit", "body_name": "Public Works Committee", "meeting_date": "2026-10-15", "external_id": "1003"},
            {"city": "lees-summit", "body_name": "City Council", "meeting_date": "2026-10-20", "external_id": "1001"},
        ])
        docs = self.rows("SELECT agenda_number, item_title, document_name, status, text FROM item_documents_view "
                         "WHERE document_url IS NOT NULL ORDER BY meeting_date, document_name")
        self.assertEqual(len(docs), 3)
        report = [d for d in docs if d["document_name"] == "Staff Report"]
        self.assertTrue(all("Pryor Road widening staff report" in d["text"] for d in report))
        self.assertEqual(len(list(self.docs.glob("*.pdf"))), 1)

        events_call = next(c for c in http.calls if "/events?" in c[1])
        self.assertIn("EventDate+ge+datetime%272026-10-11%27", events_call[1])

    def test_rerun_only_adds_new_items(self):
        self.run_pipeline(FakeHTTP(api_routes()))
        http = FakeHTTP(api_routes())
        summary = self.run_pipeline(http)
        self.assertEqual(summary.meetings_found, 2)
        self.assertEqual(summary.new_meetings, 0)
        self.assertEqual(summary.new_items, 0)
        self.assertEqual(summary.pdfs_processed, 0)
        self.assertFalse(any("eventitems" in c[1] for c in http.calls))  # stored meetings skipped
        self.assertEqual(self.rows("SELECT COUNT(*) AS n FROM agenda_items")[0]["n"], 3)

    def test_falls_back_to_scraper_when_api_fails(self):
        routes = scrape_routes()
        routes[("GET", API + "/bodies")] = FakeResponse(500, '{"Message": "LegistarConnectionString not set"}')
        summary = self.run_pipeline(FakeHTTP(routes))
        self.assertEqual(summary.source, "scrape")
        self.assertEqual(summary.meetings_found, 1)

    def test_falls_back_when_no_requested_body_exists(self):
        routes = scrape_routes()
        routes[("GET", API + "/bodies")] = FakeResponse(200, json.dumps([{"BodyId": 1, "BodyName": "Other"}]))
        self.assertEqual(self.run_pipeline(FakeHTTP(routes)).source, "scrape")


class ScraperTests(PipelineTestCase):
    def test_scrape_run(self):
        http = FakeHTTP(scrape_routes())
        summary = self.run_pipeline(http, source="scrape")

        self.assertEqual(summary.source, "scrape")
        self.assertEqual(summary.meetings_found, 1)  # Parks Board + out-of-window filtered
        self.assertEqual(summary.new_items, 2)
        self.assertEqual(summary.pdfs_processed, 1)
        self.assertEqual(summary.unmatched_bodies, ["Public Works Committee"])  # never on the calendar

        m = self.rows("SELECT * FROM meetings")[0]
        self.assertEqual((m["external_id"], m["body_name"], m["meeting_time"]), ("1001", "City Council", "6:00 PM"))
        self.assertEqual(m["agenda_url"], SITE + "View.ashx?M=A&ID=1001&GUID=X")
        items = self.rows("SELECT agenda_number, title, matter_file FROM agenda_items ORDER BY agenda_number")
        self.assertEqual(items[1], {"agenda_number": "2.",
                                    "title": "Approval of an agreement for the Pryor Road widening project",
                                    "matter_file": "BILL NO. 26-101"})
        urls = {r["url"] for r in self.rows("SELECT url FROM documents")}
        self.assertEqual(urls, {PDF_URL, DOCX_URL})  # link outside the attachments list ignored

        posts = [c for c in http.calls if c[0] == "POST"]
        self.assertEqual(len(posts), 1)  # window inside one month -> only "This Month"
        self.assertEqual(posts[0][2]["__VIEWSTATE"], "vs123")
        self.assertEqual(posts[0][2]["ctl00$ContentPlaceHolder1$lstYears"], "This Month")

    def test_requests_last_month_when_window_spans_months(self):
        http = FakeHTTP(scrape_routes())
        LegistarScraper(CITY, http).fetch_meetings(date(2026, 9, 28), date(2026, 10, 5), CITY.bodies)
        periods = [c[2]["ctl00$ContentPlaceHolder1$lstYears"] for c in http.calls if c[0] == "POST"]
        self.assertEqual(periods, ["This Month", "Last Month"])

    def test_api_and_scrape_share_keys(self):
        """A meeting stored via the API is not duplicated when later scraped."""
        self.run_pipeline(FakeHTTP(api_routes()))
        summary = self.run_pipeline(FakeHTTP(scrape_routes()), source="scrape")
        self.assertEqual(summary.new_meetings, 0)
        self.assertEqual(summary.new_items, 0)


class PoliteSessionTests(unittest.TestCase):
    def test_user_agent_and_delay_between_requests(self):
        now = [100.0]
        sleeps = []
        session = PoliteSession(delay=1.0, sleep=lambda s: (sleeps.append(s), now.__setitem__(0, now[0] + s)),
                                clock=lambda: now[0])
        self.assertIn("kc-infra-pipeline", session.session.headers["User-Agent"])
        session.session.request = lambda *a, **k: FakeResponse(200, "ok")
        session.get("https://example.test/a")
        now[0] += 0.25
        session.get("https://example.test/b")
        session.get("https://example.test/c")
        self.assertEqual(sleeps, [0.75, 1.0])


if __name__ == "__main__":
    unittest.main()
