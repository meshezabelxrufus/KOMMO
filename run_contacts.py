"""
run_contacts.py — Standalone CLI for Kommo contact extraction.

USAGE
-----
    python run_contacts.py                  # Full extraction
    python run_contacts.py --since 86400    # Last 24 hours
    python run_contacts.py --since-ts TS   # Since Unix timestamp
    python run_contacts.py --auto-incremental
    python run_contacts.py --no-leads      # Skip linked lead IDs
    python run_contacts.py --debug

EXIT CODES
----------
    0  Success   1  Partial (dead-letter exists)
    2  Auth/config error    3  API error
"""

from __future__ import annotations

import argparse
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

from dotenv import load_dotenv
load_dotenv()

from auth.oauth import KommoOAuthClient
from api.client import KommoAPIClient, KommoClientError
from api.contacts import ContactsExtractor, ContactExtractionResult
from utils.logger import configure_logging, get_logger
from utils.state_manager import StateManager


def _build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="run_contacts.py")
    g = p.add_mutually_exclusive_group()
    g.add_argument("--since", type=int, metavar="SECONDS")
    g.add_argument("--since-ts", type=int, dest="since_ts", metavar="TIMESTAMP")
    g.add_argument("--auto-incremental", action="store_true")
    p.add_argument("--output-dir", default="outputs")
    p.add_argument("--no-leads", action="store_true")
    p.add_argument("--debug", action="store_true")
    return p


def main() -> int:
    args = _build_parser().parse_args()
    configure_logging(level="DEBUG" if args.debug else "INFO")
    log  = get_logger(__name__)

    since_ts: int | None = None
    sm = StateManager()

    if args.since:
        since_ts = int(time.time()) - args.since
    elif args.since_ts:
        since_ts = args.since_ts
    elif args.auto_incremental:
        since_ts = sm.get_last_run_timestamp("contacts")
        if not since_ts:
            log.info("No previous contacts sync — running full extraction")

    try:
        oauth = KommoOAuthClient()
    except Exception as exc:
        log.error("Auth setup failed: %s", exc)
        return 2

    result: ContactExtractionResult | None = None
    try:
        with KommoAPIClient(oauth) as client:
            extractor = ContactsExtractor(
                client,
                output_dir=args.output_dir,
                include_leads=not args.no_leads,
            )
            result = extractor.extract_updated_since(since_ts) if since_ts else extractor.extract_all()

    except KommoClientError as exc:
        log.error("API error: %s", exc)
        sm.mark_failed("contacts", error=str(exc))
        return 3
    except Exception as exc:
        log.error("Unexpected error: %s", exc, exc_info=True)
        sm.mark_failed("contacts", error=str(exc))
        return 3

    if result.failed_records > 0:
        sm.mark_partial("contacts", records=result.total_records, failed_records=result.failed_records)
    else:
        sm.mark_success("contacts", records=result.total_records, pages=result.pages_fetched)

    print(f"\n  Contacts extracted : {result.total_records:,}")
    print(f"  With linked leads  : {result.contacts_with_leads:,}")
    print(f"  With phone         : {result.contacts_with_phone:,}")
    print(f"  With email         : {result.contacts_with_email:,}")
    print(f"  Dead-letter        : {result.failed_records:,}")
    if result.output_path:
        print(f"  Output             : {result.output_path}")
    print()
    return 1 if result.failed_records > 0 else 0


if __name__ == "__main__":
    sys.exit(main())
