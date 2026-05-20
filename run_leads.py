"""
run_leads.py
============
Standalone runner for Kommo lead extraction.

Extracts all leads from your Kommo account and writes them to
outputs/leads.json. Supports optional incremental extraction
via --since flag.

USAGE
─────
    # Full extraction (all leads)
    python run_leads.py

    # Incremental — only leads updated in the last 24 hours
    python run_leads.py --since 86400

    # Incremental — only leads updated since a specific Unix timestamp
    python run_leads.py --since-ts 1736588625

    # Custom output directory
    python run_leads.py --output-dir /data/kommo

EXECUTION FLOW
──────────────
    1. Load .env (credentials + config)
    2. Initialise KommoOAuthClient
       └── Validates env vars (KOMMO_CLIENT_ID, etc.)
    3. Validate tokens exist (state/token_store.json)
       └── If missing: print helpful error, exit 1
    4. Open KommoAPIClient session
       └── Connection pool opened, rate limiter initialised (7 req/s)
    5. LeadsExtractor.extract_all()
       ├── GET /api/v4/leads?page=1&limit=250
       │   └── Inject Bearer token → rate limit → execute
       │   └── Parse _embedded.leads → validate each → accumulate
       ├── GET /api/v4/leads?page=2&limit=250
       │   └── ... repeat until HTTP 204 or empty page
       └── Write outputs/leads.json (atomic)
    6. Print summary (total records, duration, output path)
    7. Exit 0

EXIT CODES
──────────
    0 — Extraction complete (validation errors → dead-letter file, not exit 1)
    1 — Fatal error (auth, config, unrecoverable API failure)
"""

from __future__ import annotations

import argparse
import logging
import sys
import time
from pathlib import Path

# Load .env BEFORE any module imports that read env vars
from dotenv import load_dotenv
load_dotenv()

from auth.oauth import KommoOAuthClient, KommoTokenMissingError, KommoOAuthError
from api.client import KommoAPIClient, KommoClientError
from api.leads import LeadsExtractor, ExtractionResult

# ---------------------------------------------------------------------------
# Logging — human-readable for CLI
# ---------------------------------------------------------------------------
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s — %(message)s",
    datefmt="%Y-%m-%dT%H:%M:%S",
    handlers=[logging.StreamHandler(sys.stdout)],
)
logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Argument Parser
# ---------------------------------------------------------------------------

