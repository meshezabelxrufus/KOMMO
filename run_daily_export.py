#!/usr/bin/env python3
"""
run_daily_export.py
===================
Milestone 2 — Daily AI-ready JSON export runner.

Reads outputs/messages_flat.json (produced by run_chats.py) and generates
one structured JSON file per calendar day in daily_exports/.

Each output file groups messages by lead, sorted chronologically, with
per-lead conversation statistics — ready for Claude AI analysis.

USAGE
─────
    # Activate venv first
    source .venv/bin/activate

    # Export latest day (most common — run after daily extraction)
    python run_daily_export.py

    # Export a specific date
    python run_daily_export.py --date 2025-01-15

    # Export all available dates
    python run_daily_export.py --all

    # List all dates available in messages_flat.json (no files written)
    python run_daily_export.py --list-dates

    # Use a custom messages file
    python run_daily_export.py --input /path/to/messages_flat.json

    # Use a custom output directory
    python run_daily_export.py --export-dir /path/to/daily_exports

    # Debug logging
    python run_daily_export.py --debug

EXIT CODES
──────────
    0 — All exports succeeded
    1 — One or more exports failed (partial success)
    2 — Input file missing or invalid
    3 — Date format error

PREREQUISITES
─────────────
    Run `python run_chats.py` first to generate outputs/messages_flat.json.
    If messages_flat.json does not exist, this script will exit with code 2.

OUTPUT FORMAT
─────────────
    daily_exports/
        2025-01-14.json
        2025-01-15.json
        ...

    Each file: { "_meta": {...}, "leads": [{lead_id, stats, messages}] }
"""

from __future__ import annotations

import argparse
import os
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

from dotenv import load_dotenv
load_dotenv()

from utils.logger import configure_logging, get_logger
from normalizers.daily_json_export import (
    DailyExportDateError,
    DailyExportError,
    DailyExportGenerator,
    DailyExportInputError,
    ExportResult,
    generate_daily_export,
)


# ---------------------------------------------------------------------------
# Argument parser
# ---------------------------------------------------------------------------

def _build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="run_daily_export.py",
        description="Kommo CRM — Daily AI-ready JSON export generator (Milestone 2)",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__,
    )

    # Mode selection
    mode = p.add_mutually_exclusive_group()
    mode.add_argument(
        "--date", type=str, metavar="YYYY-MM-DD",
        help=(
            "Export messages for this specific date. "
            "If omitted, auto-detects the latest date."
        ),
    )
    mode.add_argument(
        "--all", action="store_true",
        help="Generate export files for ALL dates found in the input file.",
    )
    mode.add_argument(
        "--list-dates", action="store_true", dest="list_dates",
        help="List all available dates in the input file and exit (no files written).",
    )

    # File paths
    p.add_argument(
        "--input", type=Path,
        default=Path("outputs/messages_flat.json"),
        metavar="FILE",
        help="Path to messages_flat.json (default: outputs/messages_flat.json)",
    )
    p.add_argument(
        "--export-dir", type=Path,
        default=Path("daily_exports"),
        dest="export_dir",
        metavar="DIR",
        help="Output directory for daily JSON files (default: daily_exports/)",
    )

    # Misc
    p.add_argument(
        "--debug", action="store_true",
        help="Enable DEBUG logging",
    )
    return p


# ---------------------------------------------------------------------------
# Helper: print result table
# ---------------------------------------------------------------------------

