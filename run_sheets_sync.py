#!/usr/bin/env python3
"""
run_sheets_sync.py
==================
Milestone 2 — Google Sheets sync runner.

Reads extracted JSON outputs produced by the Milestone 1 pipeline and
pushes them to the configured Google Spreadsheet in batch mode.

USAGE
─────
    # Activate venv first
    source .venv/bin/activate

    # Sync all worksheets (Leads + Messages + Daily_Summary)
    python run_sheets_sync.py

    # Sync leads only
    python run_sheets_sync.py --leads-only

    # Sync messages only
    python run_sheets_sync.py --messages-only

    # Sync without writing the daily summary row
    python run_sheets_sync.py --no-summary

    # Use custom output directory
    python run_sheets_sync.py --output-dir /path/to/outputs

    # Verbose debug logging
    python run_sheets_sync.py --debug

EXIT CODES
──────────
    0 — All syncs succeeded
    1 — One or more worksheet syncs failed (check logs)
    2 — Auth / config failure (missing env vars or credentials file)

PREREQUISITES
─────────────
    1. GOOGLE_SERVICE_ACCOUNT_FILE   — path to your service account JSON key
    2. GOOGLE_SHEETS_SPREADSHEET_ID  — ID from the spreadsheet URL
    3. The service account must have Editor access to the spreadsheet
    4. Run `python main.py` first to generate the JSON output files
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

# Load .env before any config/integration imports
from dotenv import load_dotenv
load_dotenv()

from utils.logger import configure_logging, get_logger
from integrations.google_sheets import (
    GoogleSheetsClient,
    GoogleSheetsAuthError,
    GoogleSheetsConfigError,
    GoogleSheetsWriteError,
    SheetsSyncResult,
    SheetsWriter,
    load_json_output,
)


# ---------------------------------------------------------------------------
# Argument parser
# ---------------------------------------------------------------------------

def _build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="run_sheets_sync.py",
        description="Kommo CRM — Google Sheets Sync (Milestone 2)",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__,
    )
    p.add_argument(
        "--output-dir", type=Path, default=Path("outputs"),
        dest="output_dir", metavar="PATH",
        help="Directory containing the extracted JSON files (default: outputs/)",
    )
    p.add_argument(
        "--leads-only", action="store_true", dest="leads_only",
        help="Sync only the Leads worksheet",
    )
    p.add_argument(
        "--messages-only", action="store_true", dest="messages_only",
        help="Sync only the Messages worksheet",
    )
    p.add_argument(
        "--no-summary", action="store_true", dest="no_summary",
        help="Skip writing the Daily_Summary row",
    )
    p.add_argument(
        "--debug", action="store_true",
        help="Enable DEBUG logging",
    )
    return p


# ---------------------------------------------------------------------------
# Per-entity sync helpers
# ---------------------------------------------------------------------------

def sync_leads(
    writer: SheetsWriter,
    output_dir: Path,
    log,
) -> SheetsSyncResult | None:
    """
    Load outputs/leads.json and write to the 'Leads' worksheet.

    Returns None if the file does not exist (skips gracefully).
    """
    leads_path = output_dir / "leads.json"

    if not leads_path.exists():
        log.warning(
            "leads.json not found — skipping Leads sync. "
            "Run `python main.py` first.",
            extra={"path": str(leads_path)},
        )
        return None

    log.info("Loading %s ...", leads_path)
    try:
        payload = load_json_output(leads_path)
    except (FileNotFoundError, json.JSONDecodeError, ValueError) as exc:
        log.error("Failed to load leads.json: %s", exc)
        return SheetsSyncResult(
            worksheet_name="Leads",
            success=False,
            error=str(exc),
        )

    records = payload.get("data") or []
    log.info("Leads loaded — %d records", len(records))

    result = writer.write_leads(records)
    return result


def sync_messages(
    writer: SheetsWriter,
    output_dir: Path,
    log,
) -> SheetsSyncResult | None:
    """
    Load outputs/messages_flat.json and write to the 'Messages' worksheet.

    Returns None if the file does not exist (skips gracefully).
    """
    # Support both possible filenames
    candidates = [
        output_dir / "messages_flat.json",
        output_dir / "chats.json",
        output_dir / "messages.json",
    ]
    msgs_path = next((p for p in candidates if p.exists()), None)

    if msgs_path is None:
        log.warning(
            "No messages file found in %s (tried: %s) — skipping Messages sync.",
            output_dir,
            ", ".join(str(c.name) for c in candidates),
        )
        return None

    log.info("Loading %s ...", msgs_path)
    try:
        payload = load_json_output(msgs_path)
    except (FileNotFoundError, json.JSONDecodeError, ValueError) as exc:
        log.error("Failed to load messages file: %s", exc)
        return SheetsSyncResult(
            worksheet_name="Messages",
            success=False,
            error=str(exc),
        )

    # Support both common top-level keys
    records = (
        payload.get("messages")
        or payload.get("data")
        or []
    )
    log.info("Messages loaded — %d records", len(records))

    result = writer.write_messages(records)
    return result


# ---------------------------------------------------------------------------
# Summary row
# ---------------------------------------------------------------------------

def sync_daily_summary(
    writer: SheetsWriter,
    leads_result: SheetsSyncResult | None,
    messages_result: SheetsSyncResult | None,
    log,
) -> SheetsSyncResult:
    """Write the Daily_Summary row using results from Leads + Messages syncs."""
    leads_count    = leads_result.rows_written    if leads_result    else 0
    messages_count = messages_result.rows_written if messages_result else 0

    notes_parts = []
    if leads_result and not leads_result.success:
        notes_parts.append("Leads sync FAILED")
    if messages_result and not messages_result.success:
        notes_parts.append("Messages sync FAILED")

    return writer.write_daily_summary(
        leads_count=leads_count,
        messages_count=messages_count,
        notes="; ".join(notes_parts) if notes_parts else "OK",
    )


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main() -> int:
    """
    Orchestrate the Milestone 2 Google Sheets sync.

    Returns:
        Exit code: 0=all success, 1=sync errors, 2=auth/config errors.
    """
    args = _build_parser().parse_args()

    level = "DEBUG" if args.debug else os.environ.get("LOG_LEVEL", "INFO")
    configure_logging(log_level=level, log_dir="logs")
    log = get_logger(__name__)

    run_start = time.monotonic()
    run_ts    = datetime.now(tz=timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")

    # ── Banner ────────────────────────────────────────────────────────────
    print("\n" + "═" * 65)
    print("  Kommo CRM — Google Sheets Sync  (Milestone 2)")
    print(f"  Started : {run_ts}")
    print("═" * 65 + "\n")

    # ── Authenticate ──────────────────────────────────────────────────────
    try:
        client = GoogleSheetsClient.from_env()
        print(f"  🔗 Connected: {client.spreadsheet_title}")
        print(f"     {client.spreadsheet_url}\n")
        log.info(
            "Connected to Google Sheets — title=%s id=%s",
            client.spreadsheet_title, client.spreadsheet_id,
        )
    except GoogleSheetsConfigError as exc:
        log.critical("Configuration error: %s", exc)
        print(f"\n  ❌  Config error: {exc}")
        print("      → Add GOOGLE_SERVICE_ACCOUNT_FILE and "
              "GOOGLE_SHEETS_SPREADSHEET_ID to your .env file.\n")
        return 2
    except GoogleSheetsAuthError as exc:
        log.critical("Authentication error: %s", exc)
        print(f"\n  ❌  Auth error: {exc}\n")
        return 2
    except Exception as exc:
        log.critical("Unexpected error during authentication: %s", exc, exc_info=True)
        print(f"\n  ❌  Unexpected error: {exc}\n")
        return 2

    writer  = SheetsWriter(client)
    results: list[SheetsSyncResult] = []

    # ── Leads sync ────────────────────────────────────────────────────────
    leads_result: SheetsSyncResult | None = None
    if not args.messages_only:
        log.info("Syncing Leads worksheet ...")
        leads_result = sync_leads(writer, args.output_dir, log)
        if leads_result:
            results.append(leads_result)

    # ── Messages sync ─────────────────────────────────────────────────────
    messages_result: SheetsSyncResult | None = None
    if not args.leads_only:
        log.info("Syncing Messages worksheet ...")
        messages_result = sync_messages(writer, args.output_dir, log)
        if messages_result:
            results.append(messages_result)

    # ── Daily Summary ─────────────────────────────────────────────────────
    if not args.no_summary:
        log.info("Writing Daily_Summary row ...")
        summary_result = sync_daily_summary(
            writer, leads_result, messages_result, log
        )
        results.append(summary_result)

    # ── Print results ─────────────────────────────────────────────────────
    total_duration = time.monotonic() - run_start
    any_failed     = any(not r.success for r in results)

    width = 65
    print("═" * width)
    if any_failed:
        print("  ⚠️   SHEETS SYNC COMPLETE — WITH ERRORS")
    else:
        print("  ✅  SHEETS SYNC COMPLETE")
    print("═" * width)

    for r in results:
        icon = "✅" if r.success else "❌"
        if r.success:
            print(f"  {icon}  {r.worksheet_name:<20} "
                  f"{r.rows_written:>8,} rows  [{r.duration_s:.1f}s]")
        else:
            print(f"  {icon}  {r.worksheet_name:<20} FAILED — {r.error}")

    print("─" * width)
    print(f"  Spreadsheet   : {client.spreadsheet_url}")
    print(f"  Total duration: {total_duration:.1f}s")
    print(f"  Timestamp     : {datetime.now(tz=timezone.utc).strftime('%Y-%m-%dT%H:%M:%SZ')}")
    print("═" * width + "\n")

    log.info(
        "SHEETS_SYNC_SUMMARY status=%s duration_s=%.1f sheets=%d",
        "FAILED" if any_failed else "SUCCESS",
        total_duration,
        len(results),
    )

    return 1 if any_failed else 0


if __name__ == "__main__":
    sys.exit(main())
