"""
run_chats.py — Standalone CLI for Kommo chat and message extraction.

USAGE
-----
    python run_chats.py                     # Full extraction
    python run_chats.py --since 86400       # Chats active in last 24h
    python run_chats.py --since-ts TS       # Since Unix timestamp
    python run_chats.py --auto-incremental  # Read cursor from state
    python run_chats.py --debug             # Verbose logging

EXIT CODES
----------
    0  Success    1  Partial (dead-letter exists)
    2  Auth error  3  API error
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
from api.chats import ChatsExtractor, ChatExtractionResult
from utils.logger import configure_logging, get_logger
from utils.state_manager import StateManager


def _build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="run_chats.py", description="Extract Kommo chats and messages.")
    g = p.add_mutually_exclusive_group()
    g.add_argument("--since", type=int, metavar="SECONDS",
                   help="Only chats with messages in the last N seconds")
    g.add_argument("--since-ts", type=int, dest="since_ts", metavar="TIMESTAMP",
                   help="Only chats with messages since this Unix timestamp")
    g.add_argument("--auto-incremental", action="store_true",
                   help="Read last message cursor from sync state")
    p.add_argument("--output-dir", default="outputs", metavar="DIR")
    p.add_argument("--debug", action="store_true")
    return p


def main() -> int:
    args = _build_parser().parse_args()
    configure_logging(log_level="DEBUG" if args.debug else "INFO")
    log  = get_logger(__name__)
    sm   = StateManager()

    since_ts: int | None = None

    if args.since:
        since_ts = int(time.time()) - args.since
        log.info("Incremental: chats active since %s",
                 datetime.fromtimestamp(since_ts, tz=timezone.utc).isoformat())
    elif args.since_ts:
        since_ts = args.since_ts
    elif args.auto_incremental:
        since_ts = sm.get_last_message_timestamp("chats")
        if since_ts:
            log.info("Auto-incremental cursor: %s",
                     datetime.fromtimestamp(since_ts, tz=timezone.utc).isoformat())
        else:
            log.info("No previous chat cursor found — running full extraction")

    # ------------------------------------------------------------------
    # Auth
    # ------------------------------------------------------------------
    try:
        oauth      = KommoOAuthClient()
        token_info = oauth.token_info()
        log.info("Token valid — expires in %.0fs", token_info["seconds_until_expiry"])
    except Exception as exc:
        log.error("Auth setup failed: %s", exc)
        return 2

    # ------------------------------------------------------------------
    # Extract
    # ------------------------------------------------------------------
    result: ChatExtractionResult | None = None
    try:
        with KommoAPIClient(oauth) as client:
            extractor = ChatsExtractor(client, output_dir=args.output_dir)
            result = extractor.extract_since(since_ts) if since_ts else extractor.extract_all()

    except KommoClientError as exc:
        log.error("API error: %s", exc)
        sm.mark_failed("chats", error=str(exc))
        return 3
    except Exception as exc:
        log.error("Unexpected error: %s", exc, exc_info=True)
        sm.mark_failed("chats", error=str(exc))
        return 3

    # ------------------------------------------------------------------
    # Update sync state
    # ------------------------------------------------------------------
    if result.latest_message_ts:
        sm.set_last_message_timestamp("chats", timestamp=result.latest_message_ts)

    if result.failed_chats > 0 or result.failed_messages > 0:
        sm.mark_partial("chats",
                        records=result.total_messages,
                        failed_records=result.failed_messages)
    else:
        sm.mark_success("chats", records=result.total_messages)

    # ------------------------------------------------------------------
    # Summary
    # ------------------------------------------------------------------
    elapsed = time.monotonic()

    print()
    print("─" * 55)
    print("  Kommo Chat Extraction — Complete")
    print("─" * 55)
    print(f"  Chat threads       : {result.total_chats:,}")
    print(f"  Total messages     : {result.total_messages:,}")
    print(f"  Inbound            : {result.inbound_messages:,}")
    print(f"  Outbound           : {result.outbound_messages:,}")
    print(f"  Failed chats       : {result.failed_chats:,}")
    print(f"  Failed messages    : {result.failed_messages:,}")
    if result.latest_message_ts:
        print(f"  Latest message at  : {datetime.fromtimestamp(result.latest_message_ts, tz=timezone.utc).isoformat()}")
    if result.output_path:
        size_kb = Path(result.output_path).stat().st_size // 1024
        print(f"  Chats JSON         : {result.output_path} ({size_kb} KB)")
    if result.flat_output_path:
        size_kb = Path(result.flat_output_path).stat().st_size // 1024
        print(f"  Flat messages JSON : {result.flat_output_path} ({size_kb} KB)")
    print("─" * 55)
    print()

    return 1 if (result.failed_chats + result.failed_messages) > 0 else 0


if __name__ == "__main__":
    sys.exit(main())
