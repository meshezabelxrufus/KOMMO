"""
main.py
=======
Master orchestrator for the Kommo CRM — Milestone 1 + Milestone 2 pipeline.

ORCHESTRATION FLOW
──────────────────
  Phase A — Extraction (Milestone 1)
    Step 1  → Validate OAuth token
    Step 2  → Refresh token if needed
    Step 3  → Fetch Pipelines + stages      → outputs/pipelines.json
    Step 4  → Fetch Leads                   → outputs/leads.json
    Step 5  → Fetch Tasks                   → outputs/tasks.json
    Step 6  → Verify output files

  Phase B — AI Export (Milestone 2)
    Step 7  → Generate daily AI JSON export → daily_exports/YYYY-MM-DD.json

  Phase C — Google Integrations (Milestone 2)
    Step 8  → Sync Google Sheets            → Leads / Messages / Daily_Summary
    Step 9  → Upload exports to Drive       → daily_exports/*.json → Drive

  Phase D — Analytics
    Step 10 → Generate analytics summary    → logs/analytics_YYYY-MM-DD.json

  Phase E — Finalization
    Step 11 → Final structured run report

USAGE
─────
    # Activate venv first
    source .venv/bin/activate

    # Full M1+M2 pipeline (default)
    python main.py

    # Incremental extraction
    python main.py --auto-incremental

    # Extraction + exports only (skip Google integrations)
    python main.py --skip-sheets --skip-drive

    # Extraction only (pure M1)
    python main.py --extraction-only

    # Debug logging
    python main.py --debug

EXIT CODES
──────────
    0 — All phases succeeded (or non-critical phases degraded gracefully)
    1 — One or more critical steps failed
    2 — Auth / config failure (nothing ran)

SCALABILITY NOTES
─────────────────
  - Phases B–D are optional and degrade gracefully if credentials are absent.
  - Add new entities in _build_extraction_steps().
  - Add new integrations by registering a PipelineStep in _build_pipeline().
  - --fail-fast aborts the whole run on any step failure.
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import sys
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Optional

# Load .env before any module that reads config
from dotenv import load_dotenv
load_dotenv()

# ---------------------------------------------------------------------------
# Project imports
# ---------------------------------------------------------------------------
from auth.oauth import (
    KommoOAuthClient,
    KommoOAuthError,
    KommoTokenMissingError,
    KommoAuthorizationError,
)
from api.client import KommoAPIClient, KommoClientError
from api.leads import LeadsExtractor, ExtractionResult
from api.pipelines import PipelinesExtractor, PipelineExtractionResult
from api.tasks import TasksExtractor, TaskExtractionResult
from api.chats import ChatsExtractor, ChatExtractionResult
from utils.state_manager import StateManager
from utils.logger import configure_logging, get_logger
from utils.exceptions import KommoConfigError


# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------
PHASE_EXTRACTION   = "Extraction"
PHASE_EXPORT       = "AI Export"
PHASE_SHEETS       = "Google Sheets"
PHASE_DRIVE        = "Google Drive"
PHASE_ANALYTICS    = "Analytics"
PHASE_FINAL        = "Finalization"


# ---------------------------------------------------------------------------
# Data structures
# ---------------------------------------------------------------------------

@dataclass
class StepResult:
    """
    Outcome of a single pipeline step.

    Attributes:
        name:        Human-readable step name (e.g. "Leads").
        phase:       Pipeline phase this step belongs to.
        success:     True if the step completed without a fatal error.
        records:     Number of records processed.
        skipped:     Number of records that failed validation / dead-lettered.
        output_path: Path to the primary output file (None if nothing written).
        error:       Exception message if the step failed.
        duration_s:  Wall-clock time for this step in seconds.
        metadata:    Arbitrary key/value pairs for the analytics summary.
        critical:    If False, failure degrades gracefully (doesn't set exit 1).
    """
    name:        str
    phase:       str            = PHASE_EXTRACTION
    success:     bool           = True
    records:     int            = 0
    skipped:     int            = 0
    output_path: Path | None    = None
    error:       str | None     = None
    duration_s:  float          = 0.0
    metadata:    dict[str, Any] = field(default_factory=dict)
    critical:    bool           = True   # True = failure triggers exit 1

    @property
    def status_icon(self) -> str:
        if not self.success:
            return "❌"
        if self.skipped:
            return "⚠️ "
        return "✅"

    @property
    def status_label(self) -> str:
        if not self.success:
            return "FAILED"
        if self.skipped:
            return "PARTIAL"
        return "SUCCESS"


# ---------------------------------------------------------------------------
# Argument parser
# ---------------------------------------------------------------------------

def _build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="main.py",
        description="Kommo CRM — Full M1+M2 Pipeline Orchestrator",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__,
    )

    # ── Extraction options ────────────────────────────────────────────────
    ex = p.add_argument_group("Extraction")
    ex.add_argument(
        "--since", type=int, metavar="SECONDS",
        help="Incremental mode: only extract records updated in the last N seconds",
    )
    ex.add_argument(
        "--since-ts", type=int, metavar="UNIX_TS", dest="since_ts",
        help="Incremental mode: only extract records updated at or after this Unix timestamp",
    )
    ex.add_argument(
        "--auto-incremental", action="store_true", dest="auto_incremental",
        help="Auto-detect last run timestamp from state/sync_state.json (overrides --since)",
    )
    ex.add_argument(
        "--slim-tasks", action="store_true", dest="slim_tasks",
        help="Also write tasks_slim.json with 6 core fields",
    )
    ex.add_argument(
        "--output-dir", type=Path, default=Path("outputs"),
        dest="output_dir", metavar="PATH",
        help="Directory for output JSON files (default: outputs/)",
    )

    # ── Phase control ─────────────────────────────────────────────────────
    pc = p.add_argument_group("Phase control")
    pc.add_argument(
        "--extraction-only", action="store_true", dest="extraction_only",
        help="Run extraction only — skip all Milestone 2 phases",
    )
    pc.add_argument(
        "--skip-export", action="store_true", dest="skip_export",
        help="Skip the daily AI JSON export generation (Phase B)",
    )
    pc.add_argument(
        "--skip-sheets", action="store_true", dest="skip_sheets",
        help="Skip Google Sheets sync (Phase C)",
    )
    pc.add_argument(
        "--skip-drive", action="store_true", dest="skip_drive",
        help="Skip Google Drive upload (Phase C)",
    )
    pc.add_argument(
        "--skip-analytics", action="store_true", dest="skip_analytics",
        help="Skip analytics summary generation (Phase D)",
    )
    pc.add_argument(
        "--export-dir", type=Path, default=Path("daily_exports"),
        dest="export_dir", metavar="DIR",
        help="Directory for daily AI export files (default: daily_exports/)",
    )

    # ── Run behaviour ─────────────────────────────────────────────────────
    rb = p.add_argument_group("Run behaviour")
    rb.add_argument(
        "--fail-fast", action="store_true", dest="fail_fast",
        help="Abort the entire run on the first step failure (any phase)",
    )
    rb.add_argument(
        "--debug", action="store_true",
        help="Enable DEBUG logging",
    )

    return p


# ---------------------------------------------------------------------------
# Logging setup
# ---------------------------------------------------------------------------

def _configure_logging(debug: bool = False) -> logging.Logger:
    level = "DEBUG" if debug else os.environ.get("LOG_LEVEL", "INFO")
    configure_logging(log_level=level, log_dir="logs")
    return get_logger(__name__)


# ---------------------------------------------------------------------------
# ── PHASE A: Extraction steps ─────────────────────────────────────────────
# ---------------------------------------------------------------------------

def _step_validate_token(
    oauth: KommoOAuthClient,
    log: logging.Logger,
) -> None:
    """Step 1 — Validate that a token file exists and is readable."""
    log.info("[A-1] Validating token store...")
    if not oauth.tokens_exist():
        raise KommoTokenMissingError(
            "Token store not found. "
            "Run `python run_auth.py` to complete OAuth authorization first."
        )
    info = oauth.token_info()
    log.info(
        "Token validated — account=%s expires_in=%.0fs is_expired=%s",
        info.get("account_domain"),
        info.get("seconds_until_expiry", 0),
        info.get("is_expired"),
    )


def _step_refresh_token(
    oauth: KommoOAuthClient,
    log: logging.Logger,
) -> None:
    """Step 2 — Refresh the access token if expired or near expiry."""
    log.info("[A-2] Checking token freshness...")
    token = oauth.get_valid_token()
    log.info("Access token ready (length=%d)", len(token))


def _step_fetch_pipelines(
    client: KommoAPIClient,
    output_dir: Path,
    log: logging.Logger,
) -> StepResult:
    """Step 3 — Fetch all pipelines and stages."""
    log.info("[A-3] Fetching pipelines...")
    t0 = time.monotonic()
    try:
        ext    = PipelinesExtractor(client=client, output_dir=output_dir)
        result = ext.extract_all()
        log.info(
            "Pipelines done — pipelines=%d stages=%d duration=%.1fs",
            result.total_pipelines, result.total_stages, result.duration_seconds,
        )
        return StepResult(
            name="Pipelines", phase=PHASE_EXTRACTION,
            records=result.total_pipelines, skipped=result.failed_pipelines,
            output_path=result.output_path, duration_s=time.monotonic() - t0,
        )
    except KommoClientError as exc:
        log.error("Pipeline extraction failed: %s", exc)
        return StepResult(
            name="Pipelines", phase=PHASE_EXTRACTION,
            success=False, error=str(exc), duration_s=time.monotonic() - t0,
        )


def _step_fetch_leads(
    client: KommoAPIClient,
    output_dir: Path,
    since_ts: int | None,
    log: logging.Logger,
) -> StepResult:
    """Step 4 — Fetch all leads with pagination."""
    mode = f"since_ts={since_ts}" if since_ts else "full"
    log.info("[A-4] Fetching leads (%s mode)...", mode)
    t0 = time.monotonic()
    try:
        ext    = LeadsExtractor(client=client, output_dir=output_dir)
        result = (
            ext.extract_updated_since(since_ts) if since_ts else ext.extract_all()
        )
        log.info(
            "Leads done — records=%d pages=%d failed=%d duration=%.1fs",
            result.total_records, result.pages_fetched,
            result.failed_records, result.duration_seconds,
        )
        return StepResult(
            name="Leads", phase=PHASE_EXTRACTION,
            records=result.total_records, skipped=result.failed_records,
            output_path=result.output_path, duration_s=time.monotonic() - t0,
        )
    except KommoClientError as exc:
        log.error("Lead extraction failed: %s", exc)
        return StepResult(
            name="Leads", phase=PHASE_EXTRACTION,
            success=False, error=str(exc), duration_s=time.monotonic() - t0,
        )


def _step_fetch_tasks(
    client: KommoAPIClient,
    output_dir: Path,
    since_ts: int | None,
    slim: bool,
    log: logging.Logger,
) -> StepResult:
    """Step 5 — Fetch all tasks with pagination."""
    mode = f"since_ts={since_ts}" if since_ts else "full"
    log.info("[A-5] Fetching tasks (%s mode, slim=%s)...", mode, slim)
    t0 = time.monotonic()
    try:
        ext      = TasksExtractor(client=client, output_dir=output_dir)
        slim_path: Path | None = None
        if slim:
            result_t, slim_path = ext.extract_slim()
        elif since_ts:
            result_t = ext.extract_updated_since(since_ts)
        else:
            result_t = ext.extract_all()

        log.info(
            "Tasks done — records=%d completed=%d overdue=%d failed=%d duration=%.1fs",
            result_t.total_records, result_t.completed_count,
            result_t.overdue_count, result_t.failed_records, result_t.duration_seconds,
        )
        if slim_path:
            log.info("Slim tasks written → %s", slim_path)

        return StepResult(
            name="Tasks", phase=PHASE_EXTRACTION,
            records=result_t.total_records, skipped=result_t.failed_records,
            output_path=result_t.output_path, duration_s=time.monotonic() - t0,
        )
    except KommoClientError as exc:
        log.error("Task extraction failed: %s", exc)
        return StepResult(
            name="Tasks", phase=PHASE_EXTRACTION,
            success=False, error=str(exc), duration_s=time.monotonic() - t0,
        )


def _step_fetch_chats(
    client: KommoAPIClient,
    output_dir: Path,
    since_ts: int | None,
    log: logging.Logger,
) -> StepResult:
    """Step 5b — Fetch all chats and messages."""
    mode = f"since_ts={since_ts}" if since_ts else "full"
    log.info("[A-5b] Fetching chats (%s mode)...", mode)
    t0 = time.monotonic()
    try:
        ext = ChatsExtractor(client=client, output_dir=output_dir)
        if since_ts:
            result_c = ext.extract_since(since_ts)
        else:
            result_c = ext.extract_all()

        log.info(
            "Chats done — chats=%d messages=%d failed=%d duration=%.1fs",
            result_c.total_chats, result_c.total_messages,
            result_c.failed_chats, result_c.duration_seconds,
        )

        return StepResult(
            name="Chats & Messages", phase=PHASE_EXTRACTION,
            records=result_c.total_messages, skipped=result_c.failed_chats,
            output_path=result_c.output_path, duration_s=time.monotonic() - t0,
        )
    except KommoClientError as exc:
        log.error("Chat extraction failed: %s", exc)
        return StepResult(
            name="Chats & Messages", phase=PHASE_EXTRACTION,
            success=False, error=str(exc), duration_s=time.monotonic() - t0,
        )


def _step_verify_outputs(
    steps: list[StepResult],
    log: logging.Logger,
) -> None:
    """Step 6 — Verify expected output files exist and are non-empty."""
    log.info("[A-6] Verifying output files...")
    for step in steps:
        if not step.success:
            log.warning("  ⚠  %s — skipped (step failed)", step.name)
            continue
        if step.output_path is None:
            log.warning("  ⚠  %s — no output written (0 records?)", step.name)
            continue
        if not step.output_path.exists():
            log.error("  ✗  %s — output file missing: %s", step.name, step.output_path)
            continue
        size = step.output_path.stat().st_size
        log.info("  ✓  %s → %s (%.1f KB)", step.name, step.output_path, size / 1024)


# ---------------------------------------------------------------------------
# ── PHASE B: Daily AI Export ──────────────────────────────────────────────
# ---------------------------------------------------------------------------

def _step_generate_daily_export(
    export_dir: Path,
    output_dir: Path,
    log: logging.Logger,
) -> StepResult:
    """Step 7 — Generate daily_exports/YYYY-MM-DD.json from messages_flat.json."""
    log.info("[B-7] Generating daily AI export...")
    t0 = time.monotonic()
    try:
        from normalizers.daily_json_export import (
            DailyExportGenerator,
            DailyExportInputError,
            DailyExportError,
        )
        input_file = output_dir / "messages_flat.json"
        generator  = DailyExportGenerator(
            input_file=input_file,
            export_dir=export_dir,
        )
        result = generator.export_latest_day()

        if not result.success:
            log.error("Daily export failed: %s", result.error)
            return StepResult(
                name="Daily Export", phase=PHASE_EXPORT,
                success=False, error=result.error,
                duration_s=time.monotonic() - t0,
                critical=False,  # degradable — Drive/Sheets still run independently
            )

        log.info(
            "Daily export done — date=%s messages=%d leads=%d duration=%.2fs",
            result.date, result.total_messages, result.total_leads, result.duration_s,
        )
        return StepResult(
            name="Daily Export", phase=PHASE_EXPORT,
            records=result.total_messages,
            output_path=result.output_path,
            duration_s=time.monotonic() - t0,
            metadata={
                "export_date":    result.date,
                "total_leads":    result.total_leads,
                "total_messages": result.total_messages,
            },
            critical=False,
        )

    except DailyExportInputError as exc:
        log.warning(
            "Daily export skipped — messages_flat.json not available: %s", exc
        )
        return StepResult(
            name="Daily Export", phase=PHASE_EXPORT,
            success=False, error=str(exc),
            duration_s=time.monotonic() - t0,
            critical=False,
        )
    except Exception as exc:
        log.error("Unexpected error in daily export: %s", exc, exc_info=True)
        return StepResult(
            name="Daily Export", phase=PHASE_EXPORT,
            success=False, error=str(exc),
            duration_s=time.monotonic() - t0,
            critical=False,
        )


# ---------------------------------------------------------------------------
# ── PHASE C: Google Integrations ─────────────────────────────────────────
# ---------------------------------------------------------------------------

def _step_sync_sheets(
    output_dir: Path,
    log: logging.Logger,
) -> StepResult:
    """Step 8 — Push Leads + Messages to Google Sheets."""
    log.info("[C-8] Syncing Google Sheets...")
    t0 = time.monotonic()
    try:
        from integrations.google_sheets import (
            GoogleSheetsClient,
            GoogleSheetsConfigError,
            GoogleSheetsAuthError,
            SheetsWriter,
            load_json_output,
        )

        client = GoogleSheetsClient.from_env()
        log.info(
            "Sheets connected — title=%s id=%s",
            client.spreadsheet_title, client.spreadsheet_id,
        )
        writer  = SheetsWriter(client)
        results = []
        total_rows = 0

        # Leads
        leads_path = output_dir / "leads.json"
        if leads_path.exists():
            payload = load_json_output(leads_path)
            records = payload.get("data") or []
            r = writer.write_leads(records)
            results.append(r)
            if r.success:
                total_rows += r.rows_written
                log.info("Leads sheet written — rows=%d", r.rows_written)
            else:
                log.error("Leads sheet failed: %s", r.error)
        else:
            log.warning("leads.json not found — skipping Leads sheet")

        # Messages
        msgs_candidates = [
            output_dir / "messages_flat.json",
            output_dir / "chats.json",
        ]
        msgs_path = next((p for p in msgs_candidates if p.exists()), None)
        if msgs_path:
            payload = load_json_output(msgs_path)
            records = payload.get("messages") or payload.get("data") or []
            r = writer.write_messages(records)
            results.append(r)
            if r.success:
                total_rows += r.rows_written
                log.info("Messages sheet written — rows=%d", r.rows_written)
            else:
                log.error("Messages sheet failed: %s", r.error)
        else:
            log.warning("No messages file found — skipping Messages sheet")

        # Daily summary row
        leads_r = next((r for r in results if r.worksheet_name == "Leads"), None)
        msgs_r  = next((r for r in results if r.worksheet_name == "Messages"), None)
        summary_r = writer.write_daily_summary(
            leads_count=leads_r.rows_written if leads_r and leads_r.success else 0,
            messages_count=msgs_r.rows_written if msgs_r and msgs_r.success else 0,
            notes="OK" if all(r.success for r in results) else "Partial failures",
        )
        results.append(summary_r)

        any_failed = any(not r.success for r in results)
        return StepResult(
            name="Sheets Sync", phase=PHASE_SHEETS,
            success=not any_failed,
            records=total_rows,
            error="; ".join(r.error for r in results if not r.success and r.error) or None,
            duration_s=time.monotonic() - t0,
            metadata={"spreadsheet_url": client.spreadsheet_url},
            critical=False,
        )

    except (GoogleSheetsConfigError, GoogleSheetsAuthError) as exc:
        log.warning("Sheets sync skipped — config/auth error: %s", exc)
        return StepResult(
            name="Sheets Sync", phase=PHASE_SHEETS,
            success=False, error=str(exc),
            duration_s=time.monotonic() - t0,
            critical=False,
        )
    except Exception as exc:
        log.error("Unexpected Sheets error: %s", exc, exc_info=True)
        return StepResult(
            name="Sheets Sync", phase=PHASE_SHEETS,
            success=False, error=str(exc),
            duration_s=time.monotonic() - t0,
            critical=False,
        )


def _step_upload_drive(
    export_dir: Path,
    log: logging.Logger,
) -> StepResult:
    """Step 9 — Upload latest daily export to Google Drive."""
    log.info("[C-9] Uploading to Google Drive...")
    t0 = time.monotonic()
    try:
        from integrations.google_drive import (
            GoogleDriveClient,
            DriveUploader,
            GoogleDriveConfigError,
            GoogleDriveAuthError,
            GoogleDriveNotFoundError,
        )

        client   = GoogleDriveClient.from_env()
        uploader = DriveUploader(client, export_dir=export_dir)
        result   = uploader.upload_latest_export()

        if not result.success:
            log.error("Drive upload failed: %s", result.error)
            return StepResult(
                name="Drive Upload", phase=PHASE_DRIVE,
                success=False, error=result.error,
                duration_s=time.monotonic() - t0,
                critical=False,
            )

        log.info(
            "Drive upload done — file=%s action=%s size=%d bytes link=%s",
            result.filename, result.action_label, result.size_bytes, result.web_view_link,
        )
        return StepResult(
            name="Drive Upload", phase=PHASE_DRIVE,
            records=1,
            duration_s=time.monotonic() - t0,
            metadata={
                "filename":      result.filename,
                "action":        result.action_label,
                "web_view_link": result.web_view_link,
                "size_bytes":    result.size_bytes,
                "folder_url":    client.folder_url,
            },
            critical=False,
        )

    except (GoogleDriveConfigError, GoogleDriveAuthError) as exc:
        log.warning("Drive upload skipped — config/auth error: %s", exc)
        return StepResult(
            name="Drive Upload", phase=PHASE_DRIVE,
            success=False, error=str(exc),
            duration_s=time.monotonic() - t0,
            critical=False,
        )
    except (FileNotFoundError, GoogleDriveNotFoundError) as exc:
        log.warning("Drive upload skipped — no exports found: %s", exc)
        return StepResult(
            name="Drive Upload", phase=PHASE_DRIVE,
            success=False, error=str(exc),
            duration_s=time.monotonic() - t0,
            critical=False,
        )
    except Exception as exc:
        log.error("Unexpected Drive error: %s", exc, exc_info=True)
        return StepResult(
            name="Drive Upload", phase=PHASE_DRIVE,
            success=False, error=str(exc),
            duration_s=time.monotonic() - t0,
            critical=False,
        )


# ---------------------------------------------------------------------------
# ── PHASE D: Analytics Summary ────────────────────────────────────────────
# ---------------------------------------------------------------------------

def _step_generate_analytics(
    all_steps: list[StepResult],
    run_start: float,
    since_ts: int | None,
    log: logging.Logger,
) -> StepResult:
    """
    Step 10 — Write a machine-readable analytics summary JSON to logs/.

    The file is named logs/analytics_YYYY-MM-DD.json and contains per-step
    timing, record counts, and phase-level success flags — useful for
    dashboards, alerting, and longitudinal tracking.
    """
    log.info("[D-10] Generating analytics summary...")
    t0 = time.monotonic()

    run_ts     = datetime.now(tz=timezone.utc)
    total_dur  = time.monotonic() - run_start
    any_failed = any(not s.success and s.critical for s in all_steps)

    summary: dict[str, Any] = {
        "_meta": {
            "schema_version": "2.0",
            "generated_at":   run_ts.strftime("%Y-%m-%dT%H:%M:%SZ"),
            "pipeline_mode":  "incremental" if since_ts else "full",
            "overall_status": "FAILED" if any_failed else "SUCCESS",
            "total_duration_s": round(total_dur, 2),
        },
        "phases": {},
        "steps":  [],
    }

    # Per-step records
    for s in all_steps:
        summary["steps"].append({
            "name":       s.name,
            "phase":      s.phase,
            "status":     s.status_label,
            "records":    s.records,
            "skipped":    s.skipped,
            "duration_s": round(s.duration_s, 3),
            "critical":   s.critical,
            "error":      s.error,
            **s.metadata,
        })

    # Per-phase roll-up
    phases: dict[str, list[StepResult]] = {}
    for s in all_steps:
        phases.setdefault(s.phase, []).append(s)

    for phase_name, phase_steps in phases.items():
        p_failed = any(not s.success for s in phase_steps)
        summary["phases"][phase_name] = {
            "status":      "FAILED" if p_failed else "SUCCESS",
            "steps_total": len(phase_steps),
            "steps_ok":    sum(1 for s in phase_steps if s.success),
            "duration_s":  round(sum(s.duration_s for s in phase_steps), 3),
            "records":     sum(s.records for s in phase_steps),
        }

    try:
        log_dir = Path("logs")
        log_dir.mkdir(parents=True, exist_ok=True)
        analytics_path = log_dir / f"analytics_{run_ts.strftime('%Y-%m-%d')}.json"

        # Atomic write
        tmp = analytics_path.with_suffix(".tmp")
        tmp.write_text(
            json.dumps(summary, indent=2, ensure_ascii=False),
            encoding="utf-8",
        )
        tmp.replace(analytics_path)

        log.info("Analytics summary written → %s", analytics_path)
        return StepResult(
            name="Analytics", phase=PHASE_ANALYTICS,
            output_path=analytics_path,
            duration_s=time.monotonic() - t0,
            critical=False,
        )
    except Exception as exc:
        log.error("Analytics write failed: %s", exc)
        return StepResult(
            name="Analytics", phase=PHASE_ANALYTICS,
            success=False, error=str(exc),
            duration_s=time.monotonic() - t0,
            critical=False,
        )


# ---------------------------------------------------------------------------
# ── PHASE E: Final report ────────────────────────────────────────────────
# ---------------------------------------------------------------------------

def _print_final_report(
    all_steps: list[StepResult],
    run_start: float,
    since_ts: int | None,
    log: logging.Logger,
) -> None:
    """Step 11 — Print and log a structured end-of-run summary."""
    total_dur  = time.monotonic() - run_start
    total_recs = sum(s.records for s in all_steps)
    any_crit   = any(not s.success and s.critical for s in all_steps)
    any_warn   = any(not s.success and not s.critical for s in all_steps)

    run_status = "FAILED" if any_crit else ("PARTIAL" if any_warn else "SUCCESS")

    log.info(
        "PIPELINE_SUMMARY status=%s steps=%d records=%d duration_s=%.1f",
        run_status, len(all_steps), total_recs, total_dur,
    )

    # ── Per-step structured log ────────────────────────────────────────
    for s in all_steps:
        log.info(
            "STEP status=%s name=%s phase=%s records=%d duration_s=%.2f error=%s",
            s.status_label, s.name, s.phase, s.records,
            s.duration_s, s.error or "none",
        )

    # ── Console table ─────────────────────────────────────────────────
    width = 72
    print("\n" + "═" * width)
    if any_crit:
        print("  ❌  PIPELINE COMPLETE — CRITICAL FAILURES")
    elif any_warn:
        print("  ⚠️   PIPELINE COMPLETE — WITH NON-CRITICAL ERRORS")
    else:
        print("  ✅  PIPELINE COMPLETE")
    print("═" * width)

    # Group by phase for readability
    phases_seen: list[str] = []
    for s in all_steps:
        if s.phase not in phases_seen:
            phases_seen.append(s.phase)
            print(f"\n  ── {s.phase} {'─' * (width - len(s.phase) - 6)}")

        icon  = s.status_icon
        dur   = f"{s.duration_s:.1f}s"
        recs  = f"{s.records:,} records" if s.records else ""

        if not s.success:
            crit_tag = "" if s.critical else " [non-critical]"
            print(f"  {icon}  {s.name:<18} FAILED{crit_tag} — {s.error or 'unknown'}")
        else:
            print(f"  {icon}  {s.name:<18} {recs:<22} [{dur}]")
            if s.output_path and Path(s.output_path).exists():
                kb = Path(s.output_path).stat().st_size / 1024
                print(f"            📄 {s.output_path}  ({kb:.1f} KB)")
            if s.metadata.get("web_view_link"):
                print(f"            🔗 {s.metadata['web_view_link']}")
            if s.metadata.get("spreadsheet_url"):
                print(f"            🔗 {s.metadata['spreadsheet_url']}")
            if s.skipped:
                print(f"            ⚠️  {s.skipped} records dead-lettered → outputs/errors/")

    mode_label = f"incremental (since_ts={since_ts})" if since_ts else "full"
    print("\n" + "─" * width)
    print(f"  Mode           : {mode_label}")
    print(f"  Total records  : {total_recs:,}")
    print(f"  Total steps    : {len(all_steps)}")
    print(f"  Total duration : {total_dur:.1f}s")
    print(f"  Timestamp      : {datetime.now(tz=timezone.utc).strftime('%Y-%m-%dT%H:%M:%SZ')}")
    print("═" * width + "\n")


# ---------------------------------------------------------------------------
# Main entry point
# ---------------------------------------------------------------------------

def main() -> int:
    """
    Orchestrate the full Kommo CRM M1+M2 pipeline.

    Returns:
        0 — All critical steps succeeded.
        1 — One or more critical steps failed.
        2 — Auth / config failure (nothing ran).
    """
    args = _build_parser().parse_args()
    log  = _configure_logging(debug=args.debug)

    # ── Validate config ────────────────────────────────────────────────
    try:
        from config import settings  # noqa: F401
    except KommoConfigError as exc:
        print(f"\n  ❌  Configuration error:\n", file=sys.stderr)
        print(f"     {exc}", file=sys.stderr)
        print("\n  → Copy .env.example to .env and fill in all required values.\n",
              file=sys.stderr)
        return 2

    # ── Apply --extraction-only shortcut ───────────────────────────────
    if args.extraction_only:
        args.skip_export    = True
        args.skip_sheets    = True
        args.skip_drive     = True
        args.skip_analytics = True

    run_start = time.monotonic()
    run_ts    = datetime.now(tz=timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")

    # ── Banner ─────────────────────────────────────────────────────────
    print("\n" + "═" * 72)
    print("  Kommo CRM Integration — Full Pipeline (M1 + M2)")
    print(f"  Started : {run_ts}")
    print("═" * 72 + "\n")

    all_steps: list[StepResult] = []

    # ── State manager ──────────────────────────────────────────────────
    sm = StateManager()
    log.info("Sync state loaded from %s", sm._path)

    # ── Resolve incremental timestamp ──────────────────────────────────
    since_ts: int | None = None
    if getattr(args, "auto_incremental", False):
        candidates = [
            sm.get_last_run_timestamp(e)
            for e in ("leads", "tasks", "pipelines", "chats")
        ]
        valid = [ts for ts in candidates if ts is not None]
        if valid:
            since_ts = min(valid)
            log.info("Auto-incremental: since_ts=%d", since_ts)
        else:
            log.info("Auto-incremental: no prior state — running full extraction")
    elif args.since_ts:
        since_ts = args.since_ts
    elif args.since:
        since_ts = int(time.time()) - args.since

    # ── OAuth client ───────────────────────────────────────────────────
    try:
        oauth = KommoOAuthClient()
    except EnvironmentError as exc:
        log.critical("Configuration error: %s", exc)
        print(f"\n  ❌  Config error: {exc}\n")
        return 2

    # ==================================================================
    # PHASE A — EXTRACTION
    # ==================================================================
    print("  ─── Phase A: Extraction ───────────────────────────────────────\n")

    # Step 1: Validate token (auth failure → exit 2 immediately)
    try:
        _step_validate_token(oauth, log)
    except KommoTokenMissingError as exc:
        log.critical("Token missing: %s", exc)
        print(f"\n  ❌  {exc}\n  → Run `python run_auth.py` first.\n")
        return 2
    except KommoOAuthError as exc:
        log.critical("Token validation error: %s", exc)
        print(f"\n  ❌  Token error: {exc}\n")
        return 2

    # Step 2: Refresh token (auth failure → exit 2)
    try:
        _step_refresh_token(oauth, log)
    except KommoAuthorizationError as exc:
        log.critical("Token refresh failed: %s", exc)
        print(f"\n  ❌  Auth error: {exc}\n  → Run `python run_auth.py` again.\n")
        return 2
    except KommoOAuthError as exc:
        log.critical("Unexpected auth error: %s", exc)
        print(f"\n  ❌  Auth error: {exc}\n")
        return 2

    # Steps 3–5: Extraction inside shared HTTP session
    try:
        with KommoAPIClient(oauth) as client:

            # Connectivity check
            try:
                account = client.health_check()
                log.info(
                    "Connected — account=%s domain=%s.kommo.com",
                    account.get("name", "unknown"), oauth.account_domain,
                )
                print(f"  🔗 Connected: {account.get('name')} ({oauth.account_domain}.kommo.com)\n")
            except KommoClientError as exc:
                log.critical("API health check failed: %s", exc)
                print(f"\n  ❌  Cannot reach Kommo API: {exc}\n")
                return 2

            # Step 3: Pipelines
            r3 = _step_fetch_pipelines(client, args.output_dir, log)
            all_steps.append(r3)
            sm.mark_success("pipelines", records=r3.records) if r3.success else \
                sm.mark_failed("pipelines", error=r3.error or "unknown")
            if not r3.success and args.fail_fast:
                log.warning("--fail-fast: aborting after pipeline failure")
                _print_final_report(all_steps, run_start, since_ts, log)
                return 1

            # Step 4: Leads
            r4 = _step_fetch_leads(client, args.output_dir, since_ts, log)
            all_steps.append(r4)
            if r4.success:
                sm.mark_partial("leads", records=r4.records, failed_records=r4.skipped) \
                    if r4.skipped else sm.mark_success("leads", records=r4.records)
            else:
                sm.mark_failed("leads", error=r4.error or "unknown")
            if not r4.success and args.fail_fast:
                log.warning("--fail-fast: aborting after lead failure")
                _print_final_report(all_steps, run_start, since_ts, log)
                return 1

            # Step 5: Tasks
            r5 = _step_fetch_tasks(
                client, args.output_dir, since_ts,
                slim=args.slim_tasks, log=log,
            )
            all_steps.append(r5)
            if r5.success:
                sm.mark_partial("tasks", records=r5.records, failed_records=r5.skipped) \
                    if r5.skipped else sm.mark_success("tasks", records=r5.records)
            else:
                sm.mark_failed("tasks", error=r5.error or "unknown")
            if not r5.success and args.fail_fast:
                log.warning("--fail-fast: aborting after task failure")
                _print_final_report(all_steps, run_start, since_ts, log)
                return 1

            # Step 5b: Chats & Messages
            r_chats = _step_fetch_chats(client, args.output_dir, since_ts, log)
            all_steps.append(r_chats)
            if r_chats.success:
                sm.mark_partial("chats", records=r_chats.records, failed_records=r_chats.skipped) \
                    if r_chats.skipped else sm.mark_success("chats", records=r_chats.records)
            else:
                sm.mark_failed("chats", error=r_chats.error or "unknown")
            if not r_chats.success and args.fail_fast:
                log.warning("--fail-fast: aborting after chats failure")
                _print_final_report(all_steps, run_start, since_ts, log)
                return 1

    except Exception as exc:
        log.exception("Fatal error during extraction: %s", exc)
        print(f"\n  ❌  Unexpected error: {exc}\n")
        return 1

    # Step 6: Verify outputs (no exit — just logs)
    _step_verify_outputs(
        [s for s in all_steps if s.phase == PHASE_EXTRACTION], log
    )

    # ==================================================================
    # PHASE B — AI EXPORT
    # ==================================================================
    if not args.skip_export:
        print("\n  ─── Phase B: AI Export ────────────────────────────────────────\n")
        r7 = _step_generate_daily_export(args.export_dir, args.output_dir, log)
        all_steps.append(r7)
        if not r7.success and args.fail_fast:
            log.warning("--fail-fast: aborting after export failure")
            _print_final_report(all_steps, run_start, since_ts, log)
            return 1

    # ==================================================================
    # PHASE C — GOOGLE INTEGRATIONS
    # ==================================================================
    run_integrations = not (args.skip_sheets and args.skip_drive)
    if run_integrations:
        print("\n  ─── Phase C: Google Integrations ──────────────────────────────\n")

    if not args.skip_sheets:
        r8 = _step_sync_sheets(args.output_dir, log)
        all_steps.append(r8)
        if not r8.success and args.fail_fast:
            log.warning("--fail-fast: aborting after Sheets failure")
            _print_final_report(all_steps, run_start, since_ts, log)
            return 1

    if not args.skip_drive:
        r9 = _step_upload_drive(args.export_dir, log)
        all_steps.append(r9)
        if not r9.success and args.fail_fast:
            log.warning("--fail-fast: aborting after Drive failure")
            _print_final_report(all_steps, run_start, since_ts, log)
            return 1

    # ==================================================================
    # PHASE D — ANALYTICS
    # ==================================================================
    if not args.skip_analytics:
        print("\n  ─── Phase D: Analytics ────────────────────────────────────────\n")
        r10 = _step_generate_analytics(all_steps, run_start, since_ts, log)
        all_steps.append(r10)

    # ==================================================================
    # PHASE E — FINAL REPORT
    # ==================================================================
    print("\n  ─── Phase E: Run Report ───────────────────────────────────────")
    _print_final_report(all_steps, run_start, since_ts, log)
    log.info("Updated sync state:\n%s", sm.summary())

    # Critical failures drive exit code; non-critical failures degrade gracefully
    critical_failed = any(not s.success and s.critical for s in all_steps)
    return 1 if critical_failed else 0


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    sys.exit(main())