def _print_results(results: list[ExportResult], total_duration: float) -> None:
    """Print a formatted summary table of export results."""
    width = 70
    any_failed = any(not r.success for r in results)

    print("═" * width)
    if any_failed:
        print("  ⚠️   DAILY EXPORT COMPLETE — WITH ERRORS")
    else:
        print("  ✅  DAILY EXPORT COMPLETE")
    print("═" * width)

    for r in results:
        icon = "✅" if r.success else "❌"
        if r.success:
            kb = (r.output_path.stat().st_size // 1024
                  if r.output_path and r.output_path.exists() else 0)
            print(
                f"  {icon}  {r.date}  "
                f"{r.total_messages:>6,} msgs  "
                f"{r.total_leads:>4} leads  "
                f"[{r.duration_s:.2f}s]  {kb} KB"
            )
            if r.output_path:
                print(f"            📄 {r.output_path}")
        else:
            print(f"  {icon}  {r.date}  FAILED — {r.error}")

    total_msgs  = sum(r.total_messages for r in results)
    total_leads = sum(r.total_leads for r in results)

    print("─" * width)
    print(f"  Dates exported : {len(results)}")
    print(f"  Total messages : {total_msgs:,}")
    print(f"  Total leads    : {total_leads:,}")
    print(f"  Total duration : {total_duration:.2f}s")
    print(f"  Timestamp      : {datetime.now(tz=timezone.utc).strftime('%Y-%m-%dT%H:%M:%SZ')}")
    print("═" * width + "\n")


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main() -> int:
    """
    Orchestrate the daily AI-ready JSON export generation.

    Returns:
        Exit code: 0=success, 1=partial failures, 2=input error, 3=date error.
    """
    args = _build_parser().parse_args()

    level = "DEBUG" if args.debug else os.environ.get("LOG_LEVEL", "INFO")
    configure_logging(log_level=level, log_dir="logs")
    log = get_logger(__name__)

    run_start = time.monotonic()
    run_ts    = datetime.now(tz=timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")

    # ── Banner ────────────────────────────────────────────────────────────
    print("\n" + "═" * 70)
    print("  Kommo CRM — Daily AI Export Generator  (Milestone 2)")
    print(f"  Started : {run_ts}")
    print("═" * 70 + "\n")

    # ── Initialise generator ──────────────────────────────────────────────
    try:
        generator = DailyExportGenerator(
            input_file=args.input,
            export_dir=args.export_dir,
        )
    except Exception as exc:
        log.critical("Failed to initialise generator: %s", exc)
        print(f"\n  ❌  Init error: {exc}\n")
        return 2

    # ── --list-dates mode ─────────────────────────────────────────────────
    if args.list_dates:
        try:
            print(f"  📂 Input: {args.input}\n")
            dates = generator.list_available_dates()
            if not dates:
                print("  ⚠️  No dated messages found in the input file.")
                return 0
            print(f"  Available dates ({len(dates)} total):\n")
            for d in dates:
                print(f"    {d}")
            print(f"\n  Oldest : {dates[0]}")
            print(f"  Latest : {dates[-1]}")
            print(f"  Span   : {len(dates)} days\n")
            return 0
        except DailyExportInputError as exc:
            log.error("Input error: %s", exc)
            print(f"\n  ❌  {exc}\n")
            return 2

    # ── Run export ────────────────────────────────────────────────────────
    results: list[ExportResult] = []

    try:
        if args.all:
            print(f"  📂 Mode: ALL DATES from {args.input}\n")
            log.info("Mode: generate all dates")
            results = generator.generate_all()

        elif args.date:
            print(f"  📂 Mode: specific date — {args.date}\n")
            log.info("Mode: single date — %s", args.date)
            results = [generator.export_for_date(args.date)]

        else:
            # Default: export latest day
            print(f"  📂 Mode: latest date from {args.input}\n")
            log.info("Mode: latest day auto-detection")
            results = [generator.export_latest_day()]

    except DailyExportDateError as exc:
        log.error("Date error: %s", exc)
        print(f"\n  ❌  Date error: {exc}")
        print("      → Use YYYY-MM-DD format (e.g. --date 2025-01-15)\n")
        return 3

    except DailyExportInputError as exc:
        log.error("Input error: %s", exc)
        print(f"\n  ❌  Input error: {exc}")
        print("      → Run `python run_chats.py` first to generate messages_flat.json\n")
        return 2

    except DailyExportError as exc:
        log.error("Export error: %s", exc)
        print(f"\n  ❌  Export error: {exc}\n")
        return 1

    except Exception as exc:
        log.exception("Unexpected error: %s", exc)
        print(f"\n  ❌  Unexpected error: {exc}\n")
        return 1

    # ── Print summary ─────────────────────────────────────────────────────
    total_duration = time.monotonic() - run_start
    _print_results(results, total_duration)

    any_failed = any(not r.success for r in results)
    log.info(
        "DAILY_EXPORT_SUMMARY status=%s dates=%d duration_s=%.2f",
        "FAILED" if any_failed else "SUCCESS",
        len(results),
        total_duration,
    )

    return 1 if any_failed else 0


if __name__ == "__main__":
    sys.exit(main())
