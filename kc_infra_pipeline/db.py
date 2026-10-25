"""SQLite storage. Re-runs are idempotent: meetings, items and documents are keyed
so that existing rows are skipped and only new ones are inserted."""

import sqlite3
from datetime import datetime, timezone
from pathlib import Path

SCHEMA = """
CREATE TABLE IF NOT EXISTS meetings (
    id               INTEGER PRIMARY KEY,
    city             TEXT NOT NULL,
    source           TEXT NOT NULL,           -- 'api' or 'scrape'
    external_id      TEXT NOT NULL,           -- Legistar EventId when known
    body_name        TEXT NOT NULL,
    meeting_date     TEXT NOT NULL,           -- ISO date
    meeting_time     TEXT,
    location         TEXT,
    agenda_url       TEXT,
    detail_url       TEXT,
    items_fetched_at TEXT,
    created_at       TEXT NOT NULL,
    UNIQUE (city, external_id)
);

CREATE TABLE IF NOT EXISTS agenda_items (
    id            INTEGER PRIMARY KEY,
    meeting_id    INTEGER NOT NULL REFERENCES meetings(id),
    agenda_number TEXT NOT NULL DEFAULT '',
    title         TEXT NOT NULL,
    matter_file   TEXT,
    detail_url    TEXT,
    created_at    TEXT NOT NULL,
    UNIQUE (meeting_id, agenda_number, title)
);

CREATE TABLE IF NOT EXISTS documents (
    id           INTEGER PRIMARY KEY,
    url          TEXT NOT NULL UNIQUE,
    name         TEXT,
    status       TEXT NOT NULL DEFAULT 'pending',  -- pending | done | not_pdf | error
    attempts     INTEGER NOT NULL DEFAULT 0,
    local_path   TEXT,
    page_count   INTEGER,
    text         TEXT,
    error        TEXT,
    processed_at TEXT
);

CREATE TABLE IF NOT EXISTS item_documents (
    item_id     INTEGER NOT NULL REFERENCES agenda_items(id),
    document_id INTEGER NOT NULL REFERENCES documents(id),
    PRIMARY KEY (item_id, document_id)
);

CREATE VIEW IF NOT EXISTS item_documents_view AS
SELECT m.city, m.body_name, m.meeting_date, i.agenda_number, i.title AS item_title,
       d.name AS document_name, d.url AS document_url, d.status, d.local_path, d.text
FROM agenda_items i
JOIN meetings m ON m.id = i.meeting_id
LEFT JOIN item_documents x ON x.item_id = i.id
LEFT JOIN documents d ON d.id = x.document_id;
"""

MAX_DOCUMENT_ATTEMPTS = 3


def _now():
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


class Database:
    def __init__(self, path):
        if str(path) != ":memory:":
            Path(path).parent.mkdir(parents=True, exist_ok=True)
        self.conn = sqlite3.connect(str(path))
        self.conn.row_factory = sqlite3.Row
        self.conn.execute("PRAGMA foreign_keys = ON")
        self.conn.executescript(SCHEMA)

    def close(self):
        self.conn.close()

    # -- meetings ----------------------------------------------------------

    def add_meeting(self, city, meeting):
        """Insert ``meeting`` unless already stored. Returns ``(meeting_id, is_new)``."""
        with self.conn:
            cur = self.conn.execute(
                """INSERT OR IGNORE INTO meetings
                   (city, source, external_id, body_name, meeting_date, meeting_time,
                    location, agenda_url, detail_url, created_at)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (city, meeting.source, meeting.external_id, meeting.body_name,
                 meeting.meeting_date.isoformat(), meeting.meeting_time, meeting.location,
                 meeting.agenda_url, meeting.detail_url, _now()))
            is_new = cur.rowcount == 1
        row = self.conn.execute("SELECT id FROM meetings WHERE city = ? AND external_id = ?",
                                (city, meeting.external_id)).fetchone()
        return row["id"], is_new

    def meeting_items_complete(self, meeting_id):
        """True when the meeting's agenda was already fetched and had items."""
        row = self.conn.execute(
            """SELECT m.items_fetched_at IS NOT NULL AS fetched,
                      (SELECT COUNT(*) FROM agenda_items WHERE meeting_id = m.id) AS n
               FROM meetings m WHERE m.id = ?""", (meeting_id,)).fetchone()
        return bool(row and row["fetched"] and row["n"] > 0)

    def mark_items_fetched(self, meeting_id):
        with self.conn:
            self.conn.execute("UPDATE meetings SET items_fetched_at = ? WHERE id = ?",
                              (_now(), meeting_id))

    # -- items & documents --------------------------------------------------

    def add_item(self, meeting_id, item):
        """Insert an agenda item and link its attachments. Returns True if the item is new."""
        with self.conn:
            cur = self.conn.execute(
                """INSERT OR IGNORE INTO agenda_items
                   (meeting_id, agenda_number, title, matter_file, detail_url, created_at)
                   VALUES (?, ?, ?, ?, ?, ?)""",
                (meeting_id, item.agenda_number or "", item.title, item.matter_file,
                 item.detail_url, _now()))
            is_new = cur.rowcount == 1
            item_id = self.conn.execute(
                "SELECT id FROM agenda_items WHERE meeting_id = ? AND agenda_number = ? AND title = ?",
                (meeting_id, item.agenda_number or "", item.title)).fetchone()["id"]
            for att in item.attachments:
                self.conn.execute("INSERT OR IGNORE INTO documents (url, name) VALUES (?, ?)",
                                  (att.url, att.name))
                doc_id = self.conn.execute("SELECT id FROM documents WHERE url = ?",
                                           (att.url,)).fetchone()["id"]
                self.conn.execute(
                    "INSERT OR IGNORE INTO item_documents (item_id, document_id) VALUES (?, ?)",
                    (item_id, doc_id))
        return is_new

    def pending_documents(self):
        return self.conn.execute(
            """SELECT id, url, name, attempts FROM documents
               WHERE status IN ('pending', 'error') AND attempts < ?
               ORDER BY id""", (MAX_DOCUMENT_ATTEMPTS,)).fetchall()

    def save_document_result(self, doc_id, status, local_path=None, page_count=None,
                             text=None, error=None):
        with self.conn:
            self.conn.execute(
                """UPDATE documents SET status = ?, attempts = attempts + 1, local_path = ?,
                          page_count = ?, text = ?, error = ?, processed_at = ?
                   WHERE id = ?""",
                (status, local_path, page_count, text, error, _now(), doc_id))
