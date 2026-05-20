"""
run_tasks.py
============
Standalone runner for Kommo task extraction.

USAGE
─────
    python run_tasks.py                          # All tasks
    python run_tasks.py --open-only              # Only incomplete tasks
    python run_tasks.py --entity-type leads      # Tasks linked to leads
    python run_tasks.py --since 86400            # Updated in last 24h
    python run_tasks.py --output-dir /data/kommo
    python run_tasks.py --debug
"""

from __future__ import annotations

import argparse
import logging
import sys
import time
from pathlib import Path

from dotenv import load_dotenv
load_dotenv()

from auth.oauth import KommoOAuthClient, KommoOAuthError
from api.client import KommoAPIClient, KommoClientError
from api.tasks import TasksExtractor, TaskExtractionResult

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s — %(message)s",
    datefmt="%Y-%m-%dT%H:%M:%S",
    handlers=[logging.StreamHandler(sys.stdout)],
)
logger = logging.getLogger(__name__)


def _build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        description="Extract tasks from Kommo CRM to JSON",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    p.add_argument(
        "--open-only", action="store_true", dest="open_only",
        help="Extract only incomplete (open) tasks",
    )
    p.add_argument(
        "--slim", action="store_true",
        help="Also write tasks_slim.json with 6 core fields only",
    )
    p.add_argument(
        "--entity-type",
        choices=["leads", "contacts", "companies"],
        dest="entity_type",
        help="Filter tasks by linked entity type",
    )
    p.add_argument(
        "--since", type=int, metavar="SECONDS",
        help="Extract tasks updated in the last N seconds (e.g. 86400 = 24h)",
    )
    p.add_argument(
        "--since-ts", type=int, metavar="UNIX_TS", dest="since_ts",
        help="Extract tasks updated at or after this Unix timestamp",
    )
    p.add_argument(
        "--output-dir", type=Path, default=Path("outputs"),
        dest="output_dir", metavar="PATH",
    )
    p.add_argument("--debug", action="store_true")
    return p


def main() -> int:
    args = _build_parser().parse_args()

    if args.debug:
        logging.getLogger().setLevel(logging.DEBUG)

    print("\n" + "=" * 60)
    print("  Kommo Task Extraction")
    print("=" * 60 + "\n")

    # ------------------------------------------------------------------
    # 1. Initialise OAuth
    # ------------------------------------------------------------------
    try:
        oauth = KommoOAuthClient()
    except EnvironmentError as exc:
        print(f"\n❌  Config error: {exc}\n")
        return 1

    if not oauth.tokens_exist():
        print("\n❌  No tokens found. Run `python run_auth.py` first.\n")
        return 1

    # ------------------------------------------------------------------
    # 2. Resolve extraction mode
    # ------------------------------------------------------------------
    mode = "full"
    if args.open_only:
        mode = "open tasks only"
    elif args.entity_type:
        mode = f"entity_type={args.entity_type}"
    elif args.since:
        mode = f"updated in last {args.since}s"
    elif args.since_ts:
        mode = f"updated since ts={args.since_ts}"
    print(f"  Mode: {mode}\n")

    # ------------------------------------------------------------------
    # 3. Run extraction
    # ------------------------------------------------------------------
    result: TaskExtractionResult | None = None
    slim_path = None

    try:
        with KommoAPIClient(oauth) as client:

            try:
                account = client.health_check()
                logger.info("Connected: %s", account.get("name", "unknown"))
            except KommoClientError as exc:
                print(f"\n❌  API connectivity failed: {exc}\n")
                return 1

            extractor = TasksExtractor(client, output_dir=args.output_dir)

            if args.slim:
                result, slim_path = extractor.extract_slim()
            elif args.open_only:
                result = extractor.extract_open_tasks()
            elif args.entity_type:
                result = extractor.extract_for_entity(args.entity_type)
            elif args.since_ts:
                result = extractor.extract_updated_since(args.since_ts)
            elif args.since:
                since_ts = int(time.time()) - args.since
                result = extractor.extract_updated_since(since_ts)
            else:
                result = extractor.extract_all()

    except KommoOAuthError as exc:
        print(f"\n❌  Auth error: {exc}\n")
        return 1
    except KommoClientError as exc:
        print(f"\n❌  API error: {exc}\n")
        logger.exception("Unrecoverable API error")
        return 1
    except Exception as exc:
        print(f"\n❌  Unexpected error: {exc}\n")
        logger.exception("Unexpected error")
        return 1

    # ------------------------------------------------------------------
    # 4. Print summary
    # ------------------------------------------------------------------
    print("─" * 60)
    print("  Extraction Complete")
    print("─" * 60)
    print(f"  ✅  Total tasks  : {result.total_records:,}")
    print(f"  ✓   Completed   : {result.completed_count:,}")
    print(f"  ○   Open        : {result.total_records - result.completed_count:,}")

    if result.overdue_count:
        print(f"  ⚠️   Overdue     : {result.overdue_count:,}")

    print(f"  📄  Pages       : {result.pages_fetched}")
    print(f"  ⏱️   Duration    : {result.duration_seconds:.2f}s")

    if result.output_path:
        kb = result.output_path.stat().st_size / 1024
        print(f"  💾  Output      : {result.output_path}  ({kb:.1f} KB)")

    if slim_path:
        kb2 = slim_path.stat().st_size / 1024
        print(f"  📄  Slim output : {slim_path}  ({kb2:.1f} KB)")

    if result.failed_records:
        print(f"  ⚠️   Failed      : {result.failed_records} → {result.dead_letter_path}")
    else:
        print(f"  ✓   Validation  : All records passed")

    print("─" * 60 + "\n")
    return 0


if __name__ == "__main__":
    sys.exit(main())