def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Extract leads from Kommo CRM to JSON",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument(
        "--since",
        type=int,
        metavar="SECONDS",
        help="Extract leads updated in the last N seconds (e.g. 86400 = last 24h)",
    )
    parser.add_argument(
        "--since-ts",
        type=int,
        metavar="UNIX_TS",
        dest="since_ts",
        help="Extract leads updated at or after this Unix timestamp",
    )
    parser.add_argument(
        "--output-dir",
        type=Path,
        default=Path("outputs"),
        metavar="PATH",
        dest="output_dir",
        help="Output directory (default: outputs/)",
    )
    parser.add_argument(
        "--page-size",
        type=int,
        default=250,
        metavar="N",
        dest="page_size",
        help="Records per API page (default: 250, max: 250)",
    )
    parser.add_argument(
        "--debug",
        action="store_true",
        help="Enable DEBUG-level logging",
    )
    return parser


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main() -> int:
    """
    Entry point for lead extraction.

    Returns:
        0 on success, 1 on fatal failure.
    """
    args = _build_parser().parse_args()

    # Upgrade logging level if requested
    if args.debug:
        logging.getLogger().setLevel(logging.DEBUG)

    _print_banner("Kommo Lead Extraction")

    # ------------------------------------------------------------------
    # 1. Initialise OAuth client (validates all required env vars)
    # ------------------------------------------------------------------
    try:
        oauth = KommoOAuthClient()
    except EnvironmentError as exc:
        logger.critical("Configuration error: %s", exc)
        _print_error(str(exc), hint="Check your .env file. See .env.example for required vars.")
        return 1

    # ------------------------------------------------------------------
    # 2. Verify tokens exist (user must have run run_auth.py first)
    # ------------------------------------------------------------------
    if not oauth.tokens_exist():
        _print_error(
            "No token store found.",
            hint="Run `python run_auth.py` first to complete OAuth authorization.",
        )
        return 1

    # Validate token is readable (will also check expiry)
    try:
        info = oauth.token_info()
        logger.info(
            "Token status: expires in %.0f seconds",
            info["seconds_until_expiry"],
        )
    except KommoTokenMissingError as exc:
        _print_error(f"Token error: {exc}", hint="Re-run `python run_auth.py`.")
        return 1

    # ------------------------------------------------------------------
    # 3. Determine incremental filter (if any)
    # ------------------------------------------------------------------
    since_ts: int | None = None

    if args.since_ts:
        since_ts = args.since_ts
        logger.info("Incremental mode: updated_at >= %d", since_ts)

    elif args.since:
        since_ts = int(time.time()) - args.since
        logger.info(
            "Incremental mode: last %d seconds (updated_at >= %d)",
            args.since,
            since_ts,
        )
    else:
        logger.info("Full extraction mode: all leads")

    # ------------------------------------------------------------------
    # 4. Run extraction
    # ------------------------------------------------------------------
    result: ExtractionResult | None = None

    try:
        with KommoAPIClient(oauth) as client:

            # Health check — verify connectivity before starting
            try:
                account = client.health_check()
                logger.info(
                    "Connected to Kommo account: %s",
                    account.get("name", "unknown"),
                )
            except KommoClientError as exc:
                _print_error(
                    f"API connectivity check failed: {exc}",
                    hint="Check your KOMMO_ACCOUNT_DOMAIN and network connection.",
                )
                return 1

            extractor = LeadsExtractor(
                client=client,
                output_dir=args.output_dir,
                page_size=args.page_size,
            )

            if since_ts is not None:
                result = extractor.extract_updated_since(since_ts)
            else:
                result = extractor.extract_all()

    except KommoOAuthError as exc:
        _print_error(f"Authentication error: {exc}", hint="Re-run `python run_auth.py`.")
        return 1

    except KommoClientError as exc:
        _print_error(f"API error: {exc}")
        logger.exception("Unrecoverable API error during lead extraction")
        return 1

    except Exception as exc:
        _print_error(f"Unexpected error: {exc}")
        logger.exception("Unexpected error during lead extraction")
        return 1

    # ------------------------------------------------------------------
    # 5. Print summary
    # ------------------------------------------------------------------
    _print_summary(result)

    return 0


# ---------------------------------------------------------------------------
# CLI Formatting Helpers
# ---------------------------------------------------------------------------

def _print_banner(title: str) -> None:
    width = 60
    print("\n" + "=" * width)
    print(f"  {title}")
    print("=" * width + "\n")


def _print_error(message: str, hint: str | None = None) -> None:
    print(f"\n❌  Error: {message}")
    if hint:
        print(f"    → {hint}")
    print()


def _print_summary(result: ExtractionResult) -> None:
    print("\n" + "─" * 60)
    print("  Extraction Complete")
    print("─" * 60)
    print(f"  ✅  Total leads   : {result.total_records:,}")
    print(f"  📄  Pages fetched : {result.pages_fetched}")
    print(f"  ⏱️   Duration      : {result.duration_seconds:.2f}s")

    if result.output_path:
        size_kb = result.output_path.stat().st_size / 1024
        print(f"  💾  Output file   : {result.output_path}  ({size_kb:.1f} KB)")

    if result.failed_records:
        print(f"  ⚠️   Failed records : {result.failed_records} → {result.dead_letter_path}")
    else:
        print(f"  ✓   Validation    : All records passed")

    print("─" * 60 + "\n")


if __name__ == "__main__":
    sys.exit(main())
