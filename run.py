#!/usr/bin/env python3
"""Command-line entry point.

    python run.py --city lees-summit
"""

import argparse
import logging
import sys
from pathlib import Path

from kc_infra_pipeline.config import CITIES, DEFAULT_LOOKBACK_DAYS, REQUEST_DELAY_SECONDS
from kc_infra_pipeline.db import Database
from kc_infra_pipeline.http import PoliteSession
from kc_infra_pipeline.legistar_api import LegistarAPIUnavailable
from kc_infra_pipeline.legistar_scraper import LegistarScrapeError
from kc_infra_pipeline.pipeline import run

ROOT = Path(__file__).resolve().parent


def main(argv=None):
    parser = argparse.ArgumentParser(description="Collect recent council agenda items into SQLite.")
    parser.add_argument("--city", required=True, choices=sorted(CITIES))
    parser.add_argument("--days", type=int, default=DEFAULT_LOOKBACK_DAYS,
                        help="look back this many days (default: %(default)s)")
    parser.add_argument("--source", choices=["auto", "api", "scrape"], default="auto",
                        help="auto = Legistar Web API, falling back to scraping (default)")
    parser.add_argument("--db", type=Path, default=ROOT / "data" / "pipeline.db")
    parser.add_argument("--docs-dir", type=Path, default=ROOT / "data" / "docs")
    parser.add_argument("--skip-pdfs", action="store_true", help="don't download/extract PDFs")
    parser.add_argument("--delay", type=float, default=REQUEST_DELAY_SECONDS,
                        help="seconds between HTTP requests; never less than 1 (default: %(default)s)")
    parser.add_argument("-v", "--verbose", action="store_true")
    args = parser.parse_args(argv)

    logging.basicConfig(level=logging.DEBUG if args.verbose else logging.INFO,
                        format="%(asctime)s %(levelname)s %(message)s", datefmt="%H:%M:%S")
    if args.verbose:  # keep urllib3/pdfminer chatter out of debug output
        for noisy in ("urllib3", "pdfminer"):
            logging.getLogger(noisy).setLevel(logging.INFO)

    db = Database(args.db)
    try:
        summary = run(CITIES[args.city], db, PoliteSession(delay=max(args.delay, 1.0)),
                      args.docs_dir, days=args.days, source=args.source,
                      process_pdfs=not args.skip_pdfs)
    except (LegistarAPIUnavailable, LegistarScrapeError) as exc:
        logging.error("Could not read meetings: %s", exc)
        return 1
    finally:
        db.close()
    print(summary.render())
    return 0


if __name__ == "__main__":
    sys.exit(main())
