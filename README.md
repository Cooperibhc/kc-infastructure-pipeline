# kc-infra-pipeline

Monitors Kansas City-area city council agendas for infrastructure projects.

**Step 1 (this version):** a reader for the City of Lee's Summit, MO, which publishes
agendas on Legistar (<https://lsmo.legistar.com/Calendar.aspx>). It collects recent
meetings of the **City Council**, **Public Works Committee** and **Planning Commission**,
stores their agenda items in SQLite, downloads attached PDFs and extracts their text.

## Setup

Requires Python 3.9+.

```bash
python -m venv .venv
source .venv/bin/activate        # Windows: .venv\Scripts\activate
pip install -r requirements.txt
```

## Run

```bash
python run.py --city lees-summit
```

Example output:

```
Summary for Lee's Summit, MO (2026-10-11 to 2026-10-25, source: api)
  Meetings found:     5 (5 new)
  New items added:    87
  PDFs processed:     41
```

Options:

| Option | Default | Description |
| --- | --- | --- |
| `--days N` | `14` | How many days back to look (through today). |
| `--source auto\|api\|scrape` | `auto` | `auto` tries the Legistar Web API, then falls back to scraping. |
| `--db PATH` | `data/pipeline.db` | SQLite database file. |
| `--docs-dir PATH` | `data/docs` | Where downloaded PDFs are saved. |
| `--skip-pdfs` | off | Store meetings/items only; don't download PDFs. |
| `--delay SECONDS` | `1.0` | Pause between HTTP requests (never less than 1 second). |
| `-v` | off | Verbose logging. |

If the city's Legistar API requires a token, set `LEGISTAR_TOKEN` in the environment.

## How it works

1. **Legistar Web API first.** `GET https://webapi.legistar.com/v1/lsmo/bodies` verifies
   that the API responds and that the three bodies exist, then
   `GET /events?$filter=EventDate ge datetime'…'` lists meetings in the window and
   `GET /events/{id}/eventitems?Attachments=1` returns each meeting's agenda items with
   their attached documents.
2. **Scraping fallback.** If the API errors, returns non-JSON, or has none of the
   requested bodies, the reader scrapes `Calendar.aspx` (the default view plus the
   "This Month"/"Last Month" filters), each meeting's `MeetingDetail.aspx` agenda grid,
   and each item's `LegislationDetail.aspx` attachment list. The log says which source
   was used and the summary shows it as `source: api` or `source: scrape`. For windows longer
   than about a month, the scraper uses the "This Year"/"Last Year" filters instead. It does
   not follow calendar page links, so the API is the better choice for long look-backs.
3. **Storage / de-duplication.** Meetings are keyed by city + Legistar meeting ID, and
   items by meeting + agenda number + title. A re-run skips meetings already stored
   (unless the agenda had no items the last time it was checked) and only inserts new items.
   Documents are keyed by URL, so a staff report attached to items at several meetings is
   downloaded once.
4. **PDFs.** Attachments are downloaded to `data/docs/`. Files that aren't PDFs (Word
   documents, external links) are skipped and marked `not_pdf`. Text is extracted with
   pdfplumber and stored in the `documents.text` column. Failed downloads are retried on
   later runs, up to 3 attempts.

Each request sends a `User-Agent` string that identifies the project and is spaced at least
1 second after the previous one.

## Database

`data/pipeline.db` contains these tables:

- `meetings`: city, body_name, meeting_date, meeting_time, location, agenda_url, detail_url, source
- `agenda_items`: meeting_id, agenda_number, title, matter_file
- `documents`: url, name, status (`pending`/`done`/`not_pdf`/`error`), local_path, page_count, text
- `item_documents`: links items to documents
- `item_documents_view`: one row per item and document, with city, body, date, item number, title, document link and extracted text

```bash
sqlite3 data/pipeline.db \
  "SELECT meeting_date, body_name, agenda_number, item_title, document_url
   FROM item_documents_view ORDER BY meeting_date DESC LIMIT 20;"
```

## Tests

The tests run offline against hand-written API and HTML fixtures:

```bash
python -m unittest discover -s tests -v
```

## Adding a city

Add an entry to `CITIES` in `kc_infra_pipeline/config.py` with its Legistar client name
(the subdomain in `https://<client>.legistar.com`) and the exact body names it uses. If a
body name doesn't match, the summary reports it, and in API mode the log lists every body the city has.
