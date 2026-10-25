"""Download attached documents and extract PDF text with pdfplumber."""

import hashlib
import logging
import os
import re
from pathlib import Path
from urllib.parse import unquote, urlparse

import pdfplumber

log = logging.getLogger(__name__)

MAX_DOWNLOAD_BYTES = 100 * 1024 * 1024


def local_filename(url, name):
    """Stable, filesystem-safe name: <hash-of-url>_<sanitized name>.pdf"""
    digest = hashlib.sha1(url.encode("utf-8")).hexdigest()[:12]
    stem = name or Path(unquote(urlparse(url).path)).stem
    stem = re.sub(r"[^A-Za-z0-9._-]+", "_", stem).strip("._")[:80] or "document"
    if stem.lower().endswith(".pdf"):
        stem = stem[:-4]
    return f"{digest}_{stem}.pdf"


def download(http, url, dest):
    """Download ``url`` to ``dest``. Returns False (and writes nothing) if it isn't a PDF."""
    dest = Path(dest)
    dest.parent.mkdir(parents=True, exist_ok=True)
    tmp = dest.with_name(dest.name + ".part")
    resp = http.get(url, stream=True)
    try:
        resp.raise_for_status()
        chunks = resp.iter_content(chunk_size=64 * 1024)
        first = b""
        for chunk in chunks:  # find the first non-empty chunk to sniff the type
            if chunk:
                first = chunk
                break
        if not first.lstrip()[:5].startswith(b"%PDF"):
            return False
        size = 0
        with open(tmp, "wb") as fh:
            for chunk in [first, *_rest(chunks)]:
                size += len(chunk)
                if size > MAX_DOWNLOAD_BYTES:
                    raise ValueError(f"document larger than {MAX_DOWNLOAD_BYTES} bytes")
                fh.write(chunk)
        os.replace(tmp, dest)
        return True
    finally:
        resp.close()
        if tmp.exists():
            tmp.unlink()


def _rest(chunks):
    for chunk in chunks:
        if chunk:
            yield chunk


def extract_text(path):
    """Return ``(page_count, text)`` for the PDF at ``path``."""
    pages = []
    with pdfplumber.open(path) as pdf:
        for page in pdf.pages:
            pages.append(page.extract_text() or "")
            page.close()
        return len(pdf.pages), "\n\n".join(pages).strip()


def process_documents(db, http, docs_dir):
    """Download and extract every pending document. Returns counts by outcome."""
    counts = {"processed": 0, "not_pdf": 0, "errors": 0}
    docs_dir = Path(docs_dir)
    for doc in db.pending_documents():
        dest = docs_dir / local_filename(doc["url"], doc["name"])
        try:
            if not dest.exists() and not download(http, doc["url"], dest):
                log.info("Skipping non-PDF attachment: %s", doc["url"])
                db.save_document_result(doc["id"], "not_pdf")
                counts["not_pdf"] += 1
                continue
            page_count, text = extract_text(dest)
        except Exception as exc:  # network errors, HTTP errors, unreadable/corrupt PDFs
            log.warning("Failed to process %s: %s", doc["url"], exc)
            dest.unlink(missing_ok=True)  # re-download on the next attempt
            db.save_document_result(doc["id"], "error", error=str(exc)[:500])
            counts["errors"] += 1
            continue
        db.save_document_result(doc["id"], "done", local_path=str(dest),
                                page_count=page_count, text=text)
        counts["processed"] += 1
        log.info("Extracted %d pages from %s", page_count, dest.name)
    return counts
