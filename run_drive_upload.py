#!/usr/bin/env python3
"""
run_drive_upload.py
===================
Milestone 2 — Google Drive upload runner.

Uploads daily AI-ready JSON exports (daily_exports/YYYY-MM-DD.json)
to a designated Google Drive folder for Claude AI access.

USAGE
─────
    # Activate venv first
    source .venv/bin/activate

    # Upload latest export (most common — run after run_daily_export.py)
    python run_drive_upload.py

    # Upload a specific date
    python run_drive_upload.py --date 2025-01-15

    # Upload ALL local export files
    python run_drive_upload.py --all

    # Upload all, skipping files already in Drive
    python run_drive_upload.py --all --skip-existing

    # List files currently in Drive (no upload)
    python run_drive_upload.py --list

    # Delete a specific file from Drive
    python run_drive_upload.py --delete 2025-01-15.json

    # Upload a specific file by path
    python run_drive_upload.py --file /path/to/custom.json

    # Debug logging
    python run_drive_upload.py --debug

EXIT CODES
──────────
    0 — All uploads succeeded
    1 — One or more uploads failed (partial success)
    2 — Auth / config failure (missing env vars or credentials file)
    3 — File not found (local export missing)

PREREQUISITES
─────────────
    1. GOOGLE_SERVICE_ACCOUNT_FILE   — path to your service account JSON key
    2. GOOGLE_DRIVE_FOLDER_ID        — ID from the Drive folder URL
    3. The service account must have Editor access to the target folder
    4. Run `python run_daily_export.py` first to generate JSON export files
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
from integrations.google_drive import (
    DriveFileInfo,
    DriveUploadResult,
    DriveUploader,
    GoogleDriveAuthError,
    GoogleDriveClient,
    GoogleDriveConfigError,
    GoogleDriveNotFoundError,
    GoogleDriveUploadError,
)


# ---------------------------------------------------------------------------
# Argument parser
# ---------------------------------------------------------------------------

def _build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="run_drive_upload.py",
        description="Kommo CRM — Google Drive Upload Runner (Milestone 2)",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__,
    )

    # Mode selection (mutually exclusive)
    mode = p.add_mutually_exclusive_group()
    mode.add_argument(
        "--date", type=str, metavar="YYYY-MM-DD",
        help="Upload the export for this specific date.",
    )
    mode.add_argument(
        "--all", action="store_true",
        help="Upload ALL local YYYY-MM-DD.json files to Drive.",
    )
    mode.add_argument(
        "--list", action="store_true",
        help="List files currently in Drive (no upload).",
    )
    mode.add_argument(
        "--delete", type=str, metavar="FILENAME",
        help="Delete a specific file from Drive by filename (e.g. 2025-01-15.json).",
    )
    mode.add_argument(
        "--file", type=Path, metavar="PATH",
        help="Upload a specific file by its local path.",
    )

    # Options
    p.add_argument(
        "--skip-existing", action="store_true", dest="skip_existing",
        help="When using --all, skip files already present in Drive.",
    )
    p.add_argument(
        "--export-dir", type=Path, default=Path("daily_exports"),
        dest="export_dir", metavar="DIR",
        help="Local directory containing export files (default: daily_exports/)",
    )
    p.add_argument(
        "--debug", action="store_true",
        help="Enable DEBUG logging.",
    )
    return p


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _print_upload_results(
    results: list[DriveUploadResult],
    total_duration: float,
    folder_url: str,
) -> None:
    """Print a formatted summary table of upload results."""
    width = 72
    any_failed = any(not r.success for r in results)

    print("═" * width)
    if any_failed:
        print("  ⚠️   DRIVE UPLOAD COMPLETE — WITH ERRORS")
    else:
        print("  ✅  DRIVE UPLOAD COMPLETE")
    print("═" * width)

    for r in results:
        icon   = "✅" if r.success else "❌"
        action = f"[{r.action_label}]" if r.success else "[FAILED]"
        kb     = r.size_bytes // 1024 if r.size_bytes else 0

        if r.success:
            print(f"  {icon}  {r.filename:<25} {action:<12} {kb:>5} KB  [{r.duration_s:.2f}s]")
            print(f"       🔗 {r.web_view_link}")
        else:
            print(f"  {icon}  {r.filename:<25} FAILED — {r.error}")

    total_size = sum(r.size_bytes for r in results if r.success)

    print("─" * width)
    print(f"  Files uploaded   : {len(results)}")
    print(f"  Total size       : {total_size // 1024:,} KB")
    print(f"  Drive folder     : {folder_url}")
    print(f"  Total duration   : {total_duration:.2f}s")
    print(f"  Timestamp        : {datetime.now(tz=timezone.utc).strftime('%Y-%m-%dT%H:%M:%SZ')}")
    print("═" * width + "\n")


def _print_drive_listing(files: list[DriveFileInfo], folder_url: str) -> None:
    """Print a table of files currently in Drive."""
    width = 72
    print("═" * width)
    print(f"  📂  Google Drive — Uploaded Exports ({len(files)} files)")
    print("═" * width)

    if not files:
        print("  (no dated exports found in Drive folder)")
    else:
        for f in files:
            kb    = f"{f.size_bytes // 1024:,} KB" if f.size_bytes else "n/a"
            mod   = f.modified_at[:10] if f.modified_at else ""
            print(f"  📄  {f.filename:<25} {kb:>8}  modified: {mod}")
            print(f"       🔗 {f.web_view_link}")

    print("─" * width)
    print(f"  Folder : {folder_url}")
    print("═" * width + "\n")


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main() -> int:
    """
    Orchestrate the Google Drive upload.

    Returns:
        Exit code: 0=success, 1=upload errors, 2=auth/config errors, 3=not found.
    """
    args = _build_parser().parse_args()

    level = "DEBUG" if args.debug else os.environ.get("LOG_LEVEL", "INFO")
    configure_logging(log_level=level, log_dir="logs")
    log = get_logger(__name__)

    run_start = time.monotonic()
    run_ts    = datetime.now(tz=timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")

    # ── Banner ────────────────────────────────────────────────────────────
    print("\n" + "═" * 72)
    print("  Kommo CRM — Google Drive Upload Runner  (Milestone 2)")
    print(f"  Started : {run_ts}")
    print("═" * 72 + "\n")

    # ── Authenticate ──────────────────────────────────────────────────────
    try:
        client   = GoogleDriveClient.from_env()
        uploader = DriveUploader(client, export_dir=args.export_dir)

        # Validate folder access (also surfaces permission errors early)
        folder_meta = client.get_folder_metadata()
        folder_name = folder_meta.get("name", client.folder_id)
        print(f"  🔗 Connected to Drive folder: {folder_name!r}")
        print(f"     {client.folder_url}\n")
        log.info(
            "Drive folder validated — name=%s id=%s",
            folder_name, client.folder_id,
        )

    except GoogleDriveConfigError as exc:
        log.critical("Configuration error: %s", exc)
        print(f"\n  ❌  Config error: {exc}")
        print(
            "      → Add GOOGLE_SERVICE_ACCOUNT_FILE and "
            "GOOGLE_DRIVE_FOLDER_ID to your .env file.\n"
        )
        return 2
    except GoogleDriveAuthError as exc:
        log.critical("Authentication error: %s", exc)
        print(f"\n  ❌  Auth error: {exc}\n")
        return 2
    except GoogleDriveNotFoundError as exc:
        log.critical("Folder not found: %s", exc)
        print(f"\n  ❌  Drive folder not found: {exc}")
        print("      → Check GOOGLE_DRIVE_FOLDER_ID in your .env file.\n")
        return 2
    except Exception as exc:
        log.critical("Unexpected auth error: %s", exc, exc_info=True)
        print(f"\n  ❌  Unexpected error: {exc}\n")
        return 2

    # ── --list mode ───────────────────────────────────────────────────────
    if args.list:
        try:
            files = uploader.list_uploaded_exports()
            _print_drive_listing(files, client.folder_url)
            return 0
        except GoogleDriveUploadError as exc:
            log.error("List failed: %s", exc)
            print(f"\n  ❌  {exc}\n")
            return 1

    # ── --delete mode ─────────────────────────────────────────────────────
    if args.delete:
        filename = args.delete
        print(f"  🗑️  Deleting '{filename}' from Drive ...\n")
        try:
            deleted = uploader.delete_existing_file_if_present(filename)
            if deleted:
                print(f"  ✅  '{filename}' deleted successfully.\n")
                return 0
            else:
                print(f"  ⚠️   '{filename}' was not found in Drive.\n")
                return 0
        except GoogleDriveUploadError as exc:
            log.error("Delete failed: %s", exc)
            print(f"\n  ❌  Delete failed: {exc}\n")
            return 1

    # ── Upload modes ──────────────────────────────────────────────────────
    results: list[DriveUploadResult] = []

    try:
        if args.file:
            # Upload explicit file path
            print(f"  📂 Mode: specific file → {args.file}\n")
            log.info("Mode: upload specific file — %s", args.file)
            result = uploader.upload_daily_export(file_path=args.file)
            results = [result]

        elif args.date:
            # Upload specific date
            print(f"  📂 Mode: specific date → {args.date}.json\n")
            log.info("Mode: upload date — %s", args.date)
            result = uploader.upload_daily_export(date=args.date)
            results = [result]

        elif args.all:
            # Upload all local exports
            skip = args.skip_existing
            print(
                f"  📂 Mode: ALL exports from {args.export_dir}"
                + (" [skip existing]" if skip else "") + "\n"
            )
            log.info("Mode: upload all — skip_existing=%s", skip)
            results = uploader.upload_all_exports(skip_existing=skip)

        else:
            # Default: latest export
            print(f"  📂 Mode: latest export from {args.export_dir}\n")
            log.info("Mode: upload latest")
            result = uploader.upload_latest_export()
            results = [result]

    except FileNotFoundError as exc:
        log.error("Local file not found: %s", exc)
        print(f"\n  ❌  File not found: {exc}")
        print("      → Run `python run_daily_export.py` first.\n")
        return 3

    except GoogleDriveNotFoundError as exc:
        log.error("No exports to upload: %s", exc)
        print(f"\n  ❌  {exc}")
        print("      → Run `python run_daily_export.py` first.\n")
        return 3

    except ValueError as exc:
        log.error("Invalid argument: %s", exc)
        print(f"\n  ❌  {exc}\n")
        return 1

    except (GoogleDriveUploadError, GoogleDriveError) as exc:
        log.error("Upload error: %s", exc)
        print(f"\n  ❌  Upload error: {exc}\n")
        return 1

    except Exception as exc:
        log.exception("Unexpected error: %s", exc)
        print(f"\n  ❌  Unexpected error: {exc}\n")
        return 1

    # ── Print summary ─────────────────────────────────────────────────────
    total_duration = time.monotonic() - run_start
    _print_upload_results(results, total_duration, client.folder_url)

    any_failed = any(not r.success for r in results)
    log.info(
        "DRIVE_UPLOAD_SUMMARY status=%s files=%d duration_s=%.2f",
        "FAILED" if any_failed else "SUCCESS",
        len(results),
        total_duration,
    )

    return 1 if any_failed else 0


if __name__ == "__main__":
    sys.exit(main())
